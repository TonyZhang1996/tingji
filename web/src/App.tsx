import { useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { AudioLines, ArrowDownToLine, ArrowRight, Check, CheckCheck, CheckCircle2, ChevronDown, Clock3, Copy, FileAudio2, FileText, Folder, Headphones, ListFilter, Loader2, Menu, Pause, PencilLine, Play, Plus, Search, Settings2, ShieldCheck, SkipBack, SkipForward, SlidersHorizontal, Square, UsersRound, Volume2, X, Eye, EyeOff, AlertCircle, PanelRightClose, PanelRightOpen, Trash2, Maximize2, Minimize2 } from 'lucide-react'
import { Toaster, toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Progress } from '@/components/ui/progress'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Checkbox } from '@/components/ui/checkbox'
import { Textarea } from '@/components/ui/textarea'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { api, clock, speakerLabel, type AppState, type Job, type Segment, type Settings } from '@/lib/api'
import { cn } from '@/lib/utils'
import { SummaryPanel } from '@/components/summary-panel'
import { copyToClipboard, isAndroidApp } from '@/lib/android-bridge'
import { AndroidWelcome } from '@/components/android-welcome'

const INITIAL:AppState={version:'2.2',source:null,settings:{num_speakers:2,output_root:'',hourly_price:.39,max_workers:2,retry_uncertain:false},jobs:[],selected_job:null,task:{running:false,stage:'',current:0,total:0,message:''},logs:[]}
type TranscriptPosition={top:number;anchorId?:string;inset:number}
type TranscriptController={capture:()=>TranscriptPosition}
type TranscriptRestore={serial:number;position:TranscriptPosition|null}
const COLORS=['blue','teal','violet','amber','rose','cyan']
const colorFor=(speaker:string)=>COLORS[(Math.max(1,parseInt(speaker.replace(/\D/g,''))||1)-1)%COLORS.length]
const statusName=(job:Job)=>job.old_version?'旧版任务':job.complete||job.status==='complete'?'已完成':job.status==='running'||job.status==='processing'?'处理中':job.status==='analyzed'?'待转写':job.status==='partial'?'未完成':'已保存'
function IconButton({label,children,...props}:React.ComponentProps<typeof Button>&{label:string}) {return <Tooltip><TooltipTrigger asChild><Button size="icon" variant="ghost" aria-label={label} {...props}>{children}</Button></TooltipTrigger><TooltipContent>{label}</TooltipContent></Tooltip>}

export default function App(){
 const android=isAndroidApp()
 const [state,setState]=useState<AppState>(INITIAL)
 const [connected,setConnected]=useState(false)
 const [connectionError,setConnectionError]=useState('')
 const [busy,setBusy]=useState('')
 const [settingsOpen,setSettingsOpen]=useState(false)
 const [startAfterSettings,setStartAfterSettings]=useState(false)
 const [apiKey,setApiKey]=useState('')
 const [welcomeOpen,setWelcomeOpen]=useState(false)
 const [welcomeNeedsMarker,setWelcomeNeedsMarker]=useState(false)
 const welcomeHandled=useRef(false)
 const [readingMode,setReadingMode]=useState(false)
 const [readingPlaying,setReadingPlaying]=useState(false)
 const transcriptController=useRef<TranscriptController|null>(null)
 const normalPosition=useRef<TranscriptPosition|null>(null)
 const restoreSerial=useRef(0)
 const [transcriptRestore,setTranscriptRestore]=useState<TranscriptRestore|null>(null)
 const [query,setQuery]=useState('')
 const [historyQuery,setHistoryQuery]=useState('')
 const [filter,setFilter]=useState('all')
 const [editorView,setEditorView]=useState<'transcript'|'summary'>('transcript')
 const [sidebarOpen,setSidebarOpen]=useState(false)
 const [inspectorOpen,setInspectorOpen]=useState(()=>window.innerWidth>=1090)
 const [logsOpen,setLogsOpen]=useState(false)
 const [editing,setEditing]=useState<Segment|null>(null)
 const [deleting,setDeleting]=useState<Job|null>(null)
 const [draftText,setDraftText]=useState('')
 const [draftSpeaker,setDraftSpeaker]=useState('S1')
 const [names,setNames]=useState<Record<string,string>>({})
 const [samplePlaying,setSamplePlaying]=useState('')
 const [playTime,setPlayTime]=useState(0)
 const [sourceView,setSourceView]=useState(false)
 const audioRef=useRef<HTMLAudioElement>(null)
 const sampleRef=useRef<HTMLAudioElement|null>(null)
 const lastState=useRef('')
 const namesSync=useRef('')
 const actionPending=useRef(false)
 const stateRevision=useRef(0)
 const deleteCancelRef=useRef<HTMLButtonElement>(null)
 const deleteTriggerRef=useRef<HTMLButtonElement|null>(null)
 const historyRef=useRef<HTMLElement>(null)
 const newTaskRef=useRef<HTMLButtonElement>(null)
 const deletedRef=useRef(false)
 const job=sourceView?null:state.selected_job
 const running=state.task.running
 const selectedTitle=job?.title || state.source?.name || '新建转写'
 const segments=job?.segments || []
 const speakerIds=useMemo(()=>Array.from(new Set([...segments.map(s=>s.speaker),...(job?.samples||[]).map(s=>s.speaker)])).sort((a,b)=>(parseInt(a.slice(1))||0)-(parseInt(b.slice(1))||0)),[segments,job?.samples])
 const uncertainCount=segments.filter(s=>s.uncertain&&!s.reviewed).length
 const reviewedCount=segments.filter(s=>s.reviewed).length
 const doneCount=segments.filter(s=>s.state==='done').length
 const duration=job?.duration_seconds || state.source?.duration_seconds || 0
 const audioUrl=job?.audio_url || (state.source as {audio_url?:string}|null)?.audio_url || ''
 const applyState=useCallback((next:AppState)=>{
  const fingerprint=JSON.stringify(next)
  if(fingerprint!==lastState.current){lastState.current=fingerprint;setState(next)}
  setConnected(true);setConnectionError('')
 },[])
 useEffect(()=>{let alive=true;let active=false;async function poll(){if(active||actionPending.current)return;active=true;const revision=stateRevision.current;try{const next=await api('state');if(alive&&revision===stateRevision.current)applyState(next)}catch(error){if(alive&&revision===stateRevision.current)setConnectionError((error as Error).message)}finally{active=false}}void poll();const timer=setInterval(poll,1200);return()=>{alive=false;clearInterval(timer)}},[applyState])
 useEffect(()=>{const token=JSON.stringify([job?.id,job?.speaker_names]);if(token!==namesSync.current){namesSync.current=token;setNames({...job?.speaker_names})}},[job?.id,job?.speaker_names])
 useEffect(()=>{setReadingMode(false);normalPosition.current=null;setTranscriptRestore(null);setQuery('');setFilter('all');setEditorView('transcript');setPlayTime(0);sampleRef.current?.pause();setSamplePlaying('')},[job?.id,audioUrl])
 useEffect(()=>{
  if(android&&connected&&!welcomeHandled.current){welcomeHandled.current=true;if(state.settings.api_key_prompt_seen!==true)setWelcomeOpen(true)}
 },[android,connected,state.settings.api_key_prompt_seen])
 useEffect(()=>{
  const audio=audioRef.current;if(!audio)return
  const update=()=>setReadingPlaying(!audio.paused&&!audio.ended)
  audio.addEventListener('play',update);audio.addEventListener('pause',update);audio.addEventListener('ended',update);update()
  return()=>{audio.removeEventListener('play',update);audio.removeEventListener('pause',update);audio.removeEventListener('ended',update)}
 },[audioUrl])
 function enterReading(){normalPosition.current=transcriptController.current?.capture()||null;setTranscriptRestore({serial:++restoreSerial.current,position:normalPosition.current});setInspectorOpen(false);setSidebarOpen(false);setReadingMode(true)}
 function exitReading(){const position=query||filter!=='all'?normalPosition.current:(transcriptController.current?.capture()||normalPosition.current);setTranscriptRestore({serial:++restoreSerial.current,position});setReadingMode(false)}
 function toggleReadingAudio(){const audio=audioRef.current;if(!audio||!audioUrl)return;if(audio.paused)void audio.play().catch(()=>toast.error('录音暂时无法播放，请稍后重试。'));else audio.pause()}
 useEffect(()=>{
  if(!android)return
  const refresh=()=>{if(!actionPending.current)void api('state').then(applyState).catch(()=>{})}
  window.addEventListener('tingji:refresh',refresh)
  const visibility=()=>{if(document.visibilityState==='visible')refresh()}
  document.addEventListener('visibilitychange',visibility)
  return()=>{window.removeEventListener('tingji:refresh',refresh);document.removeEventListener('visibilitychange',visibility)}
 },[android,applyState])
 useEffect(()=>{
  if(!android)return
  window.__TINGJI_ANDROID_BACK__=()=>{
   if(document.querySelector('[data-slot="dialog-content"]')){
    document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',code:'Escape',bubbles:true,cancelable:true}))
    return true
   }
   if(editing){setEditing(null);return true}
   if(deleting&&!busy){setDeleting(null);return true}
   if(settingsOpen){setSettingsOpen(false);setStartAfterSettings(false);return true}
   if(logsOpen){setLogsOpen(false);return true}
   if(readingMode){exitReading();return true}
   if(sidebarOpen){setSidebarOpen(false);return true}
   if(inspectorOpen){setInspectorOpen(false);return true}
   if(editorView==='summary'){setEditorView('transcript');return true}
   return false
  }
  return()=>{delete window.__TINGJI_ANDROID_BACK__}
 },[android,editing,deleting,busy,settingsOpen,logsOpen,sidebarOpen,inspectorOpen,editorView,readingMode,query,filter])
 const action=useCallback(async(path:string,body:unknown={})=>{if(actionPending.current)return null;actionPending.current=true;stateRevision.current++;setBusy(path);try{const next=await api(path,body);applyState(next);return next}catch(error){toast.error((error as Error).message);return null}finally{actionPending.current=false;setBusy('')}},[applyState])
 useEffect(()=>{
  if(!android||!welcomeNeedsMarker||!connected||running||busy)return
  setWelcomeNeedsMarker(false)
  void action('config',{api_key_prompt_seen:true})
 },[android,welcomeNeedsMarker,connected,running,busy,action])
 function finishWelcome(){setWelcomeOpen(false);setWelcomeNeedsMarker(true)}
 async function chooseAudio(){const next=await action('choose_audio');if(next?.source){setSourceView(false);setSidebarOpen(false)}}
 async function analyze(){setSourceView(false);await action('analyze')}
 async function transcribe(){if(!apiKey.trim()){setStartAfterSettings(true);setSettingsOpen(true);return}setSourceView(false);await action('transcribe',{api_key:apiKey.trim(),retry_uncertain:state.settings.retry_uncertain})}
 async function selectJob(id:string){const next=await action('select_job',{id});if(next){setSourceView(false);setSidebarOpen(false)}}
 async function deleteJob(){
  if(!deleting||running||actionPending.current)return
  const current=deleting.id===state.selected_job?.id
  if(current){audioRef.current?.pause();audioRef.current?.removeAttribute('src');audioRef.current?.load();sampleRef.current?.pause();if(sampleRef.current){sampleRef.current.removeAttribute('src');sampleRef.current.load();sampleRef.current=null}setSamplePlaying('')}
  const next=await action('delete_job',{job_id:deleting.id})
  if(current&&audioRef.current){const url=next?(next.selected_job?.audio_url||next.source?.audio_url||''):audioUrl;if(url)audioRef.current.src=url;else audioRef.current.removeAttribute('src');audioRef.current.load()}
  if(next){if(current){setSourceView(false);setEditing(null)}deletedRef.current=true;setDeleting(null);toast.success('任务已删除，原始录音已保留')}
 }
 function playAt(time:number){sampleRef.current?.pause();setSamplePlaying('');const audio=audioRef.current;if(!audio)return;audio.currentTime=time;void audio.play().catch(()=>toast.error('音频暂时无法播放，请确认文件仍在原位置。'))}
 function playSample(speaker:string,url:string){audioRef.current?.pause();if(samplePlaying===speaker){sampleRef.current?.pause();setSamplePlaying('');return}sampleRef.current?.pause();const audio=new Audio(url);audio.volume=audioRef.current?.volume??.8;sampleRef.current=audio;audio.onended=()=>setSamplePlaying('');audio.onerror=()=>{setSamplePlaying('');toast.error('声音样例读取失败，请重新分析人物。')};void audio.play().then(()=>setSamplePlaying(speaker)).catch(()=>toast.error('无法播放声音样例'))}
 function openEdit(segment:Segment){setEditing(segment);setDraftText(segment.text);setDraftSpeaker(segment.speaker)}
 async function saveReview(){if(!editing)return;const next=await action('segment_review',{id:editing.id,text:draftText,speaker:draftSpeaker,reviewed:true});if(next){setEditing(null);toast.success('校对已保存，导出文件同步更新')}}
 async function markReviewed(segment:Segment){await action('segment_review',{id:segment.id,reviewed:!segment.reviewed})}
 async function copyText(){const text=segments.filter(s=>s.state==='done').map(s=>`${speakerLabel(s.speaker,job?.speaker_names)} ${clock(s.start,true)}\n${s.text}`).join('\n\n');try{await copyToClipboard(text);toast.success('已复制分人物文字')}catch{toast.error('复制未完成，请使用导出文件。')}}
 const history=state.jobs.filter(j=>j.title.toLowerCase().includes(historyQuery.toLowerCase()))
 const filtered=useMemo(()=>readingMode?segments:segments.filter(s=>(!query||s.text.toLowerCase().includes(query.toLowerCase())||speakerLabel(s.speaker,job?.speaker_names).includes(query))&&(filter==='all'||filter==='uncertain'&&s.uncertain&&!s.reviewed||filter==='reviewed'&&s.reviewed||filter===s.speaker)),[segments,query,filter,job?.speaker_names,readingMode])
 const analyzed=Boolean(job&&segments.length)
 const sourceAvailable=Boolean(state.source||job)
 return <TooltipProvider delayDuration={350}><div className={cn("workbench",android&&"android-workbench",android&&readingMode&&"android-reading-mode",android&&!sourceAvailable&&"android-empty-view",android&&inspectorOpen&&analyzed&&!running&&"android-people-view",android&&editorView==='summary'&&!inspectorOpen&&!running&&"android-summary-view")}>
  {sidebarOpen&&<button className="mobile-backdrop" aria-label="收起任务导航" onClick={()=>setSidebarOpen(false)}/>}
  <aside className={cn('sidebar',sidebarOpen&&'is-open')} aria-label="录音任务" aria-hidden={android&&!sidebarOpen?true:undefined} inert={android&&!sidebarOpen?true:undefined}>
   <div className="brand"><div className="brand-symbol"><AudioLines size={23} strokeWidth={2.1}/></div><div><strong>听记</strong><span>{android?'手机会议转写':'会议转写工作台'}</span></div>{android&&<IconButton className="history-close" label="关闭任务列表" onClick={()=>setSidebarOpen(false)}><X/></IconButton>}</div>
   <Button ref={newTaskRef} className="new-task" onClick={()=>{setSourceView(true);setSidebarOpen(false);void chooseAudio()}} disabled={!!busy||running}><Plus size={17}/>新建转写</Button>
   <div className="sidebar-search"><Search size={14}/><input aria-label="搜索历史录音" placeholder="搜索录音任务" value={historyQuery} onChange={e=>setHistoryQuery(e.target.value)}/></div>
   <div className="sidebar-section-label"><span>录音任务</span><span>{state.jobs.length}</span></div>
   <nav ref={historyRef} className="history-list" aria-label="录音任务列表">{history.length?history.map(item=><div key={item.id} className="history-row"><button className={cn('history-item',job?.id===item.id&&'is-active')} aria-current={job?.id===item.id?'page':undefined} disabled={running||!!busy} onClick={()=>void selectJob(item.id)}><FileAudio2 className="history-icon" size={17}/><div><span className="history-title" title={item.title}>{item.title}</span><span className="history-meta">{clock(item.duration_seconds)}<i/> {statusName(item)}</span></div></button><IconButton className="history-delete" label={`删除「${item.title}」`} disabled={running||!!busy} onClick={event=>{deleteTriggerRef.current=event.currentTarget;deletedRef.current=false;setDeleting(item)}}><Trash2 size={15}/></IconButton></div>):<div className="history-empty"><Folder size={24}/><span>{historyQuery?'没有找到相关任务':'你的录音会保存在这里'}</span><small>{historyQuery?'试试其他文件名':'选择文件，开始第一份转写'}</small></div>}</nav>
   <div className="sidebar-bottom"><div className="local-note"><ShieldCheck size={15}/><span>任务与文字保存在本机</span><span className="local-dot"/></div><button className="sidebar-settings" onClick={()=>{setStartAfterSettings(false);setSettingsOpen(true)}}><Settings2 size={17}/><span>转写设置</span><ChevronDown size={14}/></button><div className="sidebar-version">DeepSeek 语音转写<span>v{state.version||'2.1'}</span></div></div>
  </aside>
  <main className="main-shell" inert={android&&sidebarOpen?true:undefined}>
   {android&&readingMode&&<div className="android-reading-bar"><Button variant="ghost" aria-label="退出全屏阅读" onClick={exitReading}><Minimize2 size={18}/>退出</Button><div><strong>{selectedTitle}</strong><span>全部 {segments.length} 个片段</span></div><IconButton label={readingPlaying?'暂停录音':'播放录音'} disabled={!audioUrl} onClick={toggleReadingAudio}>{readingPlaying?<Pause size={20}/>:<Play size={20}/>}</IconButton></div>}
   <header className="topbar"><div className="topbar-location"><IconButton label="打开任务导航" className="mobile-menu" onClick={()=>setSidebarOpen(true)}><Menu/></IconButton><span className="workspace-label">{android?'听记':'录音任务'}</span></div><div className="topbar-actions">{(!connected||connectionError)&&<span className={cn('connection-state',connectionError&&'is-error')}><span/>{connectionError?'连接中断':'正在连接'}</span>}{android&&<IconButton label="转写设置" onClick={()=>{setStartAfterSettings(false);setSettingsOpen(true)}}><Settings2/></IconButton>}<IconButton label="查看处理记录" onClick={()=>setLogsOpen(true)}><Clock3/></IconButton></div></header>
   {connectionError&&<div className="connection-banner" role="status"><AlertCircle size={16}/>{android?'手机服务暂时未响应，正在恢复任务状态。':'本地连接暂时中断，正在重连。请保持桌面窗口开启。'}</div>}
   <section className="document-header"><div className="document-heading"><div className="document-title"><h1 title={selectedTitle}>{selectedTitle}</h1><div className="document-meta">{sourceAvailable?<><span><Clock3 size={13}/>{clock(duration,true)}</span>{analyzed&&<span><UsersRound size={13}/>{speakerIds.length} 位说话人</span>}</>:<span>请选择本地录音</span>}</div></div>{job&&<Badge variant="secondary" className={cn('status-badge',job.complete&&'complete')}>{job.complete?<CheckCircle2 size={12}/>:running?<Loader2 size={12} className="spin"/>:null}{running?'正在处理':statusName(job)}</Badge>}</div><div className="document-tools"><IconButton label="复制完整文字" disabled={!doneCount} onClick={()=>void copyText()}><Copy/></IconButton><Select value="" onValueChange={value=>void action('open_output',{format:value})} disabled={!doneCount||!!busy}><SelectTrigger className="export-trigger"><ArrowDownToLine size={15}/><SelectValue placeholder="导出文字"/></SelectTrigger><SelectContent>{[['txt','纯文字 TXT'],['md','Markdown'],['csv','分段明细 CSV'],['json','结构化 JSON'],['srt','片段字幕 SRT']].map(([value,label])=><SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select><IconButton label={inspectorOpen?'收起人物面板':'展开人物面板'} className="panel-toggle" onClick={()=>setInspectorOpen(!inspectorOpen)}>{inspectorOpen?<PanelRightClose/>:<PanelRightOpen/>}</IconButton></div></section>
   <section className="action-strip"><div className="action-context">{busy==='choose_audio'?<><Loader2 className="spin" size={16}/><span>正在导入录音，请稍候…</span></>:running?<><Loader2 className="spin" size={16}/><span>{state.task.message||'正在处理录音…'}</span></>:state.task.stage==='error'?<><AlertCircle size={16}/><span>{state.task.message||'任务未完成，请查看处理记录。'}</span></>:state.task.stage==='cancelled'||state.task.stage==='partial'?<><Clock3 size={16}/><span>{state.task.message||'进度已保留，可以继续转写。'}</span></>:analyzed?<><CheckCircle2 size={16}/><span>{doneCount?`已转写 ${doneCount} / ${segments.length} 段`:'人物分析完成，请试听确认姓名'}</span></>:<><span className="ready-dot"/><span>{state.source?'录音已就绪，先确认发言人数':'请先选择录音'}</span></>}</div><div className="action-buttons">{!running&&<><Button variant="outline" disabled={!sourceAvailable||!!busy} onClick={()=>void analyze()}>{busy==='analyze'?<Loader2 className="spin"/>:<UsersRound size={15}/>}分析人物</Button><Button disabled={!sourceAvailable||!!busy} onClick={()=>void transcribe()}>{busy==='transcribe'?<Loader2 className="spin"/>:<AudioLines size={16}/>} {doneCount&&doneCount<segments.length?'继续转写':'开始转写'}</Button></>}{running&&<Button variant="outline" disabled={!!busy||state.task.cancel_requested} onClick={()=>void action(state.task.stage==='summary'?'cancel_summary':'cancel')}><Square size={13}/>{state.task.cancel_requested?'正在保存进度':state.task.stage==='summary'?'停止总结':'停止并保留进度'}</Button>}</div></section>
   {running&&<Progress className={cn('task-progress',!state.task.total&&'indeterminate')} value={state.task.total?state.task.current/state.task.total*100:30}/>}
   <div className={cn('editor-layout',!inspectorOpen&&'inspector-closed')}>
    <section className="editor-pane">
     {analyzed?<>
      <div className={cn('editor-toolbar','has-summary-tab',editorView==='summary'&&'is-summary-view')}>
       <div className="editor-tabs">
        <button className={editorView==='transcript'&&filter!=='uncertain'?'active':''} aria-pressed={editorView==='transcript'&&filter!=='uncertain'} onClick={()=>{setEditorView('transcript');setFilter('all')}}>分人物文字<span>{segments.length}</span></button>
        <button onClick={()=>{setEditorView('transcript');setFilter(editorView==='transcript'&&filter==='uncertain'?'all':'uncertain')}} aria-pressed={editorView==='transcript'&&filter==='uncertain'} className={cn(editorView==='transcript'&&filter==='uncertain'&&'active selected-filter')}>待核片段<span className={uncertainCount?'amber-count':''}>{uncertainCount}</span></button>
        {android&&<button className="android-reading-trigger" aria-label="全屏阅读" onClick={enterReading}><Maximize2 size={14}/>全屏阅读</button>}
        <button className={cn('summary-tab',editorView==='summary'&&'active')} aria-pressed={editorView==='summary'} onClick={()=>setEditorView('summary')}>AI 总结{job?.ai_summary?.report&&<span className="summary-tab-dot" aria-label="已生成总结"/>}</button>
       </div>
       {editorView==='transcript'&&<div className="transcript-search"><Search size={15}/><Input aria-label="搜索转写内容" placeholder="搜索文字或人物" value={query} onChange={e=>setQuery(e.target.value)}/>{query&&<button aria-label="清除搜索" onClick={()=>setQuery('')}><X size={13}/></button>}</div>}
      </div>
      {editorView==='summary'?<SummaryPanel job={job!} apiKey={apiKey} setApiKey={setApiKey} busy={!!busy} running={running} task={state.task} onGenerate={async(allowPartial,retryFailed)=>Boolean(await action('summarize',{api_key:apiKey.trim(),allow_partial:allowPartial,retry_failed:retryFailed}))} onCancel={()=>void action('cancel_summary')} onOpen={()=>void action('open_summary')}/>:<>
       <div className="filter-row"><Select value={filter} onValueChange={setFilter}><SelectTrigger className="filter-select"><ListFilter size={13}/><SelectValue/></SelectTrigger><SelectContent><SelectItem value="all">全部片段</SelectItem><SelectItem value="uncertain">仅看待核</SelectItem><SelectItem value="reviewed">已校对</SelectItem>{speakerIds.map(id=><SelectItem key={id} value={id}>{speakerLabel(id,job?.speaker_names)}</SelectItem>)}</SelectContent></Select>{(query||filter!=='all')&&<span>{filtered.length} 个片段</span>}<span className="reviewed-count"><CheckCheck size={13}/>{reviewedCount} 段已校对</span></div>
       {job?.old_version&&<div className="inline-notice"><AlertCircle size={15}/>这是旧版人物结果。重新分析会另建任务，并保留这份文字。</div>}
       <TranscriptList controller={transcriptController} restoreRequest={transcriptRestore} listIdentity={android?`${job?.id}|${readingMode?'reading':`${query}|${filter}`}`:undefined} items={filtered} names={job?.speaker_names} playTime={playTime} onPlay={playAt} onEdit={openEdit} onReview={segment=>void markReviewed(segment)} disabled={!!busy||running}/>
       <div className="transcript-footer"><span>自动转写初稿，人物与关键数字请回听确认。</span><span>{doneCount}/{segments.length} 已转写</span></div>
      </>}
     </>:<EmptyState source={state.source} chooseAudio={chooseAudio} busy={!!busy||running} settings={state.settings} configure={settings=>void action('config',settings)} analyzing={running}/>}
    </section>
    <aside className={cn('inspector',!inspectorOpen&&'is-hidden')}><div className="inspector-heading"><h2><UsersRound size={16}/>人物校对</h2><span>{speakerIds.length?`${speakerIds.length} 人`:'待分析'}</span>{android&&<IconButton className="inspector-close" label="关闭人物面板" onClick={()=>setInspectorOpen(false)}><X/></IconButton>}</div>{speakerIds.length?<><p className="inspector-intro">试听后确认姓名。</p><div className="speaker-list">{speakerIds.map(id=>{const sample=job?.samples?.find(s=>s.speaker===id);const speakerSeconds=segments.filter(s=>s.speaker===id).reduce((sum,s)=>sum+s.end-s.start,0);return <div className="speaker-card" key={id}><div className="speaker-card-header"><span className={cn('speaker-avatar',colorFor(id))}>{id.slice(1)}</span><div><strong>{speakerLabel(id,job?.speaker_names)}</strong><small>{clock(speakerSeconds)} 发言</small></div><Tooltip><TooltipTrigger asChild><Button variant="ghost" size="icon-sm" aria-label={`试听${speakerLabel(id,job?.speaker_names)}`} disabled={!sample?.audio_url} onClick={()=>{if(sample?.audio_url)playSample(id,sample.audio_url)}}>{samplePlaying===id?<Pause size={14}/>:<Play size={14}/>}</Button></TooltipTrigger><TooltipContent>试听声音样例</TooltipContent></Tooltip></div><label className="sr-only" htmlFor={`name-${id}`}>{id} 的姓名</label><Input id={`name-${id}`} placeholder="填写姓名或称呼" value={names[id]||''} onChange={e=>setNames({...names,[id]:e.target.value})} disabled={running}/>{sample&&(sample.too_short||sample.uncertain)&&<small className="sample-warning"><AlertCircle size={11}/> {sample.too_short?'样例较短，请回听确认':'样例归属待核'}</small>}</div>})}</div><Button className="save-names" variant="outline" disabled={!!busy||running} onClick={()=>void action('speaker_names',{names}).then(next=>next&&toast.success('人物姓名已更新'))}><Check size={14}/>保存人物姓名</Button></>:<div className="inspector-empty"><strong>暂无人物</strong><p>分析录音后可试听声音、填写姓名。</p><span className="free-note"><ShieldCheck size={12}/>人物分析不调用 API</span></div>}
      {!!job?.warnings?.length&&<div className="analysis-warnings" role="status"><strong><AlertCircle size={14}/>需要留意</strong>{job.warnings.map((warning,i)=><p key={i}>{warning}</p>)}</div>}
      <div className="inspector-divider"/><div className="inspector-section-title">转写信息<button aria-label="修改转写设置" onClick={()=>{setStartAfterSettings(false);setSettingsOpen(true)}}><SlidersHorizontal size={14}/></button></div><dl className="info-list"><div><dt>识别服务</dt><dd>DeepSeek</dd></div><div><dt>发言人数</dt><dd>{state.settings.num_speakers===0?'自动判断':`${state.settings.num_speakers} 人`}</dd></div><div><dt>预计费用</dt><dd>约 ¥{(duration/3600*state.settings.hourly_price).toFixed(3)}</dd></div></dl><p className="cost-note">按音频时长粗略估算，实际以接口账单为准。</p><div className="saved-location"><Folder size={14}/><div><span>保存在本地</span><button title={state.settings.output_root} onClick={()=>{if(android){if(job)void action('open_output',{format:'txt'});else toast.info('任务保存在应用内，可在完成后导出分享。')}else void action(job?'open_output':'choose_output')}}>{android?(job?'导出或分享文字':'应用内私有目录'):(state.settings.output_root?state.settings.output_root.split(/[\\/]/).filter(Boolean).slice(-2).join(' / '):'选择保存目录')}<ArrowRight size={12}/></button></div></div>
    </aside>
   </div>
   <AudioPlayer audioRef={audioRef} src={audioUrl} duration={duration} title={selectedTitle} onTime={setPlayTime}/>
  </main>
  {android&&<nav className="android-navigation" aria-label="主要功能" inert={sidebarOpen?true:undefined}>
   <button aria-label="录音任务" aria-pressed={sidebarOpen} onClick={()=>{setInspectorOpen(false);setSidebarOpen(true)}}><Folder aria-hidden="true"/><span>任务</span></button>
   <button aria-label="分人物转写文字" aria-pressed={!sidebarOpen&&!inspectorOpen&&editorView==='transcript'} onClick={()=>{setSidebarOpen(false);setInspectorOpen(false);setEditorView('transcript')}}><FileAudio2 aria-hidden="true"/><span>文字</span></button>
   <button aria-label="人物样例与姓名校对" aria-pressed={inspectorOpen&&!sidebarOpen} onClick={()=>{setSidebarOpen(false);setInspectorOpen(true)}}><UsersRound aria-hidden="true"/><span>人物</span></button>
   <button aria-label="总体与逐人总结" disabled={!analyzed} aria-pressed={!sidebarOpen&&!inspectorOpen&&editorView==='summary'} onClick={()=>{setSidebarOpen(false);setInspectorOpen(false);setEditorView('summary')}}><FileText aria-hidden="true"/><span>总结</span></button>
  </nav>}
 </div>
 <Dialog open={!!deleting} onOpenChange={open=>{if(!open&&!busy)setDeleting(null)}}><DialogContent showCloseButton={!busy} className="delete-dialog" onOpenAutoFocus={event=>{event.preventDefault();deleteCancelRef.current?.focus()}} onCloseAutoFocus={event=>{event.preventDefault();if(deletedRef.current){(historyRef.current?.querySelector<HTMLButtonElement>('.history-item:not(:disabled)')||newTaskRef.current)?.focus();deletedRef.current=false}else deleteTriggerRef.current?.focus()}}><DialogHeader><DialogTitle>删除这条录音任务？</DialogTitle><DialogDescription>将从历史列表中移除，并永久删除本机保存的转写文字、人物校对、AI 总结及音频缓存。{android?'从其它应用导入的原始文件会保留。请先导出需要保存的结果。':'原始录音文件会保留。'}</DialogDescription></DialogHeader><p className="delete-job-title">{deleting?.title}</p><p className="delete-warning">此操作无法撤销，删除后需重新分析和转写。</p><DialogFooter><Button ref={deleteCancelRef} variant="outline" disabled={!!busy} onClick={()=>setDeleting(null)}>取消</Button><Button variant="destructive" disabled={!!busy||running} onClick={()=>void deleteJob()}>{busy==='delete_job'?<Loader2 className="spin"/>:<Trash2 size={15}/>} {busy==='delete_job'?'正在删除…':'删除任务'}</Button></DialogFooter></DialogContent></Dialog>
 {android&&<AndroidWelcome open={welcomeOpen} apiKey={apiKey} onKeyChange={setApiKey} onComplete={finishWelcome}/>}
 <SettingsDialog open={settingsOpen} onOpenChange={open=>{setSettingsOpen(open);if(!open)setStartAfterSettings(false)}} settings={state.settings} apiKey={apiKey} setApiKey={setApiKey} busy={!!busy||running} startAfter={startAfterSettings} chooseOutput={async()=>{const next=await action('choose_output');return next?.settings.output_root}} save={async(settings)=>{const next=await action('config',settings);if(next){setSettingsOpen(false);if(startAfterSettings){setStartAfterSettings(false);if(!apiKey.trim()){toast.error('请填写 DeepSeek API Key');return}await action('transcribe',{api_key:apiKey.trim(),retry_uncertain:settings.retry_uncertain})}else toast.success('转写设置已保存')}}}/>
 <Dialog open={!!editing} onOpenChange={open=>!open&&setEditing(null)}><DialogContent className="edit-dialog"><DialogHeader><DialogTitle>校对这一段发言</DialogTitle><DialogDescription>{editing&&`${clock(editing.start,true)} — ${clock(editing.end,true)}`}　修改会保存在本机，并同步到导出文件。</DialogDescription></DialogHeader><div className="edit-speaker-row"><label>说话人</label><Select value={draftSpeaker} onValueChange={setDraftSpeaker}><SelectTrigger><SelectValue/></SelectTrigger><SelectContent>{speakerIds.map(id=><SelectItem value={id} key={id}>{speakerLabel(id,job?.speaker_names)}</SelectItem>)}</SelectContent></Select><Button variant="outline" onClick={()=>editing&&playAt(editing.start)}><Play size={14}/>回听本段</Button></div><label htmlFor="transcript-edit" className="field-label">发言内容</label><Textarea id="transcript-edit" className="transcript-textarea" value={draftText} onChange={e=>setDraftText(e.target.value)}/><DialogFooter><Button variant="outline" onClick={()=>setEditing(null)}>取消</Button><Button disabled={!!busy} onClick={()=>void saveReview()}>{busy?<Loader2 className="spin"/>:<CheckCheck size={15}/>}保存并标记已校对</Button></DialogFooter></DialogContent></Dialog>
 <Dialog open={logsOpen} onOpenChange={setLogsOpen}><DialogContent className="logs-dialog"><DialogHeader><DialogTitle>处理记录</DialogTitle><DialogDescription>查看当前任务的分析、转写与恢复进度。</DialogDescription></DialogHeader><div className="log-list">{state.logs.length?state.logs.map((line,i)=><div key={i}><time>{line.time}</time><p>{line.message}</p></div>):<p className="no-logs">还没有处理记录。选择录音后开始分析。</p>}</div></DialogContent></Dialog>
 <Toaster richColors closeButton position="top-center"/></TooltipProvider>
}

function EmptyState({source,chooseAudio,busy,settings,configure}:{source:AppState['source'];chooseAudio:()=>Promise<void>;busy:boolean;settings:Settings;configure:(settings:Partial<Settings>)=>void;analyzing:boolean}){
 return <div className="empty-workspace">
  <div className="empty-main">
   <h2>{source?'录音已选择':'选择录音'}</h2>
   <p>{source?'确认发言人数后，先分析人物，再开始转写。':'选择本地录音，按人物转写为文字。支持长录音和断点续传。'}</p>
   {source?<div className="selected-file"><span><FileAudio2 size={19}/></span><div><strong title={source.name}>{source.name}</strong><small>{clock(source.duration_seconds,true)}　{source.channels===1?'单声道':source.channels?`${source.channels} 声道`:''}</small></div><Button variant="ghost" size="sm" disabled={busy} onClick={()=>void chooseAudio()}>更换</Button></div>:<Button size="lg" className="choose-main" disabled={busy} onClick={()=>void chooseAudio()}>{busy?<Loader2 className="spin"/>:<Plus size={18}/>} {busy?'正在导入录音…':'选择本地录音'}</Button>}
   {source?<div className="count-inline"><UsersRound size={15}/><label htmlFor="empty-speakers">实际发言人数</label><Select value={String(settings.num_speakers)} onValueChange={v=>configure({num_speakers:Number(v)})} disabled={busy}><SelectTrigger id="empty-speakers"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="0">自动判断</SelectItem>{Array.from({length:12},(_,i)=><SelectItem value={String(i+1)} key={i}>{i+1} 人</SelectItem>)}</SelectContent></Select></div>:<small className="supported-files">{isAndroidApp()?'支持 MP3、M4A、WAV 等系统可解码的音频':'支持 MP3、M4A、WAV、FLAC 及视频音轨'}</small>}
  </div>
  <div className="empty-bottom"><ShieldCheck size={14}/><span>人物分析在本地完成。转写时，音频片段会发送至 DeepSeek。</span></div>
 </div>
}

function TranscriptList({items,names,playTime,onPlay,onEdit,onReview,disabled,controller,restoreRequest,listIdentity}:{items:Segment[];names?:Record<string,string>;playTime:number;onPlay:(time:number)=>void;onEdit:(s:Segment)=>void;onReview:(s:Segment)=>void;disabled:boolean;controller:RefObject<TranscriptController|null>;restoreRequest:TranscriptRestore|null;listIdentity?:string}){
 const parentRef=useRef<HTMLDivElement>(null)
 const handledRestore=useRef(0)
 const virtualizer=useVirtualizer({count:items.length,getScrollElement:()=>parentRef.current,estimateSize:()=>132,overscan:5})
 useImperativeHandle(controller,()=>({capture:()=>{
  const top=parentRef.current?.scrollTop||0
  const row=virtualizer.getVirtualItems().find(item=>item.end>top)
  return {top,anchorId:row?items[row.index]?.id:undefined,inset:row?top-row.start:0}
 }}),[items,virtualizer])
 useLayoutEffect(()=>{
  virtualizer.measure()
  const request=restoreRequest&&restoreRequest.serial!==handledRestore.current?restoreRequest:null
  if(!request){parentRef.current?.scrollTo({top:0});return}
  handledRestore.current=request.serial
  const position=request.position
  const index=position?.anchorId?items.findIndex(item=>item.id===position.anchorId):-1
  if(index<0){virtualizer.scrollToOffset(position?.top||0);return}
  virtualizer.scrollToIndex(index,{align:'start'})
  let secondFrame=0
  const frame=requestAnimationFrame(()=>{
   virtualizer.scrollToIndex(index,{align:'start'})
   secondFrame=requestAnimationFrame(()=>{
    const row=virtualizer.getVirtualItems().find(item=>item.index===index)
    if(row)virtualizer.scrollToOffset(row.start+(position?.inset||0))
   })
  })
  return()=>{cancelAnimationFrame(frame);cancelAnimationFrame(secondFrame)}
 },[items.length,listIdentity,restoreRequest?.serial])
 return <div className="transcript-scroll" data-total-segments={items.length} ref={parentRef}>{!items.length?<div className="no-search-results"><Search size={27}/><strong>没有匹配的片段</strong><p>更换关键词，或切回全部片段。</p></div>:<div className="virtual-transcript" style={{height:virtualizer.getTotalSize(),position:'relative'}}>{virtualizer.getVirtualItems().map(v=>{const s=items[v.index];const active=playTime>=s.start&&playTime<s.end;return <div key={s.id} data-index={v.index} data-segment-id={s.id} ref={virtualizer.measureElement} className="virtual-segment" style={{position:'absolute',top:0,left:0,width:'100%',transform:`translateY(${v.start}px)`}}><article className={cn('transcript-segment',active&&'is-playing')}><div className={cn('speaker-avatar',colorFor(s.speaker))}>{s.speaker.slice(1)}</div><div className="segment-body"><div className="segment-topline"><strong className={cn('speaker-name',colorFor(s.speaker))}>{speakerLabel(s.speaker,names)}</strong><button className="segment-time" onClick={()=>onPlay(s.start)} aria-label={`从 ${clock(s.start)} 回听`}><Play size={10}/>{clock(s.start,true)}<span>— {clock(s.end,true)}</span></button>{s.uncertain&&!s.reviewed&&<span className="uncertain-label"><span/>归属待核</span>}{s.reviewed&&<span className="reviewed-label"><CheckCheck size={12}/>已校对</span>}{s.edited&&!s.reviewed&&<span className="edited-label">已编辑</span>}</div>{s.state==='done'?<p className="segment-text">{s.text||<span className="pending-text">本段未识别出文字，建议回听确认。</span>}</p>:<p className="pending-text">{s.state==='ambiguous'?'上次请求结果不明，默认不会重复提交。':s.state==='failed'?'本段转写未完成，可以继续重试。':s.state==='requesting'?'正在识别这段发言…':'等待转写'}</p>}</div><div className="segment-controls"><IconButton label="编辑文字与人物" disabled={disabled||s.state!=='done'} onClick={()=>onEdit(s)}><PencilLine size={14}/></IconButton><IconButton label={s.reviewed?'取消已校对':'标记已校对'} disabled={disabled||s.state!=='done'} onClick={()=>onReview(s)}><Check size={14}/></IconButton></div></article></div>})}</div>}</div>
}

function AudioPlayer({audioRef,src,duration,title,onTime}:{audioRef:RefObject<HTMLAudioElement|null>;src:string;duration:number;title:string;onTime:(time:number)=>void}){
 const [playing,setPlaying]=useState(false);const [time,setTime]=useState(0);const [loadedDuration,setLoadedDuration]=useState(0);const [rate,setRate]=useState('1');const [volume,setVolume]=useState(.8)
 useEffect(()=>{setPlaying(false);setTime(0);setLoadedDuration(0);onTime(0)},[src])
 const length=loadedDuration||duration
 function toggle(){const a=audioRef.current;if(!a||!src)return;if(a.paused)void a.play().catch(()=>toast.error('音频读取失败，请确认文件仍在原位置。'));else a.pause()}
 function seek(value:number){const a=audioRef.current;if(a){a.currentTime=Math.max(0,Math.min(length,value));setTime(a.currentTime);onTime(a.currentTime)}}
 return <footer className="audio-player"><audio ref={audioRef} src={src||undefined} preload="metadata" onPlay={()=>setPlaying(true)} onPause={()=>setPlaying(false)} onEnded={()=>setPlaying(false)} onTimeUpdate={e=>{setTime(e.currentTarget.currentTime);onTime(e.currentTarget.currentTime)}} onLoadedMetadata={e=>{if(Number.isFinite(e.currentTarget.duration))setLoadedDuration(e.currentTarget.duration);e.currentTarget.volume=volume;e.currentTarget.playbackRate=Number(rate)}}/><div className="player-topline"><div className="player-controls"><IconButton label="后退 10 秒" disabled={!src} onClick={()=>seek(time-10)}><SkipBack size={17}/></IconButton><Button className="play-button" size="icon" disabled={!src} aria-label={playing?'暂停录音':'播放录音'} onClick={toggle}>{playing?<Pause size={17} fill="currentColor"/>:<Play size={17} fill="currentColor"/>}</Button><IconButton label="前进 10 秒" disabled={!src} onClick={()=>seek(time+10)}><SkipForward size={17}/></IconButton></div><div className="player-track"><div className="track-heading"><span><Headphones size={13}/>{src?title:'还没有选择录音'}</span><time>{clock(time,true)}<i>/</i>{clock(length,true)}</time></div><input className="audio-seek" aria-label="音频播放位置" type="range" min="0" max={length||1} step="0.1" value={Math.min(time,length||1)} disabled={!src} style={{'--played':`${length?time/length*100:0}%`} as React.CSSProperties} onChange={e=>seek(Number(e.target.value))}/></div><div className="player-options"><Select value={rate} onValueChange={value=>{setRate(value);if(audioRef.current)audioRef.current.playbackRate=Number(value)}}><SelectTrigger aria-label="播放速度" className="speed-select"><SelectValue/></SelectTrigger><SelectContent>{['0.75','1','1.25','1.5','2'].map(value=><SelectItem key={value} value={value}>{value}×</SelectItem>)}</SelectContent></Select><Volume2 size={16}/><input aria-label="播放音量" type="range" min="0" max="1" step=".05" value={volume} onChange={e=>{const value=Number(e.target.value);setVolume(value);if(audioRef.current)audioRef.current.volume=value}}/></div></div></footer>
}

function SettingsDialog({open,onOpenChange,settings,apiKey,setApiKey,busy,startAfter,chooseOutput,save}:{open:boolean;onOpenChange:(open:boolean)=>void;settings:Settings;apiKey:string;setApiKey:(key:string)=>void;busy:boolean;startAfter:boolean;chooseOutput:()=>Promise<string|undefined>;save:(settings:Settings)=>Promise<void>}){
 const [draft,setDraft]=useState(settings);const [show,setShow]=useState(false)
 useEffect(()=>{if(open)setDraft({...settings})},[open])
 async function submit(){if(!Number.isFinite(draft.num_speakers)||draft.num_speakers<0||draft.num_speakers>20){toast.error('发言人数请填写 0 到 20');return}if(!(draft.hourly_price>0)||draft.max_workers<1||draft.max_workers>4){toast.error('请检查单价和同时转写段数');return}if(startAfter&&!apiKey.trim()){toast.error('请填写 DeepSeek API Key');return}await save(draft)}
 return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent className="settings-dialog"><DialogHeader><DialogTitle>转写设置</DialogTitle><DialogDescription>设置只保存在本机。API Key 仅在当前窗口内使用。</DialogDescription></DialogHeader><div className="settings-content"><section className="settings-section"><h3><ShieldCheck size={15}/>DeepSeek 接口</h3><label htmlFor="api-key" className="field-label">API Key</label><div className="password-field"><Input id="api-key" type={show?'text':'password'} autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder="粘贴你的 DeepSeek API Key" value={apiKey} onChange={e=>setApiKey(e.target.value)}/><button aria-label={show?'隐藏 API Key':'显示 API Key'} onClick={()=>setShow(!show)}>{show?<EyeOff size={16}/>:<Eye size={16}/>}</button></div><p className="field-hint">分析人物不需要填写。开始文字转写后按上传音频时长计费。</p></section><section className="settings-section"><h3><UsersRound size={15}/>录音与人物</h3><div className="settings-two-cols"><div><label htmlFor="people-count" className="field-label">实际发言人数</label><Input id="people-count" type="number" min="0" max="20" value={draft.num_speakers} onChange={e=>setDraft({...draft,num_speakers:Number(e.target.value)})}/><p className="field-hint">0 为自动判断，知道人数时建议填写。</p></div>{!isAndroidApp()&&<div><label htmlFor="workers-count" className="field-label">同时转写段数</label><Select value={String(draft.max_workers)} onValueChange={v=>setDraft({...draft,max_workers:Number(v)})}><SelectTrigger id="workers-count"><SelectValue/></SelectTrigger><SelectContent>{[1,2,3,4].map(n=><SelectItem key={n} value={String(n)}>{n} 段</SelectItem>)}</SelectContent></Select><p className="field-hint">遇到服务限流时，可以调低。</p></div>}</div><label className="field-label">{isAndroidApp()?'本地数据':'结果保存目录'}</label><div className="output-field"><div title={draft.output_root}>{isAndroidApp()?'保存在听记的应用私有目录':(draft.output_root||'尚未设置')}</div>{!isAndroidApp()&&<Button variant="outline" size="sm" disabled={busy} onClick={()=>void chooseOutput().then(path=>path&&setDraft({...draft,output_root:path}))}><Folder size={14}/>更改</Button>}</div>{isAndroidApp()&&<p className="field-hint">卸载会清除应用内数据。请通过导出分享保存重要的文字与总结。</p>}</section><section className="settings-section"><h3><SlidersHorizontal size={15}/>费用与恢复</h3><div className="price-field"><label htmlFor="hourly-price" className="field-label">估算单价</label><Input id="hourly-price" type="number" min="0.001" step="0.01" value={draft.hourly_price} onChange={e=>setDraft({...draft,hourly_price:Number(e.target.value)})}/><span>元 / 小时</span></div><p className="field-hint">只用于界面估算，实际价格以 DeepSeek 官方账单为准。</p><label className="retry-checkbox"><Checkbox checked={draft.retry_uncertain} onCheckedChange={v=>setDraft({...draft,retry_uncertain:v===true})}/><span><strong>重试上次结果不明的片段</strong><small>上次超时可能已经扣费，重新提交可能再次计费。</small></span></label></section></div><DialogFooter><Button variant="outline" onClick={()=>onOpenChange(false)}>取消</Button><Button disabled={busy} onClick={()=>void submit()}>{busy?<Loader2 className="spin"/>:<Check size={15}/>} {startAfter?'保存并开始转写':'保存设置'}</Button></DialogFooter></DialogContent></Dialog>
}
