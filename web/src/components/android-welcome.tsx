import { useRef } from 'react'
import { ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ApiKeyField } from '@/components/api-key-field'

type AndroidWelcomeProps={open:boolean;apiKey:string;onKeyChange:(key:string)=>void;supported:boolean;saved:boolean;busy:boolean;onSkip:()=>void;onStart:()=>void}

export function AndroidWelcome({open,apiKey,onKeyChange,supported,saved,busy,onSkip,onStart}:AndroidWelcomeProps) {
 const titleRef=useRef<HTMLHeadingElement>(null)
 return <Dialog open={open} onOpenChange={next=>{if(!next&&!busy)onSkip()}}>
  <DialogContent className="android-welcome-dialog" showCloseButton={false} onEscapeKeyDown={event=>{if(busy)event.preventDefault()}} onInteractOutside={event=>{if(busy)event.preventDefault()}} onOpenAutoFocus={event=>{event.preventDefault();titleRef.current?.focus()}}>
   <DialogHeader><DialogTitle ref={titleRef} tabIndex={-1}>欢迎使用听记</DialogTitle><DialogDescription>先设置你的 DeepSeek API Key。也可以稍后填写，先导入录音、分析人物。</DialogDescription></DialogHeader>
   <div className="android-welcome-content">
    <ApiKeyField id="onboarding-api-key" value={apiKey} onChange={onKeyChange} supported={supported} saved={saved} disabled={busy}/>
    <div className="android-welcome-note"><ShieldCheck size={18}/><p>人物分析在手机本地完成。转写会发送音频片段，总结会发送文字到 DeepSeek，费用由你的 DeepSeek 账户承担。</p></div>
   </div>
   <DialogFooter><Button variant="outline" disabled={busy} onClick={onSkip}>稍后填写</Button><Button disabled={busy||!saved&&!apiKey.trim()} onClick={onStart}>{busy?'正在保存…':supported&&!saved?'保存并开始使用':'开始使用'}</Button></DialogFooter>
  </DialogContent>
 </Dialog>
}
