// ─── HumanoidDashboard.tsx ────────────────────────────────────────────
// Master Stage & Audio Reactivity: full-screen Canvas with Web Audio API,
// post-processing bloom, and 2D cyberpunk HUD overlay.
// Each 3D component is individually guarded against crashes.

import { useRef, useState, useCallback, useEffect, Suspense, Component, type ReactNode } from 'react'
import { Canvas } from '@react-three/fiber'
import { PerspectiveCamera } from '@react-three/drei'

import { ScatteringHumanoid } from './ScatteringHumanoid'
import { NexusCore } from './NexusCore'
import { SonarRings } from './SonarRings'
import { ParticleTerrain } from './ParticleTerrain'

// ─── Micro Error Boundary (per-component) ────────────────────────────

interface MEState { hasError: boolean; msg: string }
class MicroBoundary extends Component<{ name: string; children: ReactNode; onError?: (msg: string) => void }, MEState> {
  state: MEState = { hasError: false, msg: '' }
  static getDerivedStateFromError(e: Error): MEState {
    return { hasError: true, msg: e.message }
  }
  componentDidCatch(e: Error): void {
    console.error(`[Humanoid:${this.props.name}]`, e)
    this.props.onError?.(e.message)
  }
  render() {
    return this.state.hasError ? null : this.props.children
  }
}

// ─── Audio Analyzer Hook ─────────────────────────────────────────────

function useAudioAnalyzer() {
  const [level, setLevel] = useState(0)
  const [isActive, setIsActive] = useState(true) // simulated by default
  const animFrameRef = useRef(0)

  // Start simulated audio immediately on mount
  useEffect(() => {
    const simulate = () => {
      const t = performance.now() / 1000
      setLevel(0.15 + Math.sin(t * 0.8) * 0.1 + Math.sin(t * 2.3) * 0.05)
      animFrameRef.current = requestAnimationFrame(simulate)
    }
    simulate()
    return () => cancelAnimationFrame(animFrameRef.current)
  }, [])

  const startMic = useCallback(async () => {
    try {
      cancelAnimationFrame(animFrameRef.current)
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const ctx = new AudioContext()
      const src = ctx.createMediaStreamSource(stream)
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 64
      src.connect(analyser)
      const data = new Uint8Array(analyser.frequencyBinCount)
      const analyze = () => {
        analyser.getByteFrequencyData(data)
        let sum = 0
        for (let i = 0; i < data.length; i++) sum += data[i]
        setLevel(sum / data.length / 255)
        animFrameRef.current = requestAnimationFrame(analyze)
      }
      analyze()
      setIsActive(true)
    } catch {
      // Resume simulated audio
      const simulate = () => {
        const t = performance.now() / 1000
        setLevel(0.15 + Math.sin(t * 0.8) * 0.1)
        animFrameRef.current = requestAnimationFrame(simulate)
      }
      simulate()
    }
  }, [])

  return { level, isActive, startMic }
}

// ─── Scene Component (inside Canvas) ─────────────────────────────────

function HumanoidScene({
  progress,
  audioLevel,
  onError,
}: {
  progress: number
  audioLevel: number
  onError: (msg: string) => void
}): JSX.Element {
  return (
    <>
      <PerspectiveCamera makeDefault position={[0, 0.8, 4]} fov={50} />
      <ambientLight intensity={0.15} />
      <pointLight position={[0, 1.5, 2]} intensity={0.5} color="#00e5ff" />
      <pointLight position={[0, -0.5, 1]} intensity={0.3} color="#ff8f00" />

      <MicroBoundary name="Terrain" onError={onError}>
        <ParticleTerrain audioLevel={audioLevel} />
      </MicroBoundary>

      <MicroBoundary name="Nexus" onError={onError}>
        <NexusCore audioLevel={audioLevel} progress={progress} />
      </MicroBoundary>

      <MicroBoundary name="Humanoid" onError={onError}>
        <ScatteringHumanoid progress={progress} audioLevel={audioLevel} />
      </MicroBoundary>

      <MicroBoundary name="Sonar" onError={onError}>
        <SonarRings audioLevel={audioLevel} />
      </MicroBoundary>



    </>
  )
}

// ─── HUD Overlay ─────────────────────────────────────────────────────

function CyberpunkHUD({
  progress,
  audioLevel,
  systemLoad,
  latency,
}: {
  progress: number
  audioLevel: number
  systemLoad: number
  latency: number
}): JSX.Element {
  const [time, setTime] = useState(new Date())

  useEffect(() => {
    const interval = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(interval)
  }, [])

  return (
    <div className="absolute inset-0 pointer-events-none z-10 font-mono">
      {/* Top-left telemetry */}
      <div className="absolute top-6 left-6 space-y-2">
        <div className="flex items-center gap-2">
          <div className="w-1.5 h-1.5 rounded-full bg-cyan-400 shadow-[0_0_8px_rgba(0,229,255,0.6)]" />
          <span className="text-[10px] text-cyan-300/70 tracking-[0.2em] uppercase">System Active</span>
        </div>
        <div className="text-[9px] text-white/30 space-y-0.5">
          <div>CPU: <span className="text-cyan-300/60 tabular-nums">{Math.round(systemLoad)}%</span></div>
          <div>MEM SYNC: <span className="text-emerald-400/60">NOMINAL</span></div>
          <div>LAT: <span className="text-amber-300/60 tabular-nums">{latency}ms</span></div>
        </div>
      </div>

      {/* Top-right timestamp */}
      <div className="absolute top-6 right-6 text-right">
        <div className="text-[9px] text-white/20 tracking-[0.15em] uppercase">
          {time.toLocaleTimeString('en-US', { hour12: false })}
        </div>
        <div className="text-[8px] text-white/15 tracking-wider">HUMANOID DASHBOARD v1.0</div>
      </div>

      {/* Bottom telemetry bar */}
      <div className="absolute bottom-6 left-6 right-6 flex items-end justify-between">
        <div className="flex items-center gap-4">
          <div className="flex flex-col gap-1">
            <span className="text-[8px] text-white/20 tracking-wider uppercase">Assembly</span>
            <div className="w-24 h-0.5 bg-white/5 rounded-full overflow-hidden">
              <div className="h-full bg-cyan-400/60 rounded-full transition-all duration-300" style={{ width: `${progress * 100}%` }} />
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-[8px] text-white/20 tracking-wider uppercase">Audio</span>
            <div className="w-16 h-0.5 bg-white/5 rounded-full overflow-hidden">
              <div className="h-full bg-amber-400/60 rounded-full transition-all duration-100" style={{ width: `${audioLevel * 100}%` }} />
            </div>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {['CORE', 'SONAR', 'TERRAIN'].map(label => (
            <div key={label} className="flex items-center gap-1">
              <div className="w-1 h-1 rounded-full bg-cyan-400/50 shadow-[0_0_4px_rgba(0,229,255,0.3)]" />
              <span className="text-[8px] text-white/25 tracking-wider">{label}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Decorative corner brackets */}
      <div className="absolute top-4 left-4 w-6 h-6 border-l border-t border-cyan-500/20" />
      <div className="absolute top-4 right-4 w-6 h-6 border-r border-t border-cyan-500/20" />
      <div className="absolute bottom-4 left-4 w-6 h-6 border-l border-b border-cyan-500/20" />
      <div className="absolute bottom-4 right-4 w-6 h-6 border-r border-b border-cyan-500/20" />
    </div>
  )
}

// ─── Main Export ──────────────────────────────────────────────────────

export function HumanoidDashboard(): JSX.Element {
  const [progress, setProgress] = useState(0)
  const [isAssembled, setIsAssembled] = useState(false)
  const [systemLoad, setSystemLoad] = useState(35)
  const [latency, setLatency] = useState(12)
  const [componentErrors, setComponentErrors] = useState<string[]>([])

  const { level: audioLevel, isActive: audioActive, startMic } = useAudioAnalyzer()

  const handleComponentError = useCallback((msg: string) => {
    setComponentErrors(prev => [...prev, msg])
  }, [])

  // Auto-assemble on mount
  useEffect(() => {
    const timer = setTimeout(() => {
      setProgress(1)
      setIsAssembled(true)
    }, 500)
    return () => clearTimeout(timer)
  }, [])

  // Simulate system metrics
  useEffect(() => {
    const interval = setInterval(() => {
      setSystemLoad(prev => Math.max(10, Math.min(90, prev + (Math.random() - 0.5) * 15)))
      setLatency(Math.round(8 + Math.random() * 20))
    }, 3000)
    return () => clearInterval(interval)
  }, [])

  const handleToggleAssembly = useCallback(() => {
    if (isAssembled) {
      setProgress(0)
      setIsAssembled(false)
    } else {
      setProgress(1)
      setIsAssembled(true)
    }
  }, [isAssembled])

  const handleToggleMic = useCallback(() => {
    void startMic()
  }, [startMic])

  return (
    <div className="fixed inset-0 w-screen h-screen overflow-hidden bg-black">
      {/* 3D Canvas */}
      <div className="absolute inset-0">
        <Suspense
          fallback={
            <div className="w-full h-full flex items-center justify-center bg-black">
              <div className="text-center">
                <div className="w-16 h-16 border-2 border-cyan-400/20 border-t-cyan-400 rounded-full animate-spin mx-auto mb-4" />
                <p className="text-[10px] font-mono text-cyan-300/50 tracking-[0.2em] uppercase">Loading Humanoid...</p>
              </div>
            </div>
          }
        >
          <Canvas
            camera={{ position: [0, 0.8, 4], fov: 50 }}
            gl={{
              antialias: true,
              alpha: false,
              powerPreference: 'high-performance',
            }}
            dpr={[1, 1]}
            style={{ width: '100%', height: '100%', background: '#000008' }}
            onCreated={(state) => {
              state.gl.setClearColor('#000008')
            }}
          >
            <HumanoidScene
              progress={progress}
              audioLevel={audioLevel}
              onError={handleComponentError}
            />
          </Canvas>
        </Suspense>
      </div>

      {/* HUD Overlay */}
      <CyberpunkHUD
        progress={progress}
        audioLevel={audioLevel}
        systemLoad={systemLoad}
        latency={latency}
      />

      {/* Component error indicator (for debugging) */}
      {componentErrors.length > 0 && (
        <div className="absolute top-16 left-6 z-20 space-y-1">
          {componentErrors.map((err, i) => (
            <div key={i} className="text-[8px] font-mono text-red-400/70 bg-red-500/10 px-2 py-1 rounded border border-red-500/20 max-w-[300px] truncate">
              ⚠ {err}
            </div>
          ))}
        </div>
      )}

      {/* Control buttons */}
      <div className="absolute bottom-16 left-1/2 -translate-x-1/2 z-20 flex items-center gap-3">
        <button
          onClick={handleToggleAssembly}
          className="pointer-events-auto px-5 py-2.5 rounded-xl backdrop-blur-md bg-white/5 border border-white/10 hover:bg-white/10 hover:border-cyan-500/30 transition-all duration-300 group"
        >
          <span className="text-[10px] font-mono text-white/50 tracking-[0.2em] uppercase group-hover:text-cyan-300/80 transition-colors duration-300">
            {isAssembled ? 'Disperse to Nexus' : 'Assemble Humanoid'}
          </span>
        </button>

        <button
          onClick={handleToggleMic}
          className="pointer-events-auto flex items-center justify-center w-10 h-10 rounded-xl backdrop-blur-md bg-white/5 border border-white/10 hover:bg-white/10 transition-all duration-300"
          title={audioActive ? 'Disable microphone' : 'Enable microphone'}
        >
          <div className={`w-2 h-2 rounded-full transition-all duration-300 ${audioActive ? 'bg-cyan-400 shadow-[0_0_8px_rgba(0,229,255,0.6)]' : 'bg-white/20'}`} />
          <span className="text-[8px] font-mono text-white/30 ml-2 tracking-wider">
            {audioActive ? 'MIC' : 'OFF'}
          </span>
        </button>
      </div>

      {/* Bottom-left branding */}
      <div className="absolute bottom-4 left-6 z-10">
        <span className="text-[8px] font-mono text-white/15 tracking-[0.3em] uppercase">
          Cybernetic Humanoid Dashboard
        </span>
      </div>
    </div>
  )
}
