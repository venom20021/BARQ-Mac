/**
 * CompanionDashboard.tsx — BARQ's particle companion view.
 *
 * Replaces the humanoid dashboard. The visual layer is the ported Lovable
 * "particle AI companion" scene; the behaviour layer is rewired to BARQ's real
 * stack:
 *
 *   reference (demo)                     BARQ (this file)
 *   ─────────────────────────────────    ──────────────────────────────────────
 *   webkitSpeechRecognition              BARQ's own detector + Deepgram/Gemini
 *   speechSynthesis + random envelope    BARQ's real TTS, level follows responseText
 *   Lovable Cloud LLM via useServerFn    BARQ's sidecar conversation (no second brain)
 *   its own getUserMedia stream          opt-in only (see audio.ts) — the voice
 *                                        agent already owns the microphone
 *
 * Status is a pure projection of useVoice().aiState, so the companion can never
 * disagree with the rest of the app about whether BARQ is listening.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ChevronDown,
  Loader2,
  Mic,
  MicOff,
  RotateCcw,
  Volume2,
  VolumeX,
  Waves,
} from 'lucide-react'

import { useVoice } from '../../../contexts/VoiceContext'
import { createParticleScene, type ThemeKind, type VisualKind } from './particleScene'
import { MicAnalyser, sfx } from './audio'

// ─── State mapping ──────────────────────────────────────────────────────────

/** BARQ's AIState -> the scene's mode uniform. Order is load-bearing. */
const MODE_VALUE = { idle: 0, listening: 1, thinking: 2, responding: 3 } as const

const STATUS_LABEL = {
  idle: 'STANDBY',
  listening: 'LISTENING',
  thinking: 'THINKING',
  responding: 'SPEAKING',
} as const

/** Token role: cyan = at rest, violet = thinking, plasma = speaking.
 *  Both classes are written out as literals — Tailwind's JIT cannot see a
 *  class name built at runtime with .replace(), so the dot would render empty. */
const STATUS_TONE = {
  idle: { text: 'text-cyan-300/50', dot: 'bg-cyan-300/50' },
  listening: { text: 'text-cyan-300', dot: 'bg-cyan-300' },
  thinking: { text: 'text-holographic-400', dot: 'bg-holographic-400' },
  responding: { text: 'text-plasma-400', dot: 'bg-plasma-400' },
} as const

const VISUALS: { id: VisualKind; label: string }[] = [
  { id: 'sphere', label: 'contour sphere' },
  { id: 'node', label: 'neural node' },
  { id: 'vortex', label: 'energy vortex' },
  { id: 'iris', label: 'holographic iris' },
  { id: 'reactor', label: 'arc reactor' },
  { id: 'singularity', label: 'singularity' },
  { id: 'gyro', label: 'gyroscope' },
]

const THEME_LIST: { id: ThemeKind; label: string; dot: string }[] = [
  { id: 'barq', label: 'barq', dot: '#00f0ff' },
  { id: 'aura', label: 'aura', dot: '#40b8ff' },
  { id: 'ultron', label: 'ultron', dot: '#ff3a18' },
  { id: 'jarvis', label: 'jarvis', dot: '#8fe4ff' },
  { id: 'matrix', label: 'matrix', dot: '#14ff7a' },
  { id: 'eve', label: 'eve', dot: '#ffffff' },
]

const STORE_KEY = 'barq_companion_v1'

type Custom = { core: string; edge: string; accent: string; ring: string }
type Calib = { micGain: number; spike: number; speakBright: number; intensity: number }

type Saved = {
  visual: VisualKind
  theme: ThemeKind
  brightness: number
  customOn: boolean
  custom: Custom
  calib: Calib
}

const DEFAULT_CUSTOM: Custom = {
  core: '#ff6b35',
  edge: '#00f0ff',
  accent: '#a855f7',
  ring: '#00e6e6',
}
const DEFAULT_CALIB: Calib = { micGain: 1, spike: 1, speakBright: 1, intensity: 1 }

const COLOR_FIELDS: { key: keyof Custom; label: string }[] = [
  { key: 'core', label: 'core accent' },
  { key: 'edge', label: 'sphere edge' },
  { key: 'accent', label: 'mood accent' },
  { key: 'ring', label: 'rings' },
]

const CALIB_FIELDS: { key: keyof Calib; label: string; min: number; max: number }[] = [
  { key: 'micGain', label: 'pulse sensitivity', min: 0.2, max: 3 },
  { key: 'spike', label: 'spike intensity', min: 0, max: 2.5 },
  { key: 'speakBright', label: 'speaking brightness', min: 0.2, max: 2 },
]

const hexNum = (hex: string) => parseInt(hex.replace('#', ''), 16) || 0

const loadSaved = (): Saved => {
  const fallback: Saved = {
    visual: 'sphere',
    theme: 'barq',
    brightness: 1,
    customOn: false,
    custom: DEFAULT_CUSTOM,
    calib: DEFAULT_CALIB,
  }
  if (typeof window === 'undefined') return fallback
  try {
    const raw = window.localStorage.getItem(STORE_KEY)
    if (!raw) return fallback
    const parsed = JSON.parse(raw) as Partial<Saved>
    return {
      visual: VISUALS.some((v) => v.id === parsed.visual) ? parsed.visual! : fallback.visual,
      theme: THEME_LIST.some((t) => t.id === parsed.theme) ? parsed.theme! : fallback.theme,
      brightness:
        typeof parsed.brightness === 'number' && parsed.brightness >= 0.2 && parsed.brightness <= 2
          ? parsed.brightness
          : fallback.brightness,
      customOn: !!parsed.customOn,
      custom: { ...DEFAULT_CUSTOM, ...(parsed.custom ?? {}) },
      calib: { ...DEFAULT_CALIB, ...(parsed.calib ?? {}) },
    }
  } catch {
    return fallback
  }
}

const BAND_COUNT = 28

/** Shape a level into a believable spectrum without inventing audio data. */
const shapeBands = (level: number, tilt: number): number[] => {
  const out = new Array<number>(BAND_COUNT).fill(0)
  if (level <= 0) return out
  for (let i = 0; i < BAND_COUNT; i++) {
    const falloff = 1 - Math.abs(i / (BAND_COUNT - 1) - tilt) * 1.15
    out[i] = Math.max(0, Math.min(1, level * Math.max(0, falloff)))
  }
  return out
}

// ─── Component ──────────────────────────────────────────────────────────────

export function CompanionDashboard(): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const sceneRef = useRef<ReturnType<typeof createParticleScene> | null>(null)
  const micRef = useRef<MicAnalyser | null>(null)
  const frameRef = useRef(0)

  // Live energy decays; a real transcript update re-arms it.
  const micEnergy = useRef(0)
  const voiceEnergy = useRef(0)

  // Preferences are read from storage once, as lazy state initialisers — there is
  // no restore effect, so nothing cascades on mount.
  const saved = useMemo(() => loadSaved(), [])
  const [visual, setVisual] = useState<VisualKind>(saved.visual)
  const [theme, setTheme] = useState<ThemeKind>(saved.theme)
  const [brightness, setBrightness] = useState(saved.brightness)
  const [customOn, setCustomOn] = useState(saved.customOn)
  const [custom, setCustom] = useState<Custom>(saved.custom)
  const [calib, setCalib] = useState<Calib>(saved.calib)

  const [bands, setBands] = useState<number[]>(() => new Array(BAND_COUNT).fill(0))
  const [transcriptOpen, setTranscriptOpen] = useState(false)
  const [muted, setMuted] = useState(false)
  const [micReactive, setMicReactive] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [panel, setPanel] = useState<'none' | 'theme' | 'audio'>('none')

  // ── BARQ truth ────────────────────────────────────────────────────────
  const {
    aiState,
    sttText,
    responseText,
    detectorRunning,
    voiceListening,
    wsConnected,
    isRemote,
    toggleDetector,
  } = useVoice()

  // Status is DERIVED from BARQ's aiState rather than mirrored into state: a copy
  // could drift from the app's truth, and syncing it in an effect cascades.
  const status = (aiState in MODE_VALUE ? aiState : 'idle') as keyof typeof MODE_VALUE
  // Ref copy so the rAF level pump never re-subscribes when the status changes.
  const statusRef = useRef<keyof typeof MODE_VALUE>(status)

  // Push the mode uniform into the scene — an external system, which is what
  // effects are actually for.
  useEffect(() => {
    statusRef.current = status
    if (sceneRef.current) sceneRef.current.levels.mode = MODE_VALUE[status]
  }, [status])

  // Real conversation activity re-arms the energy envelopes.
  useEffect(() => {
    if (sttText) micEnergy.current = 1
  }, [sttText])
  useEffect(() => {
    if (responseText) voiceEnergy.current = 1
  }, [responseText])

  // ── Scene lifecycle ───────────────────────────────────────────────────
  useEffect(() => {
    if (!canvasRef.current) return
    let scene: ReturnType<typeof createParticleScene> | null = null
    try {
      scene = createParticleScene(canvasRef.current, { theme: 'barq', visual: 'sphere' })
    } catch (error) {
      // No WebGL (or a lost context): keep the HUD usable rather than taking the
      // whole view down through the error boundary.
      console.error('[Companion] scene init failed:', error)
      // One-shot result of a failed external-system init — it cannot cascade,
      // since the notice only renders a banner.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setNotice('WebGL unavailable — companion visuals are disabled.')
      return
    }
    sceneRef.current = scene
    return () => {
      scene?.dispose()
      sceneRef.current = null
    }
  }, [])

  // Persist preferences whenever they change (the first run writes back what was
  // just read — harmless and keeps the shape canonical).
  useEffect(() => {
    try {
      window.localStorage.setItem(
        STORE_KEY,
        JSON.stringify({ visual, theme, brightness, customOn, custom, calib }),
      )
    } catch {
      /* storage unavailable */
    }
  }, [visual, theme, brightness, customOn, custom, calib])

  useEffect(() => {
    sceneRef.current?.setVisual(visual)
  }, [visual])

  useEffect(() => {
    const scene = sceneRef.current
    if (!scene) return
    if (customOn) {
      scene.setCustomTheme(
        {
          core: hexNum(custom.core),
          edge: hexNum(custom.edge),
          accent: hexNum(custom.accent),
          ring: hexNum(custom.ring),
          peak: hexNum(custom.core),
          dustA: hexNum(custom.edge),
        },
        theme,
      )
    } else {
      scene.setTheme(theme)
    }
  }, [theme, customOn, custom])

  useEffect(() => {
    sceneRef.current?.setBrightness(brightness)
  }, [brightness])

  useEffect(() => {
    sceneRef.current?.setCalibration(calib)
  }, [calib])

  // ── Level pump ────────────────────────────────────────────────────────
  // Drives scene.levels.mic / .voice. Uses the opt-in analyser when armed,
  // otherwise an envelope re-armed by real transcript updates — never a second
  // microphone stream by default.
  useEffect(() => {
    let raf = 0
    let tilt = 0.5
    const tick = () => {
      raf = requestAnimationFrame(tick)
      const scene = sceneRef.current
      const analyser = micRef.current
      const speak = statusRef.current === 'responding'
      const listen = statusRef.current === 'listening'

      if (analyser) {
        const level = analyser.level()
        if (scene) {
          scene.levels.mic = level
          scene.levels.voice = speak ? 0.35 + Math.random() * 0.5 : 0
        }
        if (++frameRef.current % 3 === 0) setBands(analyser.bands(BAND_COUNT))
        return
      }

      micEnergy.current *= 0.93
      voiceEnergy.current *= 0.955
      const mic = listen ? Math.min(1, micEnergy.current * 0.85 + 0.1 + Math.random() * 0.06) : 0
      const voice = speak ? Math.min(1, voiceEnergy.current * 0.8 + 0.14 + Math.random() * 0.08) : 0

      if (scene) {
        scene.levels.mic = mic
        scene.levels.voice = voice
      }
      if (++frameRef.current % 3 === 0) {
        tilt = Math.max(0.2, Math.min(0.8, tilt + (Math.random() - 0.5) * 0.08))
        setBands(shapeBands(Math.max(mic, voice), tilt))
      }
    }
    tick()
    return () => cancelAnimationFrame(raf)
  }, [])

  // ── Opt-in microphone analyser ────────────────────────────────────────
  useEffect(() => {
    if (!micReactive) {
      micRef.current?.stop()
      micRef.current = null
      return
    }
    let cancelled = false
    const analyser = new MicAnalyser()
    analyser
      .start()
      .then(() => {
        if (cancelled) {
          analyser.stop()
          return
        }
        micRef.current = analyser
      })
      .catch(() => {
        if (!cancelled) {
          setMicReactive(false)
          setNotice('Microphone unavailable — falling back to voice-state reactivity.')
        }
      })
    return () => {
      cancelled = true
      analyser.stop()
      if (micRef.current === analyser) micRef.current = null
    }
  }, [micReactive])

  // ── Sound effects on real state transitions ───────────────────────────
  const prevStatus = useRef<keyof typeof MODE_VALUE>('idle')
  useEffect(() => {
    const from = prevStatus.current
    prevStatus.current = status
    if (muted || from === status) return
    if (status === 'listening') sfx.listenOn()
    else if (status === 'thinking') sfx.thinking()
    else if (status === 'responding') sfx.reply()
    else if (from === 'listening') sfx.listenOff()
  }, [status, muted])

  // ── Controls ──────────────────────────────────────────────────────────
  const handleMicToggle = useCallback(() => {
    void toggleDetector()
  }, [toggleDetector])

  const handleReemerge = useCallback(() => {
    sceneRef.current?.restartEmergence()
  }, [])

  // Cleanup the opt-in stream on unmount.
  useEffect(() => () => micRef.current?.stop(), [])

  const hasExchange = useMemo(
    () => Boolean(sttText || responseText),
    [sttText, responseText],
  )

  const tone = STATUS_TONE[status]

  return (
    <div className="absolute inset-0 overflow-hidden bg-void-900">
      {/* ── particle scene ─────────────────────────────────────────────── */}
      <canvas
        ref={canvasRef}
        className="absolute inset-0 h-full w-full touch-none cursor-grab active:cursor-grabbing"
      />
      {/* vignette — same treatment the network view uses, keeps the HUD legible */}
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            'radial-gradient(ellipse at center, transparent 38%, rgba(0,0,0,0.55) 100%)',
        }}
      />

      {/* ── HUD ────────────────────────────────────────────────────────── */}
      <div className="absolute inset-0 pointer-events-none z-10 font-mono">
        {/* corner brackets — HUD frame, not decoration: they mark the viewport */}
        <div className="absolute top-4 left-4 w-6 h-6 border-l border-t border-cyan-500/20" />
        <div className="absolute top-4 right-4 w-6 h-6 border-r border-t border-cyan-500/20" />
        <div className="absolute bottom-4 left-4 w-6 h-6 border-l border-b border-cyan-500/20" />
        <div className="absolute bottom-4 right-4 w-6 h-6 border-r border-b border-cyan-500/20" />

        {/* ── left rail: identity + scene controls ─────────────────────── */}
        <div className="absolute top-6 left-6 space-y-1">
          <div className="flex items-center gap-2">
            <span className="text-cyan-300 text-hud tracking-[0.35em] uppercase">BARQ</span>
            <span className={`w-1.5 h-1.5 rounded-full ${tone.dot} ${status === 'idle' ? 'opacity-40' : 'animate-pulse'}`} />
          </div>
          <div className="text-[9px] tracking-[0.3em] text-cyan-100/30 uppercase">
            companion · {VISUALS.find((v) => v.id === visual)?.label}
          </div>

          <div className="pointer-events-auto pt-4 flex flex-col gap-1">
            {VISUALS.map((v) => (
              <button
                key={v.id}
                onClick={() => setVisual(v.id)}
                className={`w-fit rounded-full border px-3 py-1 text-[9px] tracking-[0.25em] uppercase transition ${
                  visual === v.id
                    ? 'border-cyan-300/70 bg-cyan-300/10 text-cyan-300'
                    : 'border-cyan-300/15 text-cyan-100/35 hover:border-cyan-300/50 hover:text-cyan-100/60'
                }`}
              >
                {v.label}
              </button>
            ))}

            <div className="mt-3 flex flex-wrap gap-1.5 max-w-[13rem]">
              {THEME_LIST.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setTheme(t.id)}
                  title={t.label}
                  aria-label={`Theme ${t.label}`}
                  className={`flex items-center gap-1.5 rounded-full border px-2 py-1 text-[9px] tracking-[0.2em] uppercase transition ${
                    theme === t.id
                      ? 'border-cyan-300/70 text-cyan-300'
                      : 'border-cyan-300/15 text-cyan-100/35 hover:border-cyan-300/50'
                  }`}
                >
                  <span
                    className="inline-block w-2 h-2 rounded-full"
                    style={{ backgroundColor: t.dot, boxShadow: `0 0 6px ${t.dot}` }}
                  />
                  {t.label}
                </button>
              ))}
            </div>

            <label className="mt-3 flex w-40 flex-col gap-1 text-[9px] tracking-[0.25em] text-cyan-100/35 uppercase">
              brightness
              <input
                type="range"
                min={0.3}
                max={1.8}
                step={0.05}
                value={brightness}
                onChange={(e) => setBrightness(Number(e.target.value))}
                className="h-1 w-full cursor-pointer appearance-none rounded-full bg-cyan-300/25 accent-cyan-300"
              />
            </label>

            <div className="mt-3 flex gap-1.5">
              {(['theme', 'audio'] as const).map((p) => (
                <button
                  key={p}
                  onClick={() => setPanel((cur) => (cur === p ? 'none' : p))}
                  className={`rounded-full border px-2.5 py-1 text-[9px] tracking-[0.2em] uppercase transition ${
                    panel === p
                      ? 'border-cyan-300/70 text-cyan-300'
                      : 'border-cyan-300/15 text-cyan-100/35 hover:border-cyan-300/50'
                  }`}
                >
                  {p === 'theme' ? 'theme editor' : 'audio tuning'}
                </button>
              ))}
            </div>

            {panel === 'theme' && (
              <div className="mt-2 w-52 space-y-2 rounded-xl border border-cyan-300/20 bg-void-900/80 p-3 backdrop-blur-md">
                <label className="flex items-center justify-between text-[9px] tracking-[0.2em] text-cyan-100/40 uppercase">
                  custom colors
                  <input
                    type="checkbox"
                    checked={customOn}
                    onChange={(e) => setCustomOn(e.target.checked)}
                    className="accent-cyan-300"
                  />
                </label>
                {COLOR_FIELDS.map((f) => (
                  <label
                    key={f.key}
                    className="flex items-center justify-between text-[9px] tracking-[0.2em] text-cyan-100/40 uppercase"
                  >
                    {f.label}
                    <input
                      type="color"
                      value={custom[f.key]}
                      onChange={(e) => {
                        setCustomOn(true)
                        setCustom((c) => ({ ...c, [f.key]: e.target.value }))
                      }}
                      className="h-5 w-10 cursor-pointer rounded border border-cyan-300/30 bg-transparent"
                    />
                  </label>
                ))}
                <label className="flex flex-col gap-1 text-[9px] tracking-[0.2em] text-cyan-100/40 uppercase">
                  animation intensity {calib.intensity.toFixed(2)}
                  <input
                    type="range"
                    min={0}
                    max={2}
                    step={0.05}
                    value={calib.intensity}
                    onChange={(e) => setCalib((c) => ({ ...c, intensity: Number(e.target.value) }))}
                    className="h-1 w-full cursor-pointer appearance-none rounded-full bg-cyan-300/25 accent-cyan-300"
                  />
                </label>
                <button
                  onClick={() => {
                    setCustom(DEFAULT_CUSTOM)
                    setCustomOn(false)
                  }}
                  className="w-full rounded-full border border-cyan-300/25 py-1 text-[9px] tracking-[0.2em] text-cyan-100/40 uppercase hover:border-cyan-300/60"
                >
                  reset colors
                </button>
              </div>
            )}

            {panel === 'audio' && (
              <div className="mt-2 w-52 space-y-3 rounded-xl border border-cyan-300/20 bg-void-900/80 p-3 backdrop-blur-md">
                {CALIB_FIELDS.map((f) => (
                  <label
                    key={f.key}
                    className="flex flex-col gap-1 text-[9px] tracking-[0.2em] text-cyan-100/40 uppercase"
                  >
                    {f.label} {calib[f.key].toFixed(2)}
                    <input
                      type="range"
                      min={f.min}
                      max={f.max}
                      step={0.05}
                      value={calib[f.key]}
                      onChange={(e) => setCalib((c) => ({ ...c, [f.key]: Number(e.target.value) }))}
                      className="h-1 w-full cursor-pointer appearance-none rounded-full bg-cyan-300/25 accent-cyan-300"
                    />
                  </label>
                ))}
                <button
                  onClick={() => setCalib(DEFAULT_CALIB)}
                  className="w-full rounded-full border border-cyan-300/25 py-1 text-[9px] tracking-[0.2em] text-cyan-100/40 uppercase hover:border-cyan-300/60"
                >
                  reset tuning
                </button>
              </div>
            )}
          </div>
        </div>

        {/* ── right rail: status + waveform ────────────────────────────── */}
        <div className="absolute top-6 right-6 text-right">
          <div className={`flex items-center justify-end gap-2 text-hud tracking-[0.25em] ${tone.text}`}>
            <span
              className={`inline-block w-1.5 h-1.5 rounded-full bg-current ${
                status === 'idle' ? 'opacity-40' : 'animate-pulse'
              }`}
            />
            STATUS: {STATUS_LABEL[status]}
          </div>
          <div className="mt-1 text-[9px] tracking-[0.2em] text-cyan-100/25 uppercase">
            {wsConnected ? 'link nominal' : 'link down'} · {isRemote ? 'lan backend' : 'local'}
          </div>
          <div className="mt-2 flex h-8 items-end justify-end gap-[3px]">
            {bands.map((b, i) => (
              <span
                key={i}
                className="w-[3px] rounded-full bg-cyan-300/80"
                style={{
                  height: `${Math.max(2, b * 32 + (status === 'responding' ? 6 : 0))}px`,
                  opacity: 0.35 + b * 0.65,
                }}
              />
            ))}
          </div>
        </div>

        {/* ── live caption ─────────────────────────────────────────────── */}
        {(sttText || status === 'thinking' || status === 'responding') && (
          <div className="pointer-events-none absolute inset-x-0 top-1/2 mt-40 flex justify-center px-6">
            <p className="max-w-2xl text-center font-mono text-sm tracking-wide text-cyan-100/80">
              {status === 'thinking' && !responseText ? (
                <span className="inline-flex items-center gap-2 text-holographic-400/80">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" /> processing…
                </span>
              ) : (
                <span className={status === 'responding' ? 'text-plasma-400/90' : undefined}>
                  {responseText || sttText}
                </span>
              )}
            </p>
          </div>
        )}

        {notice && (
          <div className="absolute inset-x-0 top-24 flex justify-center px-6">
            <button
              onClick={() => setNotice(null)}
              className="pointer-events-auto rounded-full border border-cyan-300/30 bg-void-900/80 px-4 py-2 font-mono text-[11px] tracking-wider text-cyan-300 backdrop-blur"
            >
              {notice}
            </button>
          </div>
        )}

        {/* ── transcript ───────────────────────────────────────────────── */}
        <div className="absolute bottom-28 left-1/2 z-10 w-full max-w-2xl -translate-x-1/2 px-5">
          <button
            onClick={() => setTranscriptOpen((v) => !v)}
            className="pointer-events-auto mx-auto flex items-center gap-2 rounded-full border border-cyan-300/25 bg-void-900/60 px-4 py-1.5 font-mono text-[10px] tracking-[0.3em] text-cyan-300/80 uppercase backdrop-blur transition hover:border-cyan-300/60"
          >
            live exchange
            <ChevronDown
              className={`w-3 h-3 transition-transform ${transcriptOpen ? '' : 'rotate-180'}`}
            />
          </button>
          {transcriptOpen && (
            <div className="pointer-events-auto mt-3 max-h-[38vh] space-y-3 overflow-y-auto rounded-2xl border border-cyan-300/20 bg-void-900/80 p-4 backdrop-blur-md">
              {!hasExchange && (
                <p className="text-center font-mono text-xs text-cyan-100/40">
                  Nothing yet. Arm the microphone, or say the wake word.
                </p>
              )}
              {sttText && (
                <div className="text-right">
                  <span className="font-mono text-[9px] tracking-[0.3em] text-cyan-100/35 uppercase">
                    you
                  </span>
                  <p className="text-sm leading-relaxed text-ghost">{sttText}</p>
                </div>
              )}
              {responseText && (
                <div className="text-left">
                  <span className="font-mono text-[9px] tracking-[0.3em] text-cyan-100/35 uppercase">
                    barq
                  </span>
                  <p className="text-sm leading-relaxed text-plasma-400/90">{responseText}</p>
                </div>
              )}
            </div>
          )}
        </div>

        {/* ── controls ─────────────────────────────────────────────────── */}
        <div className="absolute inset-x-0 bottom-0 p-5 sm:p-6">
          <div className="pointer-events-auto mx-auto flex w-full max-w-2xl items-center gap-2 rounded-full border border-cyan-300/25 bg-void-900/70 p-2 backdrop-blur-md">
            <button
              onClick={handleMicToggle}
              aria-label={detectorRunning ? 'Stop listening' : 'Start listening'}
              title={detectorRunning ? 'Stop listening' : 'Start listening'}
              className={`flex w-11 h-11 shrink-0 items-center justify-center rounded-full border transition ${
                detectorRunning
                  ? 'border-cyan-300 bg-cyan-300/15 text-cyan-300 shadow-glow-cyan'
                  : 'border-cyan-300/30 text-cyan-100/40 hover:text-cyan-300'
              }`}
            >
              {detectorRunning ? <Mic className="w-5 h-5" /> : <MicOff className="w-5 h-5" />}
            </button>

            <div className="flex-1 min-w-0 px-2">
              <div className="font-mono text-[10px] tracking-[0.2em] text-cyan-100/45 uppercase truncate">
                {detectorRunning
                  ? voiceListening
                    ? 'in conversation'
                    : 'armed — waiting for wake word'
                  : 'microphone idle'}
              </div>
              <div className="font-mono text-[9px] tracking-[0.15em] text-cyan-100/25 truncate">
                drag to orbit · scroll to zoom
              </div>
            </div>

            <button
              onClick={() => setMicReactive((v) => !v)}
              aria-pressed={micReactive}
              title={
                micReactive
                  ? 'Stop sampling the microphone directly'
                  : 'React to real microphone levels (opens a second mic stream)'
              }
              className={`flex w-9 h-9 shrink-0 items-center justify-center rounded-full transition ${
                micReactive ? 'text-cyan-300' : 'text-cyan-100/30 hover:text-cyan-300'
              }`}
            >
              <Waves className="w-4 h-4" />
            </button>

            <button
              onClick={handleReemerge}
              aria-label="Replay emergence"
              title="Replay emergence"
              className="flex w-9 h-9 shrink-0 items-center justify-center rounded-full text-cyan-100/30 transition hover:text-cyan-300"
            >
              <RotateCcw className="w-4 h-4" />
            </button>

            <button
              onClick={() => setMuted((m) => !m)}
              aria-label={muted ? 'Enable sound effects' : 'Mute sound effects'}
              title={muted ? 'Enable sound effects' : 'Mute sound effects'}
              className="flex w-9 h-9 shrink-0 items-center justify-center rounded-full text-cyan-100/30 transition hover:text-cyan-300"
            >
              {muted ? <VolumeX className="w-4 h-4" /> : <Volume2 className="w-4 h-4" />}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

export default CompanionDashboard
