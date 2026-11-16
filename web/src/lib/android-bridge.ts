/** Only the packaged Android WebView supplies this bridge. No secrets are stored. */
type NativeReply<T> = { ok:true; state:T } | { ok:false; message?:string }
type Pending = { resolve:(value:unknown)=>void; reject:(error:Error)=>void; timer:ReturnType<typeof setTimeout> }

declare global {
 interface Window {
  TingjiAndroid?: { postMessage:(message:string)=>void }
  __TINGJI_ANDROID_RESULT__?: (id:string,reply:NativeReply<unknown>)=>void
  __TINGJI_ANDROID_BACK__?: ()=>boolean
  __TINGJI_ANDROID_REFRESH__?: ()=>void
 }
}

const pending=new Map<string,Pending>()
let requestNumber=0
export const isAndroidApp=()=>typeof window!=='undefined'&&typeof window.TingjiAndroid?.postMessage==='function'

if(typeof window!=='undefined') {
 if(isAndroidApp()) document.documentElement.classList.add('android-app')
 window.__TINGJI_ANDROID_RESULT__=(id,reply)=>{
  const call=pending.get(String(id))
  if(!call)return
  clearTimeout(call.timer);pending.delete(String(id))
  if(reply?.ok===true)call.resolve(reply.state)
  else call.reject(new Error(reply?.ok===false&&reply.message?reply.message:'操作未完成，请查看处理记录。'))
 }
 window.__TINGJI_ANDROID_REFRESH__=()=>window.dispatchEvent(new Event('tingji:refresh'))
 window.addEventListener('pagehide',()=>{
  for(const call of pending.values()){clearTimeout(call.timer);call.reject(new Error('窗口已关闭，任务进度保存在手机中。'))}
  pending.clear()
 })
}

export function nativeRequest<T>(action:string,payload?:unknown):Promise<T> {
 const bridge=window.TingjiAndroid
 if(!bridge)return Promise.reject(new Error('手机服务尚未就绪，请重新打开听记。'))
 const id=`${Date.now()}-${++requestNumber}`
 return new Promise<T>((resolve,reject)=>{
  const timer=setTimeout(()=>{
   pending.delete(id)
   reject(new Error('操作响应超时，任务可能仍在后台运行，请先查看进度，避免重复提交。'))
  },action==='choose_audio'?30*60_000:action==='state'?20_000:60_000)
  pending.set(id,{resolve:value=>resolve(value as T),reject,timer})
  try { bridge.postMessage(JSON.stringify({id,action,...(payload===undefined?{}:{payload})})) }
  catch {clearTimeout(timer);pending.delete(id);reject(new Error('无法连接手机服务，请重新打开听记。'))}
 })
}

export async function copyToClipboard(text:string):Promise<void> {
 if(isAndroidApp()){await nativeRequest('copy_text',{text});return}
 await navigator.clipboard.writeText(text)
}
