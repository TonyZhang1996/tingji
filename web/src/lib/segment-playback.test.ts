import assert from 'node:assert/strict'
import test from 'node:test'
import { createSegmentPlayback } from './segment-playback.ts'

class FakeAudio extends EventTarget {
  readyState = 0
  duration = Number.NaN
  currentSrc = ''
  src = 'https://appassets.androidplatform.net/media/synthetic'
  seeking = false
  paused = true
  ended = false
  playbackRate = 1
  seekDelay = 3
  playCalls: { time: number; seeking: boolean; ready: number }[] = []
  private time = 0
  private seekTimer?: ReturnType<typeof setTimeout>
  getAttribute(name: string) { return name === 'src' ? this.src : null }
  get currentTime() { return this.time }
  set currentTime(value: number) {
    assert.ok(this.readyState >= 1, 'Seeking before metadata must never occur')
    clearTimeout(this.seekTimer)
    this.time = value
    this.seeking = true
    this.dispatchEvent(new Event('seeking'))
    this.seekTimer = setTimeout(() => { this.seeking = false; this.dispatchEvent(new Event('seeked')) }, this.seekDelay)
  }
  metadata(duration = 100) { this.readyState = 1; this.duration = duration; this.dispatchEvent(new Event('loadedmetadata')) }
  advance(value: number) { this.time = value; this.dispatchEvent(new Event('timeupdate')) }
  pause() { this.paused = true; this.dispatchEvent(new Event('pause')) }
  async play() {
    this.playCalls.push({ time: this.time, seeking: this.seeking, ready: this.readyState })
    this.paused = false
    this.dispatchEvent(new Event('playing'))
  }
  close() { clearTimeout(this.seekTimer) }
  element() { return this as unknown as HTMLAudioElement }
}
const tick = () => new Promise(resolve => setTimeout(resolve, 12))

test('a segment waits for metadata and completed seek before starting', async () => {
  const audio = new FakeAudio()
  const playback = createSegmentPlayback(audio.element(), { timeoutMs: 1000 })
  try {
    const playing = playback.play(23, 25)
    await tick(); assert.equal(audio.playCalls.length, 0)
    audio.metadata()
    await playing
    assert.deepEqual(audio.playCalls, [{ time: 23, seeking: false, ready: 1 }])
  } finally { playback.dispose(); audio.close() }
})

test('rapid clicks take the newest segment without a stale play', async () => {
  const audio = new FakeAudio()
  const playback = createSegmentPlayback(audio.element())
  try {
    const first = playback.play(10, 12)
    const second = playback.play(40, 43)
    audio.metadata()
    await Promise.all([first, second])
    assert.deepEqual(audio.playCalls.map(call => call.time), [40])
    audio.seekDelay = 20
    const third = playback.play(60, 63)
    await tick()
    const fourth = playback.play(80, 83)
    await Promise.all([third, fourth])
    assert.deepEqual(audio.playCalls.map(call => call.time), [40, 80])
  } finally { playback.dispose(); audio.close() }
})

test('preview pauses at its end and manual full-audio mode clears that limit', async () => {
  const audio = new FakeAudio(); audio.metadata()
  const playback = createSegmentPlayback(audio.element())
  try {
    await playback.play(20, 22)
    audio.advance(22.2)
    assert.equal(audio.paused, true); assert.equal(audio.currentTime, 22)
    await playback.play(40, 42)
    playback.cancel()
    assert.equal(audio.paused, false)
    audio.advance(45)
    assert.equal(audio.paused, false)
  } finally { playback.dispose(); audio.close() }
})

test('an external seek clears the old segment limit', async () => {
  const audio = new FakeAudio(); audio.metadata()
  const playback = createSegmentPlayback(audio.element())
  try {
    await playback.play(20, 22)
    audio.currentTime = 60
    await tick(); audio.advance(61)
    assert.equal(audio.paused, false)
  } finally { playback.dispose(); audio.close() }
})

test('metadata or seek timeout never starts playback at the old position', async () => {
  const audio = new FakeAudio()
  const playback = createSegmentPlayback(audio.element(), { timeoutMs: 20 })
  try {
    await assert.rejects(playback.play(20, 22), /超时/)
    assert.equal(audio.playCalls.length, 0)
    audio.metadata(); audio.seekDelay = 200
    await assert.rejects(playback.play(40, 43), /超时/)
    assert.equal(audio.playCalls.length, 0); assert.equal(audio.paused, true)
  } finally { playback.dispose(); audio.close() }
})

test('changing source or disposing a pending preview cannot start old audio', async () => {
  const audio = new FakeAudio()
  let playback = createSegmentPlayback(audio.element())
  try {
    const old = playback.play(10, 12)
    audio.src = 'https://appassets.androidplatform.net/media/other'
    audio.dispatchEvent(new Event('loadstart'))
    audio.metadata(); await old
    assert.equal(audio.playCalls.length, 0)
    playback.dispose()
    playback = createSegmentPlayback(audio.element())
    const pending = playback.play(20, 22)
    playback.dispose(); await pending; await tick()
    assert.equal(audio.playCalls.length, 0)
  } finally { playback.dispose(); audio.close() }
})

test('invalid and out-of-media bounds are rejected without playback', async () => {
  const audio = new FakeAudio(); audio.metadata(30)
  const playback = createSegmentPlayback(audio.element())
  try {
    for (const [start, end] of [[-1, 3], [5, 5], [Number.NaN, 6], [31, 33]]) {
      await assert.rejects(playback.play(start, end), RangeError)
    }
    assert.equal(audio.playCalls.length, 0)
    await playback.play(28, 35)
    audio.advance(30.1)
    assert.equal(audio.paused, true); assert.equal(audio.currentTime, 30)
  } finally { playback.dispose(); audio.close() }
})
