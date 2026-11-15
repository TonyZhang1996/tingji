import { useEffect, useState } from 'react'
import { AlertCircle, ArrowDownToLine, Check, ClipboardList, Copy, Eye, EyeOff, FileText, Loader2, RefreshCw, ShieldCheck, Square } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import { type AppState, type Job, type SummaryReport } from '@/lib/api'
import { cn } from '@/lib/utils'

type SummaryPanelProps = {
  job:Job
  apiKey:string
  setApiKey:(value:string)=>void
  busy:boolean
  running:boolean
  task:AppState['task']
  onGenerate:(allowPartial:boolean)=>Promise<boolean>
  onCancel:()=>void
  onOpen:()=>void
}

function summaryText(title:string,report:SummaryReport) {
  const { content,coverage }=report
  const lines=[`# ${title} · AI 总结`,'',coverage.full?'范围：整条录音已完成的转写文字。':`范围：仅覆盖已转写的 ${coverage.done_count} / ${coverage.total_count} 个片段，未转写部分未纳入。`,'','AI 根据转写初稿整理，人物、数字和结论请回听确认。','','## 内容概览','',content.overview,'','## 讨论要点','']
  content.topics.forEach(topic=>lines.push(`### ${topic.title}`,'',topic.summary,''))
  if(!content.topics.length)lines.push('暂无更多明确的讨论要点。','')
  lines.push('## 已明确的决定','')
  lines.push(...(content.decisions.length?content.decisions.map(item=>`- ${item}`):['录音中暂无明确决定。']))
  lines.push('','## 待确认问题','')
  lines.push(...(content.open_questions.length?content.open_questions.map(item=>`- ${item}`):['暂无额外待确认问题。']))
  lines.push('','## 待办事项','')
  if(content.todos.length)content.todos.forEach((item,index)=>lines.push(`${index+1}. ${item.task}`,`   - 负责人：${item.owner||'未明确'}`,`   - 期限：${item.deadline||'未明确'}`,`   - 依据：${item.evidence||'未明确'}`,''))
  else lines.push('录音中暂无明确待办事项。')
  return lines.join('\n').trim()
}

export function SummaryPanel({job,apiKey,setApiKey,busy,running,task,onGenerate,onCancel,onOpen}:SummaryPanelProps) {
  const [dialogOpen,setDialogOpen]=useState(false)
  const [allowPartial,setAllowPartial]=useState(false)
  const [showKey,setShowKey]=useState(false)
  const summary=job.ai_summary
  const report=summary?.report
  const segments=job.segments||[]
  const doneCount=segments.filter(segment=>segment.state==='done').length
  const hasText=segments.some(segment=>segment.state==='done'&&segment.text.trim())
  const full=segments.length>0&&doneCount===segments.length
  const generating=summary?.status==='running'||running&&task.stage==='summary'
  const progress=summary?.progress
  const total=progress?.total??(generating?task.total:0)
  const current=progress?.current??(generating?task.current:0)
  const progressMessage=progress?.message||summary?.message||task.message||'正在整理录音内容…'
  const created=report?.created_at?new Date(report.created_at):null
  const createdLabel=created&&!Number.isNaN(created.getTime())?created.toLocaleString('zh-CN',{hour12:false}):''
  useEffect(()=>{if(dialogOpen){setAllowPartial(false);setShowKey(false)}},[dialogOpen,job.id])
  async function generate(){if(await onGenerate(!full&&allowPartial))setDialogOpen(false)}
  async function copy(){if(!report)return;try{await navigator.clipboard.writeText(summaryText(job.title,report));toast.success('总结与待办事项已复制')}catch{toast.error('复制未完成，请打开 Markdown 文件。')}}
  return <div className="summary-workspace">
    <div className="summary-actions">
      <div className="summary-actions-label"><div><strong>录音总结</strong><span>{report?(report.coverage.full?'基于整条录音的转写文字':`仅覆盖 ${report.coverage.done_count} / ${report.coverage.total_count} 个片段`):'主要内容与待办事项'}</span></div></div>
      <div className="summary-action-buttons">{report&&<><Button size="sm" variant="ghost" onClick={()=>void copy()} aria-label="复制总结与待办"><Copy size={14}/><span>复制</span></Button><Button size="sm" variant="outline" onClick={onOpen} disabled={busy} aria-label="打开总结 Markdown"><ArrowDownToLine size={14}/><span>打开文件</span></Button></>}{generating?<Button size="sm" variant="outline" onClick={onCancel} disabled={busy||task.cancel_requested}><Square size={12}/>{task.cancel_requested?'正在停止':'停止总结'}</Button>:<Button size="sm" onClick={()=>setDialogOpen(true)} disabled={busy||running||!hasText}>{report?<RefreshCw size={14}/>:<FileText size={14}/>}<span>{report?'重新生成':'生成总结'}</span></Button>}</div>
    </div>
    <div className="summary-scroll">
      {generating&&<div className="summary-progress" role="status" aria-live="polite"><div><Loader2 size={16} className="spin"/><strong>{progressMessage}</strong>{total>0&&<span>{current} / {total}</span>}</div><Progress aria-label="AI 总结进度" value={total?Math.max(0,Math.min(100,current/total*100)):undefined}/><p>长录音会分批整理，再合并为一份总结。已完成的整理进度会保留。</p></div>}
      {summary?.status==='error'&&<div className="summary-notice is-warning" role="alert"><AlertCircle size={16}/><div><strong>这次总结未完成</strong><p>{summary.message||'请稍后重试；已生成的内容仍保留在本机。'}</p>{report&&<p>下面保留的是上一次成功生成的总结。</p>}</div></div>}
      {summary?.status==='cancelled'&&!generating&&<div className="summary-notice" role="status"><ShieldCheck size={16}/><div><strong>总结已停止</strong><p>{summary.message||'再次生成时会复用已经整理好的部分。'}{report?'下面保留上一次成功生成的总结。':''}</p></div></div>}
      {report&&summary?.stale&&<div className="summary-notice is-warning" role="status"><AlertCircle size={16}/><div><strong>转写或人物已更新，建议重新生成</strong><p>当前显示的是修改前生成的总结。</p></div></div>}
      {report?<article className="summary-document">
        <h2 className="sr-only">录音总结</h2>
        <header className="summary-document-header"><div><FileText size={15}/><span>录音总结</span><span className={cn('summary-coverage',!report.coverage.full&&'is-partial')}>{report.coverage.full?'整条录音':'部分内容'}</span></div>{createdLabel&&<time dateTime={report.created_at}>{createdLabel} 生成</time>}<p className="summary-source-note">AI 根据转写初稿整理，人物、数字和结论请回听确认。{!report.coverage.full&&`未转写的 ${Math.max(0,report.coverage.total_count-report.coverage.done_count)} 个片段未纳入本次总结。`}</p></header>
        <section className="summary-section summary-overview"><h3>内容概览</h3><p>{report.content.overview}</p></section>
        <section className="summary-section"><h3>讨论要点</h3>{report.content.topics.length?<div className="summary-topics">{report.content.topics.map((topic,index)=><div key={index}><h4>{topic.title}</h4><p>{topic.summary}</p></div>)}</div>:<p className="summary-muted">暂无更多明确的讨论要点。</p>}</section>
        <section className="summary-section"><h3>已明确的决定</h3>{report.content.decisions.length?<ul className="summary-list">{report.content.decisions.map((item,index)=><li key={index}>{item}</li>)}</ul>:<p className="summary-muted">录音中暂无明确决定。</p>}</section>
        <section className="summary-section"><h3>待确认问题</h3>{report.content.open_questions.length?<ul className="summary-list">{report.content.open_questions.map((item,index)=><li key={index}>{item}</li>)}</ul>:<p className="summary-muted">暂无额外待确认问题。</p>}</section>
        <section className="summary-section summary-todos"><div className="summary-section-heading"><h3><ClipboardList size={17}/>待办事项</h3><span>{report.content.todos.length} 项</span></div>{report.content.todos.length?<ol className="summary-todo-list">{report.content.todos.map((item,index)=><li key={index}><span className="summary-todo-number">{index+1}</span><div><h4>{item.task}</h4><dl><div><dt>负责人</dt><dd>{item.owner||'未明确'}</dd></div><div><dt>期限</dt><dd>{item.deadline||'未明确'}</dd></div></dl><p className="summary-evidence"><span>依据</span>{item.evidence||'未明确'}</p></div></li>)}</ol>:<div className="summary-no-todos"><Check size={16}/><p>录音中暂无明确待办事项。</p></div>}</section>
      </article>:!generating&&<div className="summary-empty"><h2>尚未生成总结</h2><p>根据转写内容整理要点，末尾列出待办事项。</p>{hasText?<><Button onClick={()=>setDialogOpen(true)} disabled={busy||running}><FileText size={16}/>生成总结</Button>{!full&&<span className="summary-empty-hint"><AlertCircle size={13}/>已转写 {doneCount} / {segments.length} 段，完成后可生成整篇总结</span>}</>:<div className="summary-before-transcript">先完成文字转写，再生成总结。</div>}<div className="summary-empty-note"><ShieldCheck size={14}/><span>负责人和期限以录音中明确内容为准。</span></div></div>}
    </div>
    <Dialog open={dialogOpen} onOpenChange={setDialogOpen}><DialogContent className="summary-dialog"><DialogHeader><DialogTitle>生成录音总结</DialogTitle><DialogDescription>整理讨论要点和决定，并在最后列出待办事项。</DialogDescription></DialogHeader><div className="summary-dialog-content"><div className="summary-input-scope"><FileText size={19}/><div><strong>{job.title}</strong><span>{full?`已完成 ${doneCount} 个片段，将总结整条录音。`:`已转写 ${doneCount} / ${segments.length} 个片段。`}</span></div></div>{!full&&<div className="summary-partial-option"><p><AlertCircle size={15}/>录音尚未全部转写，建议完成后生成整篇总结。</p><label><Checkbox checked={allowPartial} onCheckedChange={value=>setAllowPartial(value===true)}/><span><strong>仅总结已转写部分</strong><small>未转写部分不会纳入，总结将明确标注覆盖范围。</small></span></label></div>}<label htmlFor="summary-api-key" className="field-label">DeepSeek API Key</label><div className="password-field"><Input id="summary-api-key" type={showKey?'text':'password'} autoComplete="off" spellCheck={false} placeholder="粘贴你的 DeepSeek API Key" value={apiKey} onChange={event=>setApiKey(event.target.value)} disabled={busy||running}/><button aria-label={showKey?'隐藏总结 API Key':'显示总结 API Key'} onClick={()=>setShowKey(!showKey)}>{showKey?<EyeOff size={16}/>:<Eye size={16}/>}</button></div><p className="field-hint">Key 仅在当前窗口内使用，不会保存到文件。</p><p className="summary-api-note">会将转写文字发送给 DeepSeek，按文字量另外计费。长录音会分批整理，成功的部分保留在本机，方便重试时复用。</p></div><DialogFooter><Button variant="outline" onClick={()=>setDialogOpen(false)}>取消</Button><Button disabled={busy||running||!hasText||!apiKey.trim()||!full&&!allowPartial} onClick={()=>void generate()}>{busy?<Loader2 size={15} className="spin"/>:<FileText size={15}/>}开始生成</Button></DialogFooter></DialogContent></Dialog>
  </div>
}
