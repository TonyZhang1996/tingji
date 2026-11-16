export type SegmentPlayback = {
  play(start: number, end: number): Promise<void>
  /** Clear pending work and the segment boundary, retaining normal playback. */
  cancel(): void
  dispose(): void
}

type Request = { controller: AbortController; source: string; deadline: number; target: number | null }
class Superseded extends Error {}

/** Seek against the loaded media timeline before starting a bounded preview. */
export function createSegmentPlayback(audio: HTMLAudioElement, options: { timeoutMs?: number } = {}): SegmentPlayback {
  const timeoutMs = Math.max(1, options.timeoutMs ?? 12000)
  let request: Request | null = null
  let boundary: { start: number; end: number; source: string } | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false
  const source = () => audio.getAttribute('src') || audio.currentSrc || ''
  const clearTimer = () => { if (timer !== undefined) clearTimeout(timer); timer = undefined }

  function cancel() {
    request?.controller.abort()
    request = null
    boundary = null
    clearTimer()
  }
  function active(item: Request) {
    if (disposed || request !== item || item.controller.signal.aborted || source() !== item.source) throw new Superseded()
  }
  function checkBoundary() {
    clearTimer()
    const current = boundary
    if (!current) return
    if (source() !== current.source) { cancel(); return }
    if (audio.currentTime >= current.end) {
      boundary = null
      audio.pause()
      // Keep the playhead at the selected end even after a throttled event.
      audio.currentTime = current.end
    } else if (!audio.paused && !audio.ended) {
      const rate = Number.isFinite(audio.playbackRate) && audio.playbackRate > 0 ? audio.playbackRate : 1
      timer = setTimeout(checkBoundary, Math.max(10, Math.min(100, (current.end - audio.currentTime) * 1000 / rate)))
    }
  }
  function externalSeek() {
    // A seek initiated by this controller is part of preparing the preview.
    // All other seeks return the player to unrestricted full-audio mode.
    if (request?.target !== null && request?.target !== undefined && Math.abs(audio.currentTime - request.target) <= .05) return
    cancel()
  }
  function sourceChanged() {
    if (request && source() !== request.source || boundary && source() !== boundary.source) cancel()
  }
  function mediaError() {
    boundary = null
    clearTimer()
  }
  function waitUntil(item: Request, predicate: () => boolean, events: string[]): Promise<void> {
    return new Promise((resolve, reject) => {
      let deadline: ReturnType<typeof setTimeout> | undefined
      const cleanup = () => {
        if (deadline !== undefined) clearTimeout(deadline)
        for (const event of events) audio.removeEventListener(event, inspect)
        audio.removeEventListener('error', failed)
        item.controller.signal.removeEventListener('abort', aborted)
      }
      const finish = (error?: Error) => { cleanup(); error ? reject(error) : resolve() }
      const inspect = () => {
        try { active(item); if (predicate()) finish() } catch (error) { finish(error as Error) }
      }
      const failed = () => finish(new Error('录音读取失败，无法回听这一段。'))
      const aborted = () => finish(new Superseded())
      for (const event of events) audio.addEventListener(event, inspect)
      audio.addEventListener('error', failed)
      item.controller.signal.addEventListener('abort', aborted, { once: true })
      deadline = setTimeout(() => finish(new Error('录音定位超时，请稍后再试。')), Math.max(0, item.deadline - Date.now()))
      inspect()
    })
  }
  function awaitPlay(item: Request, playing: Promise<void>): Promise<void> {
    return new Promise((resolve, reject) => {
      const cleanup = () => { clearTimeout(deadline); item.controller.signal.removeEventListener('abort', aborted) }
      const aborted = () => { cleanup(); reject(new Superseded()) }
      const deadline = setTimeout(() => { cleanup(); reject(new Error('录音播放超时，请稍后再试。')) }, Math.max(0, item.deadline - Date.now()))
      item.controller.signal.addEventListener('abort', aborted, { once: true })
      playing.then(() => { cleanup(); resolve() }, error => { cleanup(); reject(error) })
      if (item.controller.signal.aborted) aborted()
    })
  }
  async function play(start: number, end: number) {
    if (disposed) throw new Error('播放器已关闭。')
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start) throw new RangeError('片段的起止时间无效。')
    cancel()
    audio.pause()
    const item: Request = { controller: new AbortController(), source: source(), deadline: Date.now() + timeoutMs, target: null }
    if (!item.source) throw new Error('用于回听的音频暂不可用。')
    request = item
    try {
      await waitUntil(item, () => audio.readyState >= 1 && Number.isFinite(audio.duration) && audio.duration > 0,
        ['loadedmetadata', 'durationchange', 'canplay'])
      active(item)
      const segmentEnd = Math.min(end, audio.duration)
      if (start >= segmentEnd) throw new RangeError('这一段超出了当前录音的可播放范围。')
      item.target = start
      audio.currentTime = start
      await waitUntil(item, () => !audio.seeking && Math.abs(audio.currentTime - start) <= .05,
        ['seeked', 'timeupdate', 'loadeddata', 'canplay'])
      active(item)
      item.target = null
      boundary = { start, end: segmentEnd, source: item.source }
      await awaitPlay(item, audio.play())
      active(item)
      request = null
      checkBoundary()
    } catch (error) {
      if (error instanceof Superseded || request !== item) return
      cancel()
      audio.pause()
      throw error
    }
  }
  const events: [string, EventListener][] = [
    ['timeupdate', checkBoundary], ['ratechange', checkBoundary], ['playing', checkBoundary],
    ['seeking', externalSeek], ['loadstart', sourceChanged], ['emptied', sourceChanged], ['error', mediaError],
  ]
  for (const [event, handler] of events) audio.addEventListener(event, handler)
  return {
    play, cancel,
    dispose() { disposed = true; cancel(); for (const [event, handler] of events) audio.removeEventListener(event, handler) },
  }
}
