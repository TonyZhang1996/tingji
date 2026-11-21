import { useCallback, useId, useLayoutEffect, useRef, type KeyboardEvent } from 'react'
import { Check, Loader2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'

export type InlineTranscriptEditProps={
 text:string
 draft?:string
 label:string
 disabled:boolean
 saving:boolean
 onChange:(text:string)=>void
 onSave:()=>void
 onCancel:()=>void
}

/** The parent retains drafts when virtualization unmounts a transcript row. */
export function InlineTranscriptEdit({text,draft,label,disabled,saving,onChange,onSave,onCancel}:InlineTranscriptEditProps){
 const value=draft??text
 const modified=value!==text
 const blocked=disabled||saving
 const helpId=useId()
 const textareaRef=useRef<HTMLTextAreaElement>(null)
 const composing=useRef(false)
 const nativeSizing=typeof CSS!=='undefined'&&CSS.supports('field-sizing','content')
 const fitFallback=useCallback(()=>{
  const field=textareaRef.current
  if(!field||nativeSizing)return
  field.style.height='0px'
  field.style.height=`${field.scrollHeight+1}px`
 },[nativeSizing])
 useLayoutEffect(()=>{fitFallback()},[value,fitFallback])
 useLayoutEffect(()=>{
  const field=textareaRef.current
  if(!field||nativeSizing)return
  let width=field.getBoundingClientRect().width
  if(typeof ResizeObserver==='undefined'){
   window.addEventListener('resize',fitFallback)
   return()=>window.removeEventListener('resize',fitFallback)
  }
  let frame=0
  const observer=new ResizeObserver(()=>{
   const next=field.getBoundingClientRect().width
   if(Math.abs(next-width)>.5){width=next;cancelAnimationFrame(frame);frame=requestAnimationFrame(fitFallback)}
  })
  observer.observe(field)
  return()=>{observer.disconnect();cancelAnimationFrame(frame)}
 },[fitFallback,nativeSizing])
 function keyDown(event:KeyboardEvent<HTMLTextAreaElement>){
  if(composing.current||event.nativeEvent.isComposing||event.keyCode===229)return
  if(event.key==='Enter'&&event.ctrlKey){
   event.preventDefault();event.stopPropagation()
   if(modified&&!blocked)onSave()
  }else if(event.key==='Escape'&&modified&&!blocked){
   event.preventDefault();event.stopPropagation();onCancel()
  }
 }
 return <div className="inline-transcript-edit" data-modified={modified||undefined} aria-busy={saving||undefined}>
  <textarea ref={textareaRef} className="segment-text inline-transcript-textarea" rows={1} wrap="soft" value={value} readOnly={blocked}
   aria-label={label} aria-describedby={helpId} aria-keyshortcuts="Control+Enter Escape" spellCheck={false}
   onChange={event=>{if(!blocked)onChange(event.target.value)}} onKeyDown={keyDown}
   onCompositionStart={()=>{composing.current=true}} onCompositionEnd={()=>{composing.current=false}}/>
  <span id={helpId} className="sr-only">直接编辑文字。Enter 换行，Ctrl+Enter 保存，Esc 取消修改。离开发言不会自动保存。</span>
  {(modified||saving)&&<div className="inline-transcript-actions" role="group" aria-label={`${label}的编辑操作`}>
   <Button type="button" size="sm" disabled={blocked||!modified} onClick={onSave} aria-busy={saving||undefined}>{saving?<Loader2 size={14} className="spin" aria-hidden="true"/>:<Check size={14} aria-hidden="true"/>}保存</Button>
   <Button type="button" size="sm" variant="outline" disabled={blocked} onClick={()=>{onCancel();textareaRef.current?.focus()}}><X size={14} aria-hidden="true"/>取消</Button>
   <span className="inline-transcript-shortcuts" aria-hidden="true">Ctrl+Enter 保存 · Esc 取消</span>
   <span className="sr-only" role="status">{saving?'正在保存修改':'修改尚未保存'}</span>
  </div>}
 </div>
}
