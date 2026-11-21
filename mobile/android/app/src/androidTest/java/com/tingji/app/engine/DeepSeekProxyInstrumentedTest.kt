package com.tingji.app.engine

import android.content.ContextWrapper
import android.os.Bundle
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.tingji.app.SherpaSpeakerAnalyzer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger

/** Opt-in only: the host proxy owns the real API key; the device never receives it. */
@RunWith(AndroidJUnit4::class)
class DeepSeekProxyInstrumentedTest {
    @Test fun actualHttpSerializerThroughHostControlledProxy() {
        val instrumentation=InstrumentationRegistry.getInstrumentation()
        val base=InstrumentationRegistry.getArguments().getString("proxy_base")?:""
        assumeTrue("Explicit host-controlled QA proxy required",base.matches(Regex("http://127\\.0\\.0\\.1:[0-9]{4,5}")))
        val source=File(instrumentation.targetContext.filesDir,"qa/input.wav")
        assertTrue("A synthetic QA recording must exist before explicit proxy verification",source.isFile)
        val info=AudioTools.wavInfo(source)
        assertTrue(info.duration in 10.0..110.0 && info.channels==1 && info.sampleRate==16000)
        val sandbox=File(instrumentation.targetContext.filesDir,"qa/proxy-pipeline-${UUID.randomUUID()}")
        assertTrue(sandbox.mkdirs())
        val context=object:ContextWrapper(instrumentation.targetContext) {
            override fun getFilesDir()=sandbox
            override fun getCacheDir()=File(sandbox,"cache").also{it.mkdirs()}
            override fun getNoBackupFilesDir()=File(sandbox,"no-backup").also{it.mkdirs()}
        }
        val asrRequests=AtomicInteger()
        val summaryRequests=AtomicInteger()
        val placeholder="qa-proxy-placeholder"
        val result=JSONObject().put("status","RUNNING").put("fixture_seconds",info.duration)
        var phase="startup"
        val engine=MeetingEngine(context,{file->
            assertTrue(file.canonicalPath.startsWith(sandbox.canonicalPath+File.separator))
            "https://appassets.androidplatform.net/media/qa-${sha256(file.name).take(20)}"
        },{},SherpaSpeakerAnalyzer(context)) {key,cancelled->
            assertEquals(placeholder,key)
            DeepSeekClient(key,cancelled) {endpoint->
                // Only the test supplies this factory. Production URLs and
                // payload construction still run through DeepSeekClient.
                assertTrue(endpoint.startsWith("https://api.deepseek.com/"))
                val path=if(endpoint.endsWith("/asr")) {
                    asrRequests.incrementAndGet();"/asr"
                } else {
                    assertEquals("https://api.deepseek.com/responses",endpoint)
                    summaryRequests.incrementAndGet();"/responses"
                }
                URL(base+path).openConnection() as HttpURLConnection
            }
        }
        fun announce(message:String) {
            instrumentation.sendStatus(0,Bundle().apply{putString("stream","\nPASS $message\n")})
        }
        fun waitIdle(timeoutSeconds:Int):JSONObject {
            val deadline=System.nanoTime()+timeoutSeconds*1_000_000_000L
            while(System.nanoTime()<deadline) {
                val state=engine.state()
                val task=state.getJSONObject("task")
                if(!task.getBoolean("running")) {
                    val stage=task.getString("stage")
                    if(stage in setOf("error","partial","cancelled")) {
                        val status=Regex("HTTP ([0-9]{3})").find(task.optString("message"))?.groupValues?.get(1)
                        if(status!=null)result.put("http_status",status.toInt())
                        throw AssertionError("$phase did not complete: $stage")
                    }
                    return state
                }
                Thread.sleep(200)
            }
            throw AssertionError("$phase timed out; saved request ledgers must be inspected before any rerun")
        }
        try {
            phase="import"
            engine.acceptAudio(source,"合成双人会议.wav")
            engine.dispatch("config",JSONObject().put("num_speakers",2).put("max_workers",1))
            phase="analysis"
            engine.dispatch("analyze",JSONObject())
            val analyzed=waitIdle(240).getJSONObject("selected_job")
            val turns=analyzed.getJSONArray("turns").objects()
            val people=turns.map{it.getString("speaker")}.toSet()
            assertEquals(2,analyzed.getInt("speaker_count"));assertEquals(2,people.size)
            assertTrue(turns.isNotEmpty());assertTrue(analyzed.getJSONArray("samples").length()>=2)
            result.put("speakers",people.size).put("segments",turns.size)
            announce("native_silero_campplus speakers=${people.size} segments=${turns.size}")

            phase="transcription"
            engine.dispatch("transcribe",JSONObject().put("api_key",placeholder).put("retry_uncertain",false))
            val transcribed=waitIdle(600).getJSONObject("selected_job")
            val segments=transcribed.getJSONArray("segments").objects()
            assertTrue(transcribed.getBoolean("complete"))
            assertTrue(segments.all{it.getString("state")=="done"})
            assertEquals(people,segments.filter{it.optString("text").isNotBlank()}.map{it.getString("speaker")}.toSet())
            assertTrue(segments.sumOf{it.optString("text").length}>40)
            val asrCount=asrRequests.get()
            assertTrue(asrCount>0)
            // An explicit second run must reuse all successful ASR segments.
            engine.dispatch("transcribe",JSONObject().put("api_key",placeholder))
            assertTrue(waitIdle(60).getJSONObject("selected_job").getBoolean("complete"))
            assertEquals(asrCount,asrRequests.get())
            for(format in listOf("txt","md","json","csv","srt")) {
                val file=engine.exportFile(format)
                assertNotNull(file);assertTrue(file!!.length()>0)
                assertTrue(file.canonicalPath.startsWith(sandbox.canonicalPath+File.separator))
            }
            result.put("asr_requests",asrCount).put("transcript_exports",5).put("asr_reused",true)
            announce("paid_asr speakers=${people.size} exports=5 successful_segments_reused=true")

            val names=JSONObject()
            people.sorted().forEachIndexed{i,id->names.put(id,"合成发言人 ${i+1}")}
            engine.dispatch("speaker_names",JSONObject().put("names",names))
            phase="summary"
            engine.dispatch("summarize",JSONObject().put("api_key",placeholder).put("allow_partial",false).put("retry_failed",false))
            val summary=waitIdle(300).getJSONObject("selected_job").getJSONObject("ai_summary")
            assertEquals("complete",summary.getString("status"))
            val report=summary.getJSONObject("report")
            assertTrue(report.getJSONObject("coverage").getBoolean("full"))
            val content=report.getJSONObject("content")
            assertTrue(content.getString("overview").isNotBlank())
            val perPerson=content.getJSONArray("speaker_summaries").objects()
            assertEquals(people,perPerson.map{it.getString("speaker")}.toSet())
            assertTrue(perPerson.all{it.getString("summary").isNotBlank()&&it.getString("evidence").isNotBlank()})
            val todos=content.getJSONArray("todos").objects()
            assertTrue("Synthetic fixture must contain at least two explicit actions",todos.size>=2)
            assertTrue(todos.all{it.getString("task").isNotBlank()&&it.getString("evidence").isNotBlank()})
            val summaryFile=engine.summaryFile()
            assertNotNull(summaryFile)
            val markdown=summaryFile!!.readText()
            assertTrue(markdown.contains("## 按人物总结"))
            assertEquals("待办事项",Regex("(?m)^## (.+)$").findAll(markdown).last().groupValues[1])
            val summaryCount=summaryRequests.get()
            assertTrue(summaryCount>0)
            engine.dispatch("summarize",JSONObject().put("api_key",placeholder).put("allow_partial",false).put("retry_failed",false))
            assertEquals("complete",waitIdle(60).getJSONObject("selected_job").getJSONObject("ai_summary").getString("status"))
            assertEquals(summaryCount,summaryRequests.get())
            result.put("summary_requests",summaryCount).put("person_summaries",perPerson.size).put("todos",todos.size).put("summary_cache_reused",true)
            announce("paid_summary persons=${perPerson.size} todos=${todos.size} completed_candidates_reused=true")

            phase="privacy"
            assertFalse(engine.state().toString().contains(placeholder))
            var files=0
            sandbox.walkTopDown().filter{it.isFile}.forEach {file->
                assertTrue(file.canonicalPath.startsWith(sandbox.canonicalPath+File.separator))
                file.inputStream().use {input->
                    val needle=placeholder.toByteArray();val utf16=placeholder.toByteArray(Charsets.UTF_16LE)
                    val buffer=ByteArray(65536);var tail=ByteArray(0)
                    while(true) {
                        val n=input.read(buffer);if(n<0)break
                        val bytes=tail+buffer.copyOf(n)
                        val latin=bytes.toString(Charsets.ISO_8859_1)
                        assertFalse("QA authentication placeholder must remain memory-only",latin.contains(needle.toString(Charsets.ISO_8859_1))||latin.contains(utf16.toString(Charsets.ISO_8859_1)))
                        tail=bytes.takeLast(256).toByteArray()
                    }
                }
                files++
            }
            result.put("scanned_files",files).put("status","PASS")
            announce("private_sandbox credential_not_persisted files=$files")
        } catch(e:Throwable) {
            // Never emit transcripts, response bodies, request headers or keys.
            result.put("status","FAIL").put("phase",phase).put("error_type",e.javaClass.simpleName)
            throw e
        } finally {
            engine.cancel();engine.close()
            writeJson(File(sandbox,"qa-result.json"),result)
            instrumentation.sendStatus(0,Bundle().apply{putString("stream","\nTingji full pipeline QA status=${result.optString("status")}\n")})
        }
    }
}
