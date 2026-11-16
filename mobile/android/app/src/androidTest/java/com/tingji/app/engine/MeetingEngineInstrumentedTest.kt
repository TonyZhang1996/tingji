package com.tingji.app.engine

import android.content.Context
import android.content.ContextWrapper
import android.util.AtomicFile
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.net.HttpURLConnection
import java.net.URL
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicInteger

@RunWith(AndroidJUnit4::class)
class MeetingEngineInstrumentedTest {
    private lateinit var testDir:File
    private lateinit var context:Context
    private var engine:MeetingEngine?=null
    private val key="fixture-memory-key-not-a-real-credential"
    private val asrCalls=AtomicInteger();private val summaryCalls=AtomicInteger()
    private var statuses=mutableListOf<Int>()
    private var longText=false
    private var malformedSummary=false
    private val requests=mutableListOf<Pair<String,ByteArray>>()
    private val analyzer=object:SpeakerAnalyzer {
        override fun analyze(wavFile:File,workDir:File,numSpeakers:Int,cancelled:()->Boolean,onProgress:(Int,Int,String)->Unit):List<SpeakerTurn> {
            assertEquals(16000,AudioTools.wavInfo(wavFile).sampleRate)
            return listOf(SpeakerTurn(0.0,2.0,"S1"),SpeakerTurn(2.0,4.0,"S2"),SpeakerTurn(4.0,6.0,"S1"))
        }
    }
    @Before fun prepare() {
        val base=InstrumentationRegistry.getInstrumentation().targetContext
        testDir=File(base.cacheDir,"engine-test-${randomId()}");testDir.mkdirs()
        context=object:ContextWrapper(base) {override fun getFilesDir()=testDir;override fun getCacheDir()=File(testDir,"cache").also{it.mkdirs()}}
        engine=newEngine()
    }
    @After fun cleanup() {engine?.close();testDir.deleteRecursively()}
    private fun newEngine()=MeetingEngine(context,{"https://appassets.androidplatform.net/media/${it.name}"},{},analyzer) { secret,cancelled ->
        DeepSeekClient(secret,cancelled) { endpoint ->
            assertTrue(endpoint.startsWith("https://api.deepseek.com/"))
            FakeConnection(endpoint) {body-> respond(endpoint,body)}
        }
    }
    private fun respond(endpoint:String,body:ByteArray):Pair<Int,ByteArray> {
        synchronized(requests){requests.add(endpoint to body)}
        if(endpoint.endsWith("/asr")) {
            val call=asrCalls.incrementAndGet()
            assertTrue(body.toString(Charsets.ISO_8859_1).contains("name=\"file\""))
            assertTrue(body.toString(Charsets.ISO_8859_1).contains("Content-Type: audio/wav"))
            val status=synchronized(statuses){if(statuses.isNotEmpty())statuses.removeAt(0) else 200}
            if(status<0)throw IOException("Synthetic connection interrupted after upload")
            val text=if(longText)("发言${call}明确讨论测试安排与验收要求。".repeat(220)) else "第${call}段发言，明天完成测试并提交验收记录。"
            return status to JSONObject().put("text",text).put("request_id","fixture-$call").toString().toByteArray()
        }
        summaryCalls.incrementAndGet()
        val request=JSONObject(body.toString(Charsets.UTF_8))
        assertEquals("json_schema",request.getJSONObject("text").getJSONObject("format").getString("type"))
        assertFalse(request.getBoolean("store"))
        val schema=request.getJSONObject("text").getJSONObject("format").getJSONObject("schema")
        val roster=schema.getJSONObject("properties").getJSONObject("speaker_summaries").getJSONObject("properties").keys().asSequence().toList()
        val people=JSONObject()
        roster.forEach { id->people.put(id,JSONObject().put("summary","$id 讨论了发布安排").put("points",JSONArray().put("完成测试并确认结果"))
            .put("commitments",JSONArray()).put("evidence","00:00:01")) }
        if(malformedSummary && roster.size>1)people.remove(roster.last())
        val content=JSONObject().put("overview","会议讨论了测试、验收和后续安排。").put("topics",JSONArray()).put("decisions",JSONArray()).put("open_questions",JSONArray())
            .put("todos",JSONArray().put(JSONObject().put("task","完成测试").put("owner","未明确").put("deadline","明天").put("evidence","00:00:01"))).put("speaker_summaries",people)
        val response=JSONObject().put("status","completed").put("error",JSONObject.NULL).put("incomplete_details",JSONObject.NULL)
            .put("output",JSONArray().put(JSONObject().put("type","message").put("role","assistant").put("status","completed")
                .put("content",JSONArray().put(JSONObject().put("type","output_text").put("text",content.toString())))))
        return 200 to response.toString().toByteArray()
    }
    private fun importAndAnalyze():String {
        val imports=File(testDir,"imports");imports.mkdirs();val wav=File(imports,"fixture.wav")
        wav(wav,6)
        val state=engine!!.acceptAudio(wav,"合成测试会议.wav");val id=state.getJSONObject("selected_job").getString("id")
        engine!!.dispatch("config",JSONObject().put("num_speakers",2));engine!!.dispatch("analyze",JSONObject());idle()
        assertEquals(2,engine!!.state().getJSONObject("selected_job").getInt("speaker_count"))
        return id
    }
    private fun idle():JSONObject {
        val deadline=System.currentTimeMillis()+20000
        while(System.currentTimeMillis()<deadline) {val state=engine!!.state();if(!state.getJSONObject("task").getBoolean("running"))return state;Thread.sleep(20)}
        fail("Synthetic engine task did not complete");throw AssertionError()
    }
    private fun transcribe(retry:Boolean=false):JSONObject {
        engine!!.dispatch("transcribe",JSONObject().put("api_key",key).put("retry_uncertain",retry));return idle()
    }
    private fun summarize(retry:Boolean=false):JSONObject {
        engine!!.dispatch("summarize",JSONObject().put("api_key",key).put("allow_partial",false).put("retry_failed",retry));return idle()
    }
    private fun wav(file:File,seconds:Int) {
        val bytes=seconds*32000;val b=ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN)
        b.put("RIFF".toByteArray()).putInt(bytes+36).put("WAVEfmt ".toByteArray()).putInt(16).putShort(1).putShort(1)
            .putInt(16000).putInt(32000).putShort(2).putShort(16).put("data".toByteArray()).putInt(bytes)
        RandomAccessFile(file,"rw").use {it.write(b.array());it.setLength(bytes+44L)}
    }
    private fun selected(state:JSONObject)=state.getJSONObject("selected_job")

    @Test fun bridgeStateActionMatchesDirectControllerState() {
        assertEquals(engine!!.state().toString(),engine!!.dispatch("state",JSONObject()).toString())
        importAndAnalyze()
        assertEquals(engine!!.state().toString(),engine!!.dispatch("state",JSONObject()).toString())
    }
    @Test fun deletingHistoryOnlyRemovesThatJobsShareSnapshots() {
        val firstId=importAndAnalyze()
        val secondSource=File(testDir,"imports/second.wav");wav(secondSource,6)
        val secondId=engine!!.acceptAudio(secondSource,"另一个合成任务.wav").getJSONObject("selected_job").getString("id")
        assertNotEquals(firstId,secondId)
        val shares=File(context.cacheDir,"shared_exports")
        val firstSnapshot=File(shares,"$firstId/${randomId()}/完整转写.txt")
        val olderSnapshot=File(shares,"$firstId/${randomId()}/完整转写.txt")
        val otherSnapshot=File(shares,"$secondId/${randomId()}/完整转写.txt")
        for(file in listOf(firstSnapshot,olderSnapshot,otherSnapshot)) {file.parentFile!!.mkdirs();file.writeText("仅用于删除范围验收的合成内容")}
        val unrelated=File(context.cacheDir,"unrelated-cache.txt");unrelated.writeText("保留")
        engine!!.dispatch("delete_job",JSONObject().put("job_id",firstId))
        assertFalse(File(testDir,"tingji/jobs/$firstId").exists())
        assertFalse(File(shares,firstId).exists())
        assertTrue(File(testDir,"tingji/jobs/$secondId/source.wav").isFile)
        assertTrue(otherSnapshot.isFile);assertTrue(unrelated.isFile)
        assertEquals(1,engine!!.state().getJSONArray("jobs").length())
        assertEquals(secondId,selected(engine!!.state()).getString("id"))
        try {
            engine!!.dispatch("delete_job",JSONObject().put("job_id","../$secondId"))
            fail("A path traversal must never delete another task's data or exports")
        } catch(_:IllegalArgumentException){}
        assertTrue(otherSnapshot.isFile);assertTrue(File(testDir,"tingji/jobs/$secondId/source.wav").isFile)
    }

    @Test fun completeWorkflowKeepsBothSpeakersExportsAndNoStoredKey() {
        importAndAnalyze()
        assertTrue(selected(transcribe()).getBoolean("complete"));assertEquals(3,asrCalls.get())
        assertTrue(selected(transcribe()).getBoolean("complete"));assertEquals(3,asrCalls.get())
        val report=selected(summarize()).getJSONObject("ai_summary")
        assertEquals("complete",report.getString("status"));assertEquals(2,report.getJSONObject("report").getJSONObject("content").getJSONArray("speaker_summaries").length())
        for(format in listOf("txt","md","json","csv","srt"))assertTrue(engine!!.exportFile(format)!!.length()>0)
        assertTrue(engine!!.summaryFile()!!.readText().contains("## 待办事项"))
        val calls=summaryCalls.get();assertEquals("complete",selected(summarize()).getJSONObject("ai_summary").getString("status"));assertEquals(calls,summaryCalls.get())
        assertFalse(engine!!.state().toString().contains(key))
        testDir.walkTopDown().filter{it.isFile}.forEach{assertFalse("Credential persisted in ${it.name}",it.readBytes().toString(Charsets.ISO_8859_1).contains(key))}
    }
    @Test fun interruptedAtomicSaveRecoversLastCompleteRecord() {
        val file=File(testDir,"atomic-ledger.json")
        writeJson(file,JSONObject().put("state","done").put("text","上次成功结果"))
        val stream=AtomicFile(file).startWrite()
        stream.write("{\"state\":".toByteArray());stream.close()
        val restored=readJson(file)!!
        assertEquals("done",restored.getString("state"));assertEquals("上次成功结果",restored.getString("text"))
    }
    @Test fun restartQuarantinesRequestingAndOnlyExplicitRetryResendsIt() {
        val id=importAndAnalyze();engine!!.close()
        val file=File(testDir,"tingji/jobs/$id/job.json");val job=readJson(file)!!
        job.getJSONArray("segments").getJSONObject(0).put("state","requesting");writeJson(file,job)
        engine=newEngine()
        val restored=selected(engine!!.state()).getJSONArray("segments");assertEquals("ambiguous",restored.getJSONObject(0).getString("state"))
        assertFalse(selected(transcribe()).getBoolean("complete"));assertEquals(2,asrCalls.get())
        assertTrue(selected(transcribe(true)).getBoolean("complete"));assertEquals(3,asrCalls.get())
    }
    @Test fun perSegmentRejectionContinuesOtherUnsentSegmentsButUnauthorizedStops() {
        importAndAnalyze();statuses.add(400)
        assertFalse(selected(transcribe()).getBoolean("complete"));assertEquals(3,asrCalls.get())
        assertTrue(selected(transcribe()).getBoolean("complete"));assertEquals(4,asrCalls.get())
        val imports=File(testDir,"imports");val second=File(imports,"second.wav");wav(second,6)
        engine!!.acceptAudio(second,"第二个合成测试.wav");engine!!.dispatch("analyze",JSONObject());idle()
        statuses.add(401);val count=asrCalls.get();val stopped=transcribe()
        assertEquals("error",stopped.getJSONObject("task").getString("stage"));assertEquals(count+1,asrCalls.get())
    }
    @Test fun interruptedNetworkIsNotSilentlyResubmitted() {
        importAndAnalyze();statuses.add(-1)
        val first=transcribe();assertFalse(selected(first).getBoolean("complete"));assertEquals(3,asrCalls.get())
        assertEquals("ambiguous",selected(first).getJSONArray("segments").getJSONObject(0).getString("state"))
        transcribe();assertEquals(3,asrCalls.get())
        assertTrue(selected(transcribe(true)).getBoolean("complete"));assertEquals(4,asrCalls.get())
    }
    @Test fun changedSourceOrDamagedLedgerCannotTriggerNewPaidRequests() {
        val id=importAndAnalyze();val folder=File(testDir,"tingji/jobs/$id")
        val job=readJson(File(folder,"job.json"))!!
        RandomAccessFile(File(folder,job.getString("source_file")),"rw").use{it.seek(100);it.write(1)}
        assertEquals("error",transcribe().getJSONObject("task").getString("stage"))
        assertEquals(0,asrCalls.get())
        engine!!.close();job.getJSONArray("segments").getJSONObject(0).put("state","done").put("text",42);writeJson(File(folder,"job.json"),job)
        engine=newEngine();assertTrue(engine!!.state().isNull("selected_job"));assertTrue(File(folder,"source.wav").isFile)
    }
    @Test fun longSummaryMergesAllChunksAndInvalidOutputPreservesOriginalReport() {
        importAndAnalyze();longText=true;transcribe()
        val first=selected(summarize()).getJSONObject("ai_summary");assertEquals("complete",first.getString("status"));assertTrue(summaryCalls.get()>=3)
        val oldReport=first.getJSONObject("report").toString()
        engine!!.dispatch("speaker_names",JSONObject().put("names",JSONObject().put("S1","甲").put("S2","乙")))
        malformedSummary=true
        val failed=selected(summarize()).getJSONObject("ai_summary");assertEquals("error",failed.getString("status"));assertEquals(oldReport,failed.getJSONObject("report").toString())
        val calls=summaryCalls.get();summarize();assertEquals(calls,summaryCalls.get())
        malformedSummary=false;val recovered=selected(summarize(true)).getJSONObject("ai_summary")
        assertEquals("complete",recovered.getString("status"));assertEquals(2,recovered.getJSONObject("report").getJSONObject("content").getJSONArray("speaker_summaries").length())
    }

    private class FakeConnection(endpoint:String,private val handler:(ByteArray)->Pair<Int,ByteArray>):HttpURLConnection(URL(endpoint)) {
        private val bytes=ByteArrayOutputStream();private var result:Pair<Int,ByteArray>?=null
        override fun disconnect(){}
        override fun usingProxy()=false
        override fun connect(){connected=true}
        override fun getOutputStream()=bytes
        private fun result()=result?:handler(bytes.toByteArray()).also{result=it}
        override fun getResponseCode()=result().first
        override fun getInputStream()=ByteArrayInputStream(result().second)
    }
}
