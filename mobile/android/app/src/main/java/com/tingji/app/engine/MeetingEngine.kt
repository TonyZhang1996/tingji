package com.tingji.app.engine

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.file.Files
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.min

/** Android's persistent single-job controller. Keys never enter any stored object. */
class MeetingEngine(
    context:Context,
    private val mediaUrl:(File)->String,
    private val onTaskChanged:()->Unit,
    private val speakerAnalyzer:SpeakerAnalyzer,
    private val clientFactory:(String,()->Boolean)->DeepSeekClient={ key,cancelled -> DeepSeekClient(key,cancelled) }
) {
    val dataRoot=File(context.filesDir,"tingji")
    private val importsRoot=File(context.filesDir,"imports")
    private val jobsRoot=File(dataRoot,"jobs")
    private val cacheRoot=context.cacheDir
    private val sharedExportsRoot=File(cacheRoot,"shared_exports")
    private val settingsFile=File(dataRoot,"settings.json")
    private val lock=Any()
    private val jobs=linkedMapOf<String,JSONObject>()
    private var selected:String?=null
    private var settings=JSONObject()
    private val executor=Executors.newSingleThreadExecutor()
    private val cancelled=AtomicBoolean(false)
    private var task=JSONObject().put("running",false).put("stage","idle").put("current",0).put("total",0).put("message","选择录音，开始第一份转写。")
    private val logs=ArrayDeque<JSONObject>()

    init {
        jobsRoot.mkdirs()
        settings=JSONObject().put("num_speakers",0).put("max_workers",1).put("hourly_price",0.39).put("retry_uncertain",false).put("api_key_prompt_seen",false).put("output_root",jobsRoot.absolutePath)
        val saved=readJson(settingsFile)
        if(saved!=null) try { updateSettings(saved,false) } catch(_:Exception) { }
        jobsRoot.listFiles()?.filter { it.isDirectory && validId(it.name) }?.sortedByDescending { it.lastModified() }?.forEach { folder ->
            val job=readJson(File(folder,"job.json"))
            try {
                require(job!=null && job.optString("id")==folder.name)
                validateLedger(job)
                val segments=job.optJSONArray("segments")?:JSONArray()
                segments.objects().filter { it.optString("state")=="requesting" }.forEach { it.put("state","ambiguous").put("error","上次请求中断，结果不明，可能已计费。") }
                if(job.optJSONObject("ai_summary")?.optString("status")=="running") {
                    job.getJSONObject("ai_summary").put("status","error").put("message","上次总结中断，已有结果保留。可重新生成，先复用已保存的小结。")
                }
                jobs[folder.name]=job
                writeJson(File(folder,"job.json"),job)
            } catch(_:Exception) {log("一份本地任务记录无法验证，未自动续传或删除；原有录音和缓存已保留。")}
        }
        selected=jobs.keys.firstOrNull()
    }

    /** The shell has already copied a selected SAF document into private imports. */
    fun acceptAudio(file:File,name:String):JSONObject {
        val speakerCount=synchronized(lock) {
            ensureIdle();cancelled.set(false)
            task=JSONObject().put("running",true).put("stage","import").put("current",0).put("total",0).put("message","正在校验导入的录音…")
            changed();settings.getInt("num_speakers")
        }
        val id=randomId();val folder=File(jobsRoot,id);folder.mkdirs()
        val extension=name.substringAfterLast('.',"").lowercase().takeIf { it.matches(Regex("[a-z0-9]{1,8}")) }?:"audio"
        val source=File(folder,"source.$extension")
        try {
            require(file.isFile && file.length()>0) { "请选择有效的本地录音。" }
            val duration=AudioTools.duration(file)
            require(duration.isFinite() && duration>0) { "无法读取录音时长。" }
            require(duration<=24*3600) { "单条录音暂支持24小时以内，请先分成几场会议。" }
            if(within(importsRoot,file) && file.renameTo(source)) { /* consume our private SAF copy */ }
            else file.inputStream().use { input -> source.outputStream().use { output ->
                val buffer=ByteArray(65536);while(true){checkCancelled();val n=input.read(buffer);if(n<0)break;output.write(buffer,0,n)}
            } }
            val title=safeName(name.substringBeforeLast('.',name))
            val job=JSONObject().put("id",id).put("title",title).put("source_name",safeName(name)).put("source_file",source.name)
                .put("duration_seconds",duration).put("created_at",nowIso()).put("updated_at",nowIso()).put("status","pending").put("complete",false)
                .put("speaker_count",0).put("speaker_names",JSONObject()).put("turns",JSONArray()).put("segments",JSONArray()).put("samples",JSONArray())
                .put("ai_summary",JSONObject().put("status","empty")).put("analysis_num_speakers",speakerCount)
                .put("source_bytes",source.length()).put("source_hash",fileHash(source){cancelled.get()})
            synchronized(lock) {
                jobs[id]=job;selected=id;save(job)
                log("已导入录音：${clock(duration)}。人物分析在本机完成，转写和总结调用 DeepSeek。")
            }
        } catch(e:Exception) { if(source.exists())source.delete();folder.delete();throw e }
        finally {synchronized(lock) {task.put("running",false).put("stage","idle").put("message","录音已准备好，可分析人物或开始转写。");changed()}}
        return state()
    }

    fun state():JSONObject = synchronized(lock) {
        val histories=jobs.values.sortedByDescending { it.optString("updated_at") }.map { brief(it) }
        val job=selected?.let { jobs[it] }
        val source=job?.let { j -> JSONObject().put("path",sourceFile(j).absolutePath).put("name",j.optString("source_name",j.getString("title")))
            .put("duration_seconds",j.getDouble("duration_seconds")).put("audio_url",mediaOrNull(playbackFile(j))) }
        JSONObject().put("version","2.3.0-android.3").put("source",source?:JSONObject.NULL).put("settings",cloneJson(settings))
            .put("jobs",jsonArray(histories)).put("selected_job",job?.let { payload(it) }?:JSONObject.NULL).put("task",cloneJson(task)).put("logs",jsonArray(logs.toList()))
    }
    fun taskState():JSONObject = synchronized(lock) {cloneJson(task)}

    fun dispatch(action:String,payload:JSONObject=JSONObject()):JSONObject {
        when(action) {
            "state"->Unit
            "cancel","cancel_summary"->cancel()
            "config"-> synchronized(lock) { ensureIdle();updateSettings(payload,true);changed() }
            "select_job"-> synchronized(lock) { ensureIdle();val id=payload.getString("id");require(validId(id)&&jobs.containsKey(id)) { "任务不存在。" };selected=id;changed() }
            "new_task"-> synchronized(lock) {ensureIdle();selected=null;changed()}
            "delete_job"-> synchronized(lock) { ensureIdle();deleteJob(payload.getString("job_id"));changed() }
            "analyze"->start("analyze","")
            "transcribe"->{
                synchronized(lock) {
                    ensureIdle()
                    if(payload.has("retry_uncertain")) { require(payload.opt("retry_uncertain") is Boolean);settings.put("retry_uncertain",payload.getBoolean("retry_uncertain"));writeJson(settingsFile,settings) }
                }
                start("transcribe",payload.optString("api_key"))
            }
            "summarize"->{
                require(payload.opt("allow_partial")==null || payload.opt("allow_partial") is Boolean)
                require(payload.opt("retry_failed")==null || payload.opt("retry_failed") is Boolean)
                start("summary",payload.optString("api_key"),payload.optBoolean("allow_partial",false),payload.optBoolean("retry_failed",false))
            }
            "speaker_names"-> synchronized(lock) {
                ensureIdle();val job=current();val names=payload.getJSONObject("names");val allowed=job.getJSONArray("segments").objects().map { it.getString("speaker") }.toSet()
                require(names.keys().asSequence().all { it in allowed && names.opt(it) is String && names.getString(it).length<=100 }) { "人物姓名与当前录音不匹配。" }
                job.put("speaker_names",cloneJson(names));save(job);writeExports(job);changed()
            }
            "segment_review"-> synchronized(lock) {
                ensureIdle();val job=current();val segment=job.getJSONArray("segments").objects().firstOrNull { it.getString("id")==payload.getString("id") }?:throw IllegalArgumentException("片段不存在。")
                require(segment.optString("state")=="done") { "只能校对已转写的片段。" }
                if(payload.has("text"))require(payload.opt("text") is String && payload.getString("text").length<=100000)
                if(payload.has("speaker")) {
                    val ids=job.getJSONArray("turns").objects().map { it.getString("speaker") }.toSet()
                    require(payload.getString("speaker") in ids)
                }
                if(payload.has("reviewed"))require(payload.opt("reviewed") is Boolean)
                if(payload.has("text"))segment.put("text",payload.getString("text")).put("edited",true)
                if(payload.has("speaker"))segment.put("speaker",payload.getString("speaker")).put("edited",true)
                if(payload.has("reviewed"))segment.put("reviewed",payload.getBoolean("reviewed"))
                save(job);writeExports(job);changed()
            }
            else->throw IllegalArgumentException("不支持的操作。")
        }
        return state()
    }

    fun cancel() = synchronized(lock) { cancelled.set(true);if(task.optBoolean("running"))task.put("cancel_requested",true).put("message","正在停止；已发出的请求返回后会保存结果。");changed() }
    fun close() { cancel();executor.shutdown() }

    fun exportFile(format:String):File? = synchronized(lock) {
        require(format in setOf("txt","md","json","csv","srt")) { "导出格式不支持。" }
        val job=selected?.let { jobs[it] }?:return@synchronized null
        if(job.getJSONArray("segments").objects().none { it.optString("state")=="done" })return@synchronized null
        writeExports(job);File(folder(job),exportName(format)).takeIf { it.isFile }
    }
    fun summaryFile():File? = synchronized(lock) {
        val job=selected?.let { jobs[it] }?:return@synchronized null
        val report=job.optJSONObject("ai_summary")?.optJSONObject("report")?:return@synchronized null
        val file=File(folder(job),"AI总结.md");writeText(file,MeetingSummary.markdown(job.getString("title"),report));file
    }

    private fun start(kind:String,secret:String,allowPartial:Boolean=false,retryFailed:Boolean=false) = synchronized(lock) {
        ensureIdle();val job=current()
        if(kind!="analyze")require(secret.isNotBlank() && secret.length<=512) { "请填写自己的 DeepSeek API Key。" }
        validateLedger(job)
        if(kind!="summary") {
            val source=sourceFile(job)
            require(source.isFile && source.length()==job.getLong("source_bytes")) {"原录音缓存发生变化。为避免重复计费，已停止，请保留任务并检查本地文件。"}
            if(job.getJSONArray("segments").length()==0)job.put("analysis_num_speakers",settings.getInt("num_speakers"))
        }
        if(kind=="summary") {
            val segments=job.getJSONArray("segments").objects();require(segments.any { it.optString("state")=="done" && it.optString("text").isNotBlank() }) { "请先转写录音。" }
            require(allowPartial || segments.all { it.optString("state")=="done" }) { "还有未完成片段，请先转写，或明确选择仅总结已完成部分。" }
            val summary=job.optJSONObject("ai_summary")?:JSONObject();summary.put("status","running").put("message","正在整理录音总结");job.put("ai_summary",summary);save(job)
        }
        cancelled.set(false)
        task=JSONObject().put("running",true).put("stage",if(kind=="summary")"summary" else "preparing").put("current",0).put("total",0).put("cancel_requested",false).put("message",if(kind=="summary")"正在生成总结" else "正在准备录音处理")
        changed()
        executor.execute {
            try {
                if(kind=="summary") {
                    val snapshot=synchronized(lock) { cloneJson(job) }
                    val service=MeetingSummary(folder(job),clientFactory(secret) {cancelled.get()},secret,{cancelled.get()},{done,total,message->
                        synchronized(lock) { job.getJSONObject("ai_summary").put("progress",JSONObject().put("stage","summary").put("current",done).put("total",total).put("message",message));updateProgress("summary",done,total,message) }
                    },retryFailed)
                    val report=service.generate(snapshot,allowPartial)
                    synchronized(lock) {
                        if(cancelled.get())throw TaskCancelled()
                        val summary=job.getJSONObject("ai_summary");summary.put("status","complete").put("report",report).put("message","录音总结已保存").remove("progress")
                        writeText(File(folder(job),"AI总结.md"),MeetingSummary.markdown(job.getString("title"),report));save(job)
                        updateProgress("done",1,1,"总体、逐人总结和待办事项已保存。")
                    }
                } else {
                    require(fileHash(sourceFile(job)){cancelled.get()}==job.getString("source_hash")) {"原录音缓存发生变化。为避免重复计费，已停止，请保留任务并检查本地文件。"}
                    analyze(job)
                    if(kind=="transcribe")transcribe(job,secret)
                    synchronized(lock) { updateProgress(if(kind=="analyze")"analyzed" else if(job.optBoolean("complete"))"done" else "partial",0,0,
                        if(kind=="analyze")"人物分析完成，可以试听命名。" else if(job.optBoolean("complete"))"转写已完成并保存。" else "已保存当前进度，请查看未完成片段。") }
                }
            } catch(e:Exception) {
                synchronized(lock) {
                    val message=(e.message?:"操作未完成，已保存进度。").replace(secret.takeIf{it.isNotBlank()}?:"\u0000","[已隐藏]").take(1000)
                    log(message)
                    if(kind=="summary") { job.getJSONObject("ai_summary").put("status",if(e is TaskCancelled)"cancelled" else "error").put("message",message);save(job) }
                    updateProgress(if(e is TaskCancelled)"cancelled" else "error",task.optInt("current"),task.optInt("total"),message)
                }
            } finally {
                synchronized(lock) {task.put("running",false).put("cancel_requested",false);changed()}
            }
        }
    }

    private fun analyze(job:JSONObject) {
        val segments=job.getJSONArray("segments")
        if(segments.length()>0) {
            require(normalizedFile(job).isFile) { "分析缓存丢失。为避免重复计费，请保留任务，重新导入录音时会创建新任务。" }
            return
        }
        checkCancelled();updateProgress("convert",0,0,"正在分块解码为16kHz单声道音频")
        val wav=normalizedFile(job)
        AudioTools.normalize(sourceFile(job),wav,{cancelled.get()}) { message-> updateProgress("convert",0,0,message) }
        val duration=AudioTools.wavInfo(wav).duration
        checkCancelled();updateProgress("diarize",0,0,"正在本地分析整场人物")
        val raw=speakerAnalyzer.analyze(wav,File(folder(job),"speaker-cache"),job.optInt("analysis_num_speakers"),{cancelled.get()}) { done,total,message ->updateProgress("diarize",done,total,message) }
        require(raw.isNotEmpty()) { "没有检测到可用发言，请检查录音音量。" }
        val turns=JSONArray();var previousEnd=0.0
        raw.sortedBy { it.start }.forEach { turn ->
            require(turn.start.isFinite() && turn.end.isFinite() && turn.start>=previousEnd-.002 && turn.start>=0 && turn.end>turn.start && turn.end<=duration+.1 && Regex("S[0-9]{1,2}").matches(turn.speaker)) { "人物分析时间段不正确，原始录音仍保留。" }
            var start=turn.start
            while(start<turn.end-0.0001) {
                val end=min(turn.end,start+110.0)
                val index=turns.length();val id=sha256("$index|$start|$end|${turn.speaker}").take(20)
                turns.put(JSONObject().put("id",id).put("index",index).put("start",start).put("end",end).put("speaker",turn.speaker)
                    .put("uncertain",turn.uncertain || end-start<0.8).put("state","pending").put("text","").put("reviewed",false))
                start=end
            }
            previousEnd=turn.end
        }
        val speakers=turns.objects().map { it.getString("speaker") }.distinct()
        val samples=JSONArray();val samplesDir=File(folder(job),"samples");samplesDir.mkdirs()
        for(speaker in speakers) {
            checkCancelled()
            val turn=turns.objects().filter { it.getString("speaker")==speaker }.maxBy { it.getDouble("end")-it.getDouble("start") }
            val start=turn.getDouble("start");val end=min(turn.getDouble("end"),start+15)
            AudioTools.slice(wav,File(samplesDir,"$speaker.wav"),start,end) {cancelled.get()}
            samples.put(JSONObject().put("speaker",speaker).put("file","samples/$speaker.wav").put("start",start).put("end",end).put("uncertain",turn.optBoolean("uncertain")).put("too_short",end-start<3))
        }
        synchronized(lock) {
            job.put("duration_seconds",duration).put("turns",JSONArray(turns.toString())).put("segments",turns).put("samples",samples).put("speaker_count",speakers.size).put("status","analyzed")
            save(job);log("人物分析完成：${speakers.size} 人，${turns.length()} 段。短句及重叠讲话仍需回听确认。")
        }
    }

    private fun transcribe(job:JSONObject,secret:String) {
        val client=clientFactory(secret) {cancelled.get()}
        val segments=job.getJSONArray("segments").objects()
        val retry=synchronized(lock) {settings.optBoolean("retry_uncertain",false)}
        var completed=segments.count { it.optString("state")=="done" }
        var globalFailure:DeepSeekFailure?=null
        for(segment in segments) {
            checkCancelled()
            if(segment.optString("state")=="done")continue
            if(segment.optString("state") in listOf("ambiguous","requesting") && !retry)continue
            val piece=File(folder(job),"upload-${segment.getString("id")}.wav")
            try {
                AudioTools.slice(normalizedFile(job),piece,segment.getDouble("start"),segment.getDouble("end")) {cancelled.get()}
                synchronized(lock) { segment.put("state","requesting").put("attempts",segment.optInt("attempts")+1);segment.remove("error");save(job);updateProgress("transcribe",completed,segments.size,"正在转写 ${segment.getInt("index")+1}/${segments.size} 段") }
                try {
                    val response=client.transcribe(piece)
                    val text=response.getString("text").replace(secret,"[已隐藏]")
                    synchronized(lock) {
                        segment.put("state","done").put("text",text).put("request_id",response.optString("request_id").replace(secret,"[已隐藏]").take(250))
                        if(text.isBlank())segment.put("uncertain",true).put("empty_recognition",true)
                        completed++;save(job);writeExports(job)
                    }
                } catch(e:TaskCancelled) {
                    synchronized(lock) {segment.put("state","pending");save(job)};throw e
                } catch(e:DeepSeekFailure) {
                    synchronized(lock) {segment.put("state",if(e.ambiguous)"ambiguous" else "failed").put("error",e.message?.replace(secret,"[已隐藏]"));save(job)}
                    if(e.stopAll) { globalFailure=e;break }
                    if(e.httpStatus in listOf(429,500,503)) {
                        // Explicit service rejection: cool down before other
                        // unsent segments, never resubmit this segment silently.
                        repeat(20) {checkCancelled();Thread.sleep(250)}
                    }
                } catch(e:Exception) {
                    synchronized(lock) {segment.put("state","ambiguous").put("error","接口结果无法验证，可能已计费。请确认后重试。");save(job)}
                }
            } finally {piece.delete()}
        }
        synchronized(lock) {
            job.put("complete",segments.all { it.optString("state")=="done" }).put("status",if(segments.all {it.optString("state")=="done"})"completed" else "partial")
            save(job);writeExports(job)
            val unknown=segments.count { it.optString("state")=="ambiguous" }
            if(unknown>0)log("$unknown 个片段结果不明，可能已计费；开启明确重试选项前不会再次发送。")
        }
        globalFailure?.let {throw it}
    }

    private fun payload(job:JSONObject):JSONObject {
        val result=cloneJson(job);val names=result.getJSONObject("speaker_names")
        result.getJSONArray("segments").objects().forEach { it.put("speaker_name",names.optString(it.getString("speaker")).ifBlank {"说话人 ${it.getString("speaker").removePrefix("S")}"}) }
        result.put("audio_url",mediaOrNull(playbackFile(job))).put("source_path",sourceFile(job).absolutePath)
        result.getJSONArray("samples").objects().forEach { sample ->
            val file=File(folder(job),sample.getString("file"));sample.put("audio_url",if(within(folder(job),file)&&file.isFile)mediaUrl(file) else JSONObject.NULL)
        }
        val summary=result.getJSONObject("ai_summary")
        summary.optJSONObject("report")?.let {report->summary.put("stale",report.optString("input_fingerprint")!=MeetingSummary.inputFingerprint(job))}
        val exports=JSONObject();for(format in listOf("txt","md","json","csv","srt"))exports.put(format,JSONObject().put("available",File(folder(job),exportName(format)).isFile))
        val warnings=JSONArray().put("自动人物归属和数字需回听确认；重叠讲话暂不单独分离。")
        if(job.getJSONArray("segments").length()>0 && !normalizedFile(job).isFile)
            warnings.put("用于分段的本地音频缓存已丢失，已暂停片段回听，避免与原始录音的时间轴不一致。录音和现有文字仍保留，不会自动重新转写。")
        result.put("exports",exports).put("warnings",warnings)
        return result
    }

    private fun brief(job:JSONObject):JSONObject = JSONObject().put("id",job.getString("id")).put("title",job.getString("title"))
        .put("duration_seconds",job.getDouble("duration_seconds")).put("speaker_count",job.optInt("speaker_count")).put("updated_at",job.optString("updated_at"))
        .put("status",if(task.optBoolean("running") && selected==job.getString("id"))"processing" else job.optString("status"))
        .put("complete",job.optBoolean("complete")).put("old_version",false)
    private fun current()=selected?.let {jobs[it]}?:throw IllegalArgumentException("请先选择录音。")
    private fun folder(job:JSONObject):File {
        val id=job.getString("id");require(validId(id));val folder=File(jobsRoot,id);require(within(jobsRoot,folder));return folder
    }
    private fun sourceFile(job:JSONObject)=File(folder(job),job.getString("source_file")).also {require(within(folder(job),it))}
    private fun normalizedFile(job:JSONObject)=File(folder(job),"recording-16k.wav")
    // Segment times belong to the decoded PCM timeline. Never silently apply
    // them to a different compressed-source timeline if that cache is missing.
    private fun playbackFile(job:JSONObject):File {
        val normalized=normalizedFile(job)
        return if(job.getJSONArray("segments").length()>0 || normalized.isFile)normalized else sourceFile(job)
    }
    private fun mediaOrNull(file:File):Any=if(file.isFile)mediaUrl(file) else JSONObject.NULL
    private fun save(job:JSONObject) {job.put("updated_at",nowIso());writeJson(File(folder(job),"job.json"),job)}
    private fun validId(id:String)=Regex("[a-f0-9]{32}").matches(id)
    private fun validateLedger(job:JSONObject) {
        require(validId(job.getString("id")))
        require(job.getString("source_file").matches(Regex("source\\.[a-z0-9]{1,8}")))
        require(job.getLong("source_bytes")>0 && job.getString("source_hash").matches(Regex("[a-f0-9]{64}")))
        require(job.getDouble("duration_seconds").let {it.isFinite()&&it>0})
        val segments=job.getJSONArray("segments").objects();val ids=mutableSetOf<String>();var previous=0.0
        for((index,row) in segments.withIndex()) {
            require(row.getInt("index")==index)
            require(row.getString("id").matches(Regex("[a-f0-9]{20}")) && ids.add(row.getString("id")))
            require(row.getString("state") in setOf("pending","requesting","done","failed","ambiguous"))
            require(row.opt("text") is String && row.getString("speaker").matches(Regex("S[0-9]{1,2}")))
            val start=row.getDouble("start");val end=row.getDouble("end")
            require(start.isFinite()&&end.isFinite()&&start>=previous-.002&&end>start&&end-start<=110.01&&end<=job.getDouble("duration_seconds")+.1)
            previous=end
        }
        require(job.getJSONArray("turns").length()==segments.size)
        job.getJSONObject("speaker_names");job.getJSONObject("ai_summary");job.getJSONArray("samples")
    }
    private fun checkCancelled() {if(cancelled.get())throw TaskCancelled()}
    private fun ensureIdle() {check(!task.optBoolean("running")) {"请先停止当前任务。"}}
    private fun changed() {try {onTaskChanged()} catch(_:Exception) {}}
    private fun updateProgress(stage:String,current:Int,total:Int,message:String) = synchronized(lock) {
        task.put("stage",stage).put("current",current).put("total",total).put("message",message);changed()
    }
    private fun log(message:String) {logs.addLast(JSONObject().put("time",nowIso()).put("message",message));while(logs.size>200)logs.removeFirst()}
    private fun updateSettings(value:JSONObject,persist:Boolean) {
        val candidate=cloneJson(settings)
        val allowed=setOf("num_speakers","max_workers","hourly_price","retry_uncertain","api_key_prompt_seen","output_root")
        require(value.keys().asSequence().all {it in allowed}) {"设置包含不支持的字段。"}
        if(value.has("num_speakers")) {val n=value.getInt("num_speakers");require(n in 0..20);candidate.put("num_speakers",n)}
        if(value.has("hourly_price")) {val price=value.getDouble("hourly_price");require(price.isFinite()&&price>0&&price<=1000);candidate.put("hourly_price",price)}
        if(value.has("retry_uncertain")) {require(value.opt("retry_uncertain") is Boolean);candidate.put("retry_uncertain",value.getBoolean("retry_uncertain"))}
        if(value.has("api_key_prompt_seen")) {require(value.opt("api_key_prompt_seen") is Boolean);candidate.put("api_key_prompt_seen",value.getBoolean("api_key_prompt_seen"))}
        settings=candidate.put("max_workers",1).put("output_root",jobsRoot.absolutePath)
        if(persist)writeJson(settingsFile,settings)
    }
    private fun deleteJob(id:String) {
        require(validId(id));val job=jobs[id]?:throw IllegalArgumentException("任务不存在。")
        val folder=folder(job);val files=folder.walkBottomUp().toList()
        require(files.all {it.canonicalPath==folder.canonicalPath || within(folder,it)}) {"任务目录包含不安全的链接，未删除。"}
        val sharedJob=File(sharedExportsRoot,id)
        require(within(cacheRoot,sharedExportsRoot) && sharedExportsRoot.canonicalPath==File(cacheRoot.canonicalFile,"shared_exports").path)
        require(within(sharedExportsRoot,sharedJob) && sharedJob.canonicalPath==File(sharedExportsRoot.canonicalFile,id).path)
        require(!Files.isSymbolicLink(sharedExportsRoot.toPath()) && !Files.isSymbolicLink(sharedJob.toPath())) {"分享缓存包含不安全的链接，未删除任务。"}
        val sharedFiles=mutableListOf<File>()
        fun collectShare(file:File) {
            require(!Files.isSymbolicLink(file.toPath()) && (file.canonicalPath==sharedJob.canonicalPath || within(sharedJob,file))) {"分享缓存包含不安全的链接，未删除任务。"}
            if(file.isDirectory)file.listFiles().orEmpty().forEach(::collectShare)
            sharedFiles.add(file)
        }
        if(sharedJob.exists())collectShare(sharedJob)
        // Clear all immutable snapshots of this job, without touching another
        // job's exports or any files shared outside the application's cache.
        sharedFiles.forEach {check(it.delete() || !it.exists()) {"分享缓存正在使用，请稍后重试。"}}
        files.forEach {check(it.delete() || !it.exists()) {"任务文件正在使用，请稍后重试。"}}
        jobs.remove(id);if(selected==id)selected=jobs.keys.firstOrNull()
    }

    private fun writeExports(job:JSONObject) {
        val rows=job.getJSONArray("segments").objects();if(rows.none {it.optString("state")=="done"})return
        val names=job.getJSONObject("speaker_names");val title=job.getString("title")
        fun label(row:JSONObject)=names.optString(row.getString("speaker")).ifBlank {"说话人 ${row.getString("speaker").removePrefix("S")}"}
        val text=buildString {
            append(title).append('\n').append(if(job.optBoolean("complete"))"完整转写" else "部分转写，未完成片段保留位置").append("\n\n")
            rows.forEach {row->append("[${clock(row.getDouble("start"),true)} → ${clock(row.getDouble("end"),true)}] ${label(row)}\n")
                append(if(row.optString("state")=="done")row.optString("text") else "[未完成：${row.optString("state")}]").append("\n\n")}
        }
        writeText(File(folder(job),exportName("txt")),text)
        writeText(File(folder(job),exportName("md")),"# "+title.replace("#","")+"\n\n"+text.substringAfter('\n'))
        writeJson(File(folder(job),exportName("json")),JSONObject().put("title",title).put("complete",job.optBoolean("complete")).put("speaker_names",names).put("segments",job.getJSONArray("segments")))
        fun csv(value:String):String {
            val safe=if(value.trimStart().firstOrNull() in listOf('=','+','-','@'))"'$value" else value
            return "\""+safe.replace("\"","\"\"")+"\""
        }
        writeText(File(folder(job),exportName("csv")),"\uFEFF"+listOf("开始,结束,人物,文字,状态",*rows.map {row->listOf(clock(row.getDouble("start"),true),clock(row.getDouble("end"),true),label(row),row.optString("text"),row.optString("state")).joinToString(",") {csv(it)}}.toTypedArray()).joinToString("\r\n"))
        writeText(File(folder(job),exportName("srt")),rows.filter {it.optString("state")=="done"}.mapIndexed {i,row->"${i+1}\n${clock(row.getDouble("start"),true).replace('.',',')} --> ${clock(row.getDouble("end"),true).replace('.',',')}\n${label(row)}：${row.optString("text")}\n"}.joinToString("\n"))
    }
    private fun exportName(format:String)=when(format) {"srt"->"片段时间轴_非逐字字幕.srt";"csv"->"分段明细.csv";else->"完整转写.$format"}
}
