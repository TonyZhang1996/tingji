import { Loader2 } from 'lucide-react'
import { Checkbox } from '@/components/ui/checkbox'

export type VoiceEnhancementOptionProps={enabled:boolean;pending:boolean;locked:boolean;disabled:boolean;onChange:(enabled:boolean)=>void}

/** The checked value comes from the server; a rejected change keeps its prior value. */
export function VoiceEnhancementOption({enabled,pending,locked,disabled,onChange}:VoiceEnhancementOptionProps) {
 return <div className="voice-enhancement-option" aria-busy={pending||undefined}>
  <label className="voice-enhancement-label" htmlFor="voice-enhancement">
   <Checkbox id="voice-enhancement" checked={enabled} disabled={disabled||locked} onCheckedChange={checked=>onChange(checked===true)} aria-describedby="voice-enhancement-description voice-enhancement-state"/>
   <strong>人声增强</strong>
   {pending&&<Loader2 size={14} className="spin" aria-hidden="true"/>}
   <span id="voice-enhancement-state" aria-live="polite">{pending?'正在保存…':enabled?'已开启':'未开启'}</span>
  </label>
  <p id="voice-enhancement-description">本机减轻底噪、抬高较轻的声音。处理后的音频用于转写，回听和人物分析用原音。</p>
  {locked&&<p className="voice-enhancement-locked">当前任务的转写音频模式已固定。</p>}
 </div>
}
