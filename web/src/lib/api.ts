export type Segment = { id: string; index: number; start: number; end: number; speaker: string; speaker_name?: string; uncertain: boolean; state: string; text: string; reviewed?: boolean; edited?: boolean }
export type Sample = { speaker: string; audio_url: string | null; start: number; end: number; uncertain?: boolean; too_short?: boolean }
export type SpeakerSummary = { speaker:string; speaker_name:string; summary:string; points:string[]; commitments:string[]; evidence:string }
export type SummaryReport = { schema_version:number; model:string; created_at:string; input_fingerprint:string; coverage:{done_count:number;total_count:number;full:boolean}; content:{overview:string;speaker_summaries?:SpeakerSummary[];topics:{title:string;summary:string}[];decisions:string[];open_questions:string[];todos:{task:string;owner:string;deadline:string;evidence:string}[]} }
export type AiSummary = { status:'empty'|'running'|'complete'|'error'|'cancelled';message?:string;progress?:{stage:string;current:number;total:number;message:string};stale?:boolean;needs_speaker_summary?:boolean;report?:SummaryReport }
export type Job = { id: string; title: string; source_path?: string; duration_seconds?: number; speaker_count?: number; updated_at?: string; status?: string; old_version?: boolean; speaker_names?: Record<string,string>; warnings?: string[]; audio_url?: string; samples?: Sample[]; segments?: Segment[]; turns?: Segment[]; complete?: boolean; exports?: Record<string,{available:boolean}>;ai_summary?:AiSummary }
export type Settings = { num_speakers: number; output_root: string; hourly_price: number; max_workers: number; retry_uncertain: boolean; api_key_prompt_seen?:boolean }
export type AppState = { version: string; source: { path:string; name:string; duration_seconds:number; channels?:number; audio_url?:string } | null; settings:Settings; jobs:Job[]; selected_job: Job|null; task: { running:boolean; stage:string; current:number; total:number; message:string; cancel_requested?:boolean }; logs:{time:string;message:string}[] }
import { isAndroidApp, nativeRequest } from './android-bridge'

declare global { interface Window { __MEETING_TOKEN__?:string } }
export async function api(path:string, body?:unknown):Promise<AppState> {
 if(isAndroidApp()) return nativeRequest<AppState>(path,body)
 const response = await fetch(`/api/${path}`, { method:body===undefined?'GET':'POST', credentials:'same-origin', headers:{'X-Meeting-Token':window.__MEETING_TOKEN__||'', ...(body===undefined?{}:{'Content-Type':'application/json'})}, ...(body===undefined?{}:{body:JSON.stringify(body)}) })
 const result = await response.json()
 if(!response.ok) throw new Error(result.message || `操作未完成（${response.status}）`)
 return result
}
export function clock(value=0, full=false) { const s=Math.max(0,Math.floor(value)); const h=Math.floor(s/3600); return `${h||full?String(h).padStart(2,'0')+':':''}${String(Math.floor(s/60)%60).padStart(2,'0')}:${String(s%60).padStart(2,'0')}` }
export function speakerLabel(id:string,names?:Record<string,string>) { return names?.[id] || `说话人 ${id.replace(/^S/,'')}` }
