import { useRef, useState } from 'react'
import { Eye, EyeOff, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'

/** Credentials belong to the parent React session, never native configuration. */
export function AndroidWelcome({open,apiKey,onKeyChange,onComplete}:{open:boolean;apiKey:string;onKeyChange:(key:string)=>void;onComplete:()=>void}) {
 const [visible,setVisible]=useState(false)
 const titleRef=useRef<HTMLHeadingElement>(null)
 return <Dialog open={open} onOpenChange={next=>{if(!next)onComplete()}}>
  <DialogContent className="android-welcome-dialog" showCloseButton={false} onOpenAutoFocus={event=>{event.preventDefault();titleRef.current?.focus()}}>
   <DialogHeader><DialogTitle ref={titleRef} tabIndex={-1}>欢迎使用听记</DialogTitle><DialogDescription>先设置你的 DeepSeek API Key。也可以稍后填写，先导入录音、分析人物。</DialogDescription></DialogHeader>
   <div className="android-welcome-content">
    <label className="field-label" htmlFor="onboarding-api-key">DeepSeek API Key</label>
    <div className="password-field"><Input id="onboarding-api-key" type={visible?'text':'password'} value={apiKey} onChange={event=>onKeyChange(event.target.value)} autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder="粘贴你自己的 API Key"/><button type="button" aria-label={visible?'隐藏 API Key':'显示 API Key'} onClick={()=>setVisible(!visible)}>{visible?<EyeOff size={18}/>:<Eye size={18}/>}</button></div>
    <p className="field-hint">Key 仅在本次打开期间使用，关闭应用后需要重新填写，不会保存到手机配置或安装包。</p>
    <div className="android-welcome-note"><ShieldCheck size={18}/><p>人物分析在手机本地完成。转写会发送音频片段，总结会发送文字到 DeepSeek，费用由你的 DeepSeek 账户承担。</p></div>
   </div>
   <DialogFooter><Button variant="outline" onClick={onComplete}>稍后填写</Button><Button disabled={!apiKey.trim()} onClick={onComplete}>开始使用</Button></DialogFooter>
  </DialogContent>
 </Dialog>
}
