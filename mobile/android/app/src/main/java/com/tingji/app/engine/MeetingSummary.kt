package com.tingji.app.engine

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** Hierarchical summaries preserve all source segments and every speaker. */
internal class MeetingSummary(
    private val jobDir:File, private val client:DeepSeekClient, private val secret:String,
    private val cancelled:()->Boolean, private val progress:(Int,Int,String)->Unit,
    private val retryFailed:Boolean
) {
    private data class Node(val content:JSONObject,val roster:LinkedHashMap<String,String>)

    fun generate(job:JSONObject,allowPartial:Boolean):JSONObject {
        val all=job.getJSONArray("segments").objects()
        val done=all.filter { it.optString("state")=="done" }
        require(done.any { it.optString("text").isNotBlank() }) { "请先完成文字转写。" }
        require(allowPartial || done.size==all.size) { "还有片段未转写，请明确选择仅总结已完成部分。" }
        val names=job.optJSONObject("speaker_names")?:JSONObject()
        val chunks=mutableListOf<List<JSONObject>>();var current=mutableListOf<JSONObject>();var size=0
        for(segment in done) {
            require(segment.optString("text").length<=24000) { "单段文字过长，请校对后再总结。" }
            val line=materialLine(segment,names)
            // A 110-second transcript normally stays well below this bound.
            // Never truncate a long segment: use a dedicated source chunk.
            if(current.isNotEmpty() && size+line.length>9000) { chunks.add(current);current=mutableListOf();size=0 }
            current.add(segment);size+=line.length
        }
        if(current.isNotEmpty()) chunks.add(current)
        val cache=File(jobDir,"summary-cache");cache.mkdirs()
        var nodes=chunks.mapIndexed { i,segments ->
            checkCancelled();progress(i,chunks.size,"正在整理第 ${i+1}/${chunks.size} 部分")
            val roster=roster(segments,names)
            Node(node(segments.joinToString("\n") { materialLine(it,names) },roster,false,cache),roster)
        }
        var level=0
        while(nodes.size>1) {
            checkCancelled();level++
            val pairs=nodes.chunked(2)
            nodes=pairs.mapIndexed { i,pair ->
                if(pair.size==1) pair.first() else {
                    val people=linkedMapOf<String,String>();pair.forEach { people.putAll(it.roster) }
                    progress(i,pairs.size,"正在合并第 $level 轮小结 ${i+1}/${pairs.size}")
                    Node(node(JSONArray(pair.map { it.content }).toString(),people,true,cache),people)
                }
            }
        }
        progress(1,1,"总结已完成，正在保存")
        val signature=inputFingerprint(job)
        return JSONObject().put("schema_version",1).put("model","deepseek-flash").put("created_at",nowIso())
            .put("input_fingerprint",signature).put("coverage",JSONObject().put("done_count",done.size).put("total_count",all.size).put("full",done.size==all.size))
            .put("content",nodes.single().content)
    }

    private fun node(material:String,roster:LinkedHashMap<String,String>,merge:Boolean,cache:File):JSONObject {
        require(material.length<=40000) { "小结合并内容超过安全限制，请保留任务并联系维护者。" }
        val digest=sha256("android-summary-v1|deepseek-flash|$merge|${JSONObject(roster as Map<*,*>)}|$material")
        val file=File(cache,"$digest.json")
        val previous=readJson(file)
        previous?.optJSONObject("candidate")?.let { candidate ->
            try { return DeepSeekClient.validateSummary(candidate,roster) } catch(_:Exception) { }
        }
        if(previous?.optString("state") in listOf("requesting","ambiguous","invalid") && !retryFailed)
            throw IllegalStateException("上次某部分总结结果不明或格式异常，可能已计费。已保留原有总结；确认后开启“重试结果不明部分”再生成。")
        checkCancelled()
        val record=JSONObject().put("state","requesting").put("created_at",nowIso()).put("input_hash",digest)
        writeJson(file,record)
        try {
            val candidate=client.summarize(material,roster,merge)
            // Save completed candidate before validation: a future parser fix
            // can recover this response offline without paying again.
            record.put("state","invalid").put("candidate",candidate)
            writeJson(file,record)
            val result=DeepSeekClient.validateSummary(candidate,roster)
            record.put("state","done");writeJson(file,record)
            return result
        } catch(e:TaskCancelled) {
            record.put("state","pending");writeJson(file,record);throw e
        } catch(e:DeepSeekFailure) {
            record.put("state",if(e.ambiguous)"ambiguous" else "failed").put("error",e.message?.replace(secret,"[已隐藏]"))
            writeJson(file,record);throw e
        } catch(e:Exception) {
            record.put("state","invalid").put("error","返回结构验证失败；原有总结保留")
            writeJson(file,record)
            if(e is TaskCancelled)throw e
            throw IllegalStateException("AI 总结结构未通过验证，已保留原有总结；可先重新生成离线复核。")
        }
    }

    private fun checkCancelled() { if(cancelled())throw TaskCancelled() }
    companion object {
        fun inputFingerprint(job:JSONObject):String = sha256((job.optJSONArray("segments")?:JSONArray()).toString()+"|"+(job.optJSONObject("speaker_names")?:JSONObject()).toString())
        private fun roster(segments:List<JSONObject>,names:JSONObject):LinkedHashMap<String,String> {
            val result=linkedMapOf<String,String>()
            segments.map { it.getString("speaker") }.distinct().sortedBy { it.removePrefix("S").toIntOrNull()?:999 }.forEach { id ->
                require(Regex("S[0-9]{1,2}").matches(id));result[id]=names.optString(id).ifBlank { "说话人 ${id.removePrefix("S")}" }
            }
            require(result.size in 1..20)
            return result
        }
        private fun materialLine(segment:JSONObject,names:JSONObject):String {
            val speaker=segment.getString("speaker");val label=names.optString(speaker).ifBlank { "说话人 ${speaker.removePrefix("S")}" }
            return "[${clock(segment.getDouble("start"))} → ${clock(segment.getDouble("end"))}] $speaker $label：${segment.optString("text")}"
        }

        fun markdown(title:String,report:JSONObject):String {
            fun clean(s:String)=s.replace(Regex("[\\r\\n]+")," ").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace(Regex("([\\\\`*_{}\\[\\]#!|])"),"\\\\$1")
            val c=report.getJSONObject("content");val coverage=report.getJSONObject("coverage")
            val lines=mutableListOf("# ${clean(title)} · AI 总结","",if(coverage.getBoolean("full"))"范围：全部已完成转写。" else "范围：仅覆盖 ${coverage.getInt("done_count")} / ${coverage.getInt("total_count")} 个片段。","","AI 根据转写整理，人物、数字和结论请回听确认。","","## 内容概览","",clean(c.getString("overview")),"","## 按人物总结","")
            c.getJSONArray("speaker_summaries").objects().forEach { p ->
                lines.addAll(listOf("### ${clean(p.getString("speaker_name"))}（${p.getString("speaker")}）","",clean(p.getString("summary")),""))
                if(p.getJSONArray("points").length()>0) {lines.add("发言要点：");p.getJSONArray("points").strings().forEach { lines.add("- "+clean(it)) }}
                if(p.getJSONArray("commitments").length()>0) {lines.add("明确承诺：");p.getJSONArray("commitments").strings().forEach { lines.add("- "+clean(it)) }}
                lines.addAll(listOf("录音依据："+clean(p.getString("evidence")),""))
            }
            lines.addAll(listOf("## 讨论要点",""))
            c.getJSONArray("topics").objects().forEach { lines.add("- **${clean(it.getString("title"))}**：${clean(it.getString("summary"))}") }
            for((key,heading) in listOf("decisions" to "已明确的决定","open_questions" to "待确认问题")) {
                lines.addAll(listOf("","## $heading","")); val items=c.getJSONArray(key).strings()
                if(items.isEmpty())lines.add("暂无明确内容。") else items.forEach { lines.add("- "+clean(it)) }
            }
            lines.addAll(listOf("","## 待办事项",""))
            val todos=c.getJSONArray("todos").objects()
            if(todos.isEmpty()) lines.add("录音中暂无明确待办事项。")
            todos.forEachIndexed { i,todo -> lines.addAll(listOf("${i+1}. ${clean(todo.getString("task"))}","   - 负责人：${clean(todo.getString("owner"))}","   - 期限：${clean(todo.getString("deadline"))}","   - 依据：${clean(todo.getString("evidence"))}","")) }
            return lines.joinToString("\n").trim()+"\n"
        }
    }
}
