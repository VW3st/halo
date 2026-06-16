// Web Audio API singleton — mic input -> AnalyserNode. Components and the
// R3F core read the live time/frequency buffers directly inside their rAF /
// useFrame loops (NOT through React state) so 60fps visuals never trigger
// re-renders. The store only mirrors the slow scalar level/voiceActivity.
import { useHaloStore } from '../store/voiceHarnessStore'

class AudioAnalyser {
  ctx: AudioContext | null = null
  analyser: AnalyserNode | null = null
  stream: MediaStream | null = null
  timeData: Uint8Array | null = null
  freqData: Uint8Array | null = null
  micLevel = 0
  voiceActivity = 0
  micDb = -Infinity
  access = false

  async enable(fromGesture = false): Promise<void> {
    try {
      if (!this.ctx) this.ctx = new (window.AudioContext || (window as any).webkitAudioContext)()
      if (this.ctx.state === 'suspended') {
        try { await this.ctx.resume() } catch { /* ignore */ }
      }
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      })
      const src = this.ctx.createMediaStreamSource(this.stream)
      this.analyser = this.ctx.createAnalyser()
      this.analyser.fftSize = 2048
      this.analyser.smoothingTimeConstant = 0.6
      src.connect(this.analyser)
      this.freqData = new Uint8Array(this.analyser.frequencyBinCount)
      this.timeData = new Uint8Array(this.analyser.fftSize)
      this.access = true
      useHaloStore.getState().setMic({ access: true, denied: false })
    } catch (e: any) {
      this.access = false
      const denied = e && e.name === 'NotAllowedError'
      useHaloStore.getState().setMic({ access: false, denied, error: e?.name || 'error' })
    }
  }

  // Pull fresh samples + recompute scalars. Called once per animation frame
  // by the render loop owner.
  sample(): void {
    if (!this.analyser || !this.timeData || !this.freqData) return
    // casts: lib.dom 5.7 narrows the arg to Uint8Array<ArrayBuffer>; our
    // buffers are plain Uint8Array — runtime-identical, type-only mismatch.
    this.analyser.getByteTimeDomainData(this.timeData as any)
    this.analyser.getByteFrequencyData(this.freqData as any)
    let sum = 0
    for (let i = 0; i < this.timeData.length; i++) {
      const v = (this.timeData[i] - 128) / 128
      sum += v * v
    }
    const rms = Math.sqrt(sum / this.timeData.length)
    this.micLevel += (rms - this.micLevel) * 0.4
    this.micDb = this.micLevel > 0.001 ? 20 * Math.log10(this.micLevel) : -Infinity
    let hi = 0
    for (let i = 5; i < 80; i++) if (this.freqData[i] > 60) hi++
    const speech = Math.min(Math.max((rms - 0.005) * 50, 0), 1) * Math.min(hi / 30, 1)
    this.voiceActivity += (speech - this.voiceActivity) * 0.25
  }
}

export const audio = new AudioAnalyser()
