import { useEffect, useState } from 'react'
import { Eye, EyeOff, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

type ApiKeyFieldProps={id:string;value:string;onChange:(value:string)=>void;supported:boolean;saved:boolean;disabled:boolean;onClear?:()=>void}

/** Only the user's replacement draft enters this field, never the saved secret. */
export function ApiKeyField({id,value,onChange,supported,saved,disabled,onClear}:ApiKeyFieldProps){
 const [visible,setVisible]=useState(false)
 useEffect(()=>{if(!value)setVisible(false)},[value])
 return <div className="api-key-field">
  {supported&&saved&&<div className="api-key-saved-state"><span role="status"><ShieldCheck size={16} aria-hidden="true"/><strong>已保存</strong></span>{onClear&&<Button type="button" size="sm" variant="outline" disabled={disabled} onClick={onClear}>清除 Key</Button>}</div>}
  <label htmlFor={id} className="field-label">{supported&&saved?'替换 API Key（留空保留）':'DeepSeek API Key'}</label>
  <div className="password-field"><Input id={id} type={visible?'text':'password'} value={value} onChange={event=>onChange(event.target.value)} disabled={disabled} autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder={supported&&saved?'粘贴新的 Key 以替换':'粘贴你自己的 DeepSeek API Key'}/><button type="button" disabled={disabled} aria-label={visible?'隐藏 API Key':'显示 API Key'} onClick={()=>setVisible(!visible)}>{visible?<EyeOff size={16}/>:<Eye size={16}/>}</button></div>
  <p className="field-hint">{supported?(saved?'留空会继续使用已保存的 Key。输入新 Key 并保存后才会替换。':'确认保存后，下次打开无需重复填写。已保存的 Key 不会显示在输入框中。'):'当前服务版本仅支持本次打开使用，关闭后需重新填写。'}</p>
 </div>
}
