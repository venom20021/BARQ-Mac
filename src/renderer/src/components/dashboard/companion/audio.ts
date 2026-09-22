/**
 * audio.ts — sound effects and (opt-in) microphone analysis for the companion.
 *
 * Ported from the Lovable reference unchanged in behaviour.
 *
 * IMPORTANT — why the mic analyser is opt-in here:
 * BARQ's voice agent (Deepgram / Gemini Live) already opens its own microphone
 * stream while a conversation is active. Opening a SECOND stream for visual
 * metering is what makes "mute" ambiguous — the OS mic indicator stays hot
 * because a stream we control is still live. So the companion drives its mic
 * level from BARQ's real voice state by default, and only attaches a real
 * analyser when the user explicitly turns "mic reactive" on in the HUD.
 */

let ctx: AudioContext | null = null

export function getAudioContext(): AudioContext | null {
  if (typeof window === 'undefined') return null
  if (!ctx) {
    const Ctor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!Ctor) return null
    ctx = new Ctor()
  }
  if (ctx.state === 'suspended') void ctx.resume()
  return ctx
}

/** Soft sci-fi blip used for state transitions. */
export function playTone(from: number, to: number, duration = 0.28, gain = 0.06): void {
  const ac = getAudioContext()
  if (!ac) return
  const osc = ac.createOscillator()
  const g = ac.createGain()
  const filter = ac.createBiquadFilter()
  filter.type = 'lowpass'
  filter.frequency.value = 2600
  osc.type = 'sine'
  const now = ac.currentTime
  osc.frequency.setValueAtTime(from, now)
  osc.frequency.exponentialRampToValueAtTime(to, now + duration)
  g.gain.setValueAtTime(0.0001, now)
  g.gain.exponentialRampToValueAtTime(gain, now + 0.02)
  g.gain.exponentialRampToValueAtTime(0.0001, now + duration)
  osc.connect(filter).connect(g).connect(ac.destination)
  osc.start(now)
  osc.stop(now + duration + 0.05)
}

export const sfx = {
  listenOn: () => playTone(420, 940, 0.22),
  listenOff: () => playTone(820, 320, 0.22),
  thinking: () => playTone(300, 620, 0.35, 0.04),
  reply: () => playTone(660, 1180, 0.3, 0.05),
}

export class MicAnalyser {
  private stream: MediaStream | null = null
  private analyser: AnalyserNode | null = null
  private data: Uint8Array<ArrayBuffer> | null = null
  private source: MediaStreamAudioSourceNode | null = null

  async start(): Promise<void> {
    const ac = getAudioContext()
    if (!ac || this.stream) return
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    this.source = ac.createMediaStreamSource(this.stream)
    this.analyser = ac.createAnalyser()
    this.analyser.fftSize = 512
    this.analyser.smoothingTimeConstant = 0.7
    this.data = new Uint8Array(new ArrayBuffer(this.analyser.frequencyBinCount))
    this.source.connect(this.analyser)
  }

  /** 0..1 loudness */
  level(): number {
    if (!this.analyser || !this.data) return 0
    this.analyser.getByteFrequencyData(this.data)
    let sum = 0
    for (let i = 0; i < this.data.length; i++) sum += this.data[i]!
    const avg = sum / this.data.length / 255
    return Math.min(1, Math.pow(avg * 2.6, 1.3))
  }

  /** spectrum bands for the HUD waveform */
  bands(count = 28): number[] {
    const out = new Array<number>(count).fill(0)
    if (!this.analyser || !this.data) return out
    const step = Math.floor(this.data.length / count)
    for (let i = 0; i < count; i++) {
      let sum = 0
      for (let j = 0; j < step; j++) sum += this.data[i * step + j]!
      out[i] = Math.min(1, sum / step / 190)
    }
    return out
  }

  stop(): void {
    this.stream?.getTracks().forEach((t) => t.stop())
    this.source?.disconnect()
    this.stream = null
    this.analyser = null
    this.source = null
    this.data = null
  }
}
