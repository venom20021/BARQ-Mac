import { Suspense, Component, useState, useEffect, useRef, useCallback, lazy, useMemo, startTransition } from 'react'
import type { MutableRefObject, ReactNode } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Cloud, Mic, MicOff, Bot, User, ArrowLeft, Send, Loader2, Trash2,
  Crosshair, MapPin, LocateFixed, Settings2,
} from 'lucide-react'
import { LiveCaptions } from '../components/LiveCaptions'
import { DynamicChart } from '../components/DynamicChart'
import type { DynamicChartSchema } from '../components/DynamicChart'
import { BarChart3 as ChartIcon } from 'lucide-react'
import { api } from '../utils/api'
import { Vector3 } from 'three'
import { useVoice } from '../contexts/VoiceContext'

// ─── User Name ─────────────────────────────────────────────────────────

const USER_NAME_KEY = 'barq_user_name'
const DEFAULT_USER_NAME = ''

function getStoredUserName(): string {
  try { return localStorage.getItem(USER_NAME_KEY) || DEFAULT_USER_NAME } catch { return DEFAULT_USER_NAME }
}

// ─── Persistent Agent Chat History ─────────────────────────────────────

const AGENT_HISTORY_KEY = 'barq_agent_history'
const MAX_MESSAGES_PER_AGENT = 50

interface AgentChatMessage { role: 'user' | 'assistant'; content: string }

type AgentHistoryMap = Record<string, AgentChatMessage[]>

function loadAgentHistory(): AgentHistoryMap {
  try {
    const raw = localStorage.getItem(AGENT_HISTORY_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as AgentHistoryMap
      // Basic validation: ensure it's an object
      if (typeof parsed === 'object' && !Array.isArray(parsed)) return parsed
    }
  } catch { /* ignore corrupt data */ }
  return {}
}

function saveAgentHistory(history: AgentHistoryMap): void {
  try {
    // Prune: keep only last N messages per agent to avoid bloating localStorage
    const pruned: AgentHistoryMap = {}
    for (const [agent, messages] of Object.entries(history)) {
      pruned[agent] = messages.length > MAX_MESSAGES_PER_AGENT
        ? messages.slice(-MAX_MESSAGES_PER_AGENT)
        : messages
    }
    localStorage.setItem(AGENT_HISTORY_KEY, JSON.stringify(pruned))
  } catch {
    console.warn('[AgentHistory] Failed to persist to localStorage')
  }
}

async function loadAgentHistoryFromBackend(): Promise<AgentHistoryMap | null> {
  try {
    const resp = await api<{ history?: AgentHistoryMap }>('/memory/agent-history')
    if (resp?.history && typeof resp.history === 'object') {
      return resp.history
    }
  } catch {
    // Backend unreachable — fall back to localStorage
  }
  return null
}

async function syncAgentHistoryToBackend(history: AgentHistoryMap): Promise<void> {
  try {
    // Prune before sending to backend
    const pruned: AgentHistoryMap = {}
    for (const [agent, messages] of Object.entries(history)) {
      pruned[agent] = messages.length > MAX_MESSAGES_PER_AGENT
        ? messages.slice(-MAX_MESSAGES_PER_AGENT)
        : messages
    }
    await api('/memory/agent-history', { history: pruned })
  } catch {
    // Backend unreachable — silently skip (localStorage still has the data)
  }
}

// ─── Lazy-loaded Agent Node Network ────────────────────────────────────

import { QUALITY_PRESETS } from '../components/ParticleSphere3D'
import type { QualityLevel } from '../components/ParticleSphere3D'

const ParticleSphere3D = lazy(() =>
  import('../components/ParticleSphere3D').then(mod => ({ default: mod.ParticleSphere3D }))
)

// Lazy-load the Humanoid Dashboard
const HumanoidDashboard = lazy(() =>
  import('../components/dashboard/humanoid').then(mod => ({ default: mod.HumanoidDashboard }))
)

// ─── Error Boundary for Humanoid view (isolated crash recovery) ─────

interface HErrorState { hasError: boolean; message?: string }

class HumanoidErrorBoundary extends Component<{ children: ReactNode; onFallback: () => void }, HErrorState> {
  state: HErrorState = { hasError: false }

  static getDerivedStateFromError(e: Error): HErrorState {
    return { hasError: true, message: e.message }
  }

  componentDidCatch(error: Error): void {
    console.error('[HumanoidDashboard] CRASH:', error)
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div className="w-full h-full flex items-center justify-center bg-black">
          <div className="text-center max-w-sm">
            <div className="text-3xl mb-4">⚠️</div>
            <p className="text-sm font-mono text-red-400 mb-2">Humanoid view crashed</p>
            <p className="text-xs font-mono text-white/30 mb-4 break-all">{this.state.message}</p>
            <button
              onClick={this.props.onFallback}
              className="px-4 py-2 rounded-lg bg-white/5 border border-white/10 text-xs font-mono text-white/50 hover:text-white/80 hover:bg-white/10 transition-all"
            >
              ← Back to Network
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

// ─── Quality preset key ────────────────────────────────────────────────

const QUALITY_STORAGE_KEY = 'barq_particle_quality'

function getStoredQuality(): QualityLevel {
  try {
    const stored = localStorage.getItem(QUALITY_STORAGE_KEY)
    if (stored === 'ultra' || stored === 'high' || stored === 'medium' || stored === 'low' || stored === 'potato') {
      return stored
    }
  } catch { /* ignore */ }
  return 'high'
}

function setStoredQuality(quality: QualityLevel): void {
  try { localStorage.setItem(QUALITY_STORAGE_KEY, quality) } catch { /* ignore */ }
}

// ─── Real Weather Data ─────────────────────────────────────────────────

interface WeatherData { city: string; temperature_c: number; feels_like_c: number; humidity: number; description: string; source: 'auto' | 'manual' | 'default' }

const DEFAULT_WEATHER_CITY = 'London'
const WEATHER_RETRY_MS = [1000, 3000, 8000] // backoff intervals

async function fetchWeatherFromBridge(city: string): Promise<Omit<WeatherData, 'source'> | null> {
  const w = await api<Record<string, unknown>>(
    `/web/weather?city=${encodeURIComponent(city)}`,
  )
  if (!w || typeof w !== 'object') return null

  // Backend unavailable responses
  if (w.status === 'unconfigured' || w.status === 'unavailable') {
    console.warn('[Weather] Backend unavailable:', w.status, w.message || '')
    return null
  }

  // Validate required field
  if (w.temperature_c == null) {
    console.warn('[Weather] Missing temperature_c in response', w)
    return null
  }

  return {
    city: (w.city as string) ?? city,
    temperature_c: w.temperature_c as number,
    feels_like_c: (w.feels_like_c as number) ?? 0,
    humidity: (w.humidity as number) ?? 0,
    description: (w.description as string) ?? '',
  }
}

// ─── Device Location Detection ────────────────────────────────────────

const LOCATION_ATTEMPTED_KEY = 'barq_geo_attempted'

async function reverseGeocode(lat: number, lon: number): Promise<string | null> {
  try {
    const resp = await fetch(
      `https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lon}&format=json&zoom=10&accept-language=en`,
      { headers: { 'User-Agent': 'BARQ/2.0' } },
    )
    if (!resp.ok) return null
    const data = await resp.json()
    const addr = data.address || {}
    return addr.city || addr.town || addr.village || addr.municipality || addr.county || null
  } catch (err) {
    console.warn('[Weather] Reverse geocode failed:', err)
    return null
  }
}

async function detectDeviceLocation(): Promise<string | null> {
  if (localStorage.getItem(LOCATION_ATTEMPTED_KEY)) {
    return null // already attempted this session — don't re-prompt
  }

  try {
    return await new Promise<string | null>((resolve) => {
      navigator.geolocation.getCurrentPosition(
        async (position) => {
          const city = await reverseGeocode(
            position.coords.latitude,
            position.coords.longitude,
          )
          resolve(city)
        },
        () => {
          console.log('[Weather] Geolocation permission denied or unavailable')
          resolve(null)
        },
        { timeout: 8000, maximumAge: 600_000 }, // 8s timeout, 10min cache
      )
    })
  } catch (err) {
    console.warn('[Weather] Geolocation not supported:', err)
    return null
  }
}

function useWeatherData(): WeatherData | null {
  const [data, setData] = useState<WeatherData | null>(null)
  const cityRef = useRef<string>(DEFAULT_WEATHER_CITY)
  const sourceRef = useRef<'auto' | 'manual' | 'default'>('default')
  const locationAttemptedRef = useRef(false)

  useEffect(() => {
    let mounted = true
    let retryCount = 0
    let retryTimer: ReturnType<typeof setTimeout> | null = null

    const attemptFetch = async () => {
      try {
        // 1. Resolve city — try voice status first (respects manual Settings/voice changes),
        //    then fall back to device location for first-time users
        let city = DEFAULT_WEATHER_CITY
        let source: 'auto' | 'manual' | 'default' = 'default'

        try {
          const statusResp = await api('/voice/status')
          if (statusResp && typeof statusResp === 'object') {
            const s = statusResp as { weather_city?: string }
            if (s.weather_city && s.weather_city !== DEFAULT_WEATHER_CITY) {
              city = s.weather_city // user has already set a city — use it
              source = 'manual'
            }
          }
        } catch (err) {
          console.warn('[Weather] Failed to get city from voice status:', err)
        }

        // Only try geolocation if no city has been configured yet
        if (city === DEFAULT_WEATHER_CITY && !locationAttemptedRef.current) {
          locationAttemptedRef.current = true
          const detected = await detectDeviceLocation()
          if (detected && mounted) {
            city = detected
            source = 'auto'
            // Persist to backend so subsequent loads use this city
            try {
              await api('/voice/weather-city', { city })
              console.log('[Weather] Device location detected & saved:', city)
            } catch {
              // Backend unreachable — use detected city for this session only
            }
          }
          // Mark as attempted in localStorage to avoid re-prompting across sessions
          if (!localStorage.getItem(LOCATION_ATTEMPTED_KEY)) {
            localStorage.setItem(LOCATION_ATTEMPTED_KEY, 'true')
          }
        }

        cityRef.current = city // store for interval refresh
        sourceRef.current = source

        // 2. Fetch weather data
        const weatherData = await fetchWeatherFromBridge(city)
        if (!mounted) return

        if (weatherData) {
          setData({ ...weatherData, source })
          retryCount = 0 // success — reset retries
          console.log('[Weather] Loaded:', weatherData.city, weatherData.temperature_c + '°C', weatherData.description, `(${source})`)
        } else {
          // No data — schedule retry if we haven't exhausted attempts
          if (retryCount < WEATHER_RETRY_MS.length) {
            const delay = WEATHER_RETRY_MS[retryCount]
            retryCount++
            console.warn(`[Weather] Fetch returned no data, retry ${retryCount}/${WEATHER_RETRY_MS.length} in ${delay}ms`)
            retryTimer = setTimeout(attemptFetch, delay)
          } else {
            console.warn('[Weather] All retries exhausted — using fallback')
            if (mounted) {
              setData({
                city: cityRef.current,
                temperature_c: 15,
                feels_like_c: 13,
                humidity: 50,
                description: 'Weather currently unavailable',
                source: 'default',
              })
            }
          }
        }
      } catch (err) {
        console.error('[Weather] Fetch error:', err)
        if (retryCount < WEATHER_RETRY_MS.length && mounted) {
          const delay = WEATHER_RETRY_MS[retryCount]
          retryCount++
          retryTimer = setTimeout(attemptFetch, delay)
        }
      }
    }

    attemptFetch()

    // Periodic refresh every 5 minutes — uses cityRef to avoid stale closure
    const interval = setInterval(async () => {
      try {
        const city = cityRef.current
        const weatherData = await fetchWeatherFromBridge(city)
        if (mounted && weatherData) {
          setData({ ...weatherData, source: sourceRef.current })
        }
      } catch {
        // silent on interval — retry next cycle
      }
    }, 300_000)

    return () => {
      mounted = false
      clearInterval(interval)
      if (retryTimer) clearTimeout(retryTimer)
    }
  }, [])

  return data
}

// ─── Greeting Helpers ─────────────────────────────────────────────────

function getGreeting(name: string): string {
  const hour = new Date().getHours()
  const timeGreeting = hour < 12 ? 'GOOD MORNING' : hour < 17 ? 'GOOD AFTERNOON' : 'GOOD EVENING'
  return name ? `${timeGreeting}, ${name.toUpperCase()}` : timeGreeting
}
function getGreetingForDisplay(sttText: string, responseText: string, aiState: string, userName: string): string {
  if (sttText) return 'LISTENING'
  if (responseText) return 'RESPONDING'
  if (aiState === 'thinking') return 'THINKING'
  return getGreeting(userName)
}
function getSubGreeting(sttText: string, responseText: string, aiState: string, weather: WeatherData | null): string {
  if (sttText) return `"${sttText}"`
  if (responseText) return responseText.length > 50 ? responseText.slice(0, 50) + '…' : responseText
  if (aiState === 'thinking') return 'Processing your request…'
  if (weather) return `${weather.city} · ${weather.description}`
  return 'BARQ is ready'
}



// ─── CSS Audio Waveform ────────────────────────────────────────────────

function AudioWaveformBars({ isActive }: { isActive: boolean }): JSX.Element {
  return (
    <div className="flex items-center gap-[3px] h-5">
      {[1, 2, 3, 4, 5, 4, 3, 2, 1].map((h, i) => (
        <motion.span key={i} className={`w-[3px] rounded-full ${isActive ? 'bg-cyan-400' : 'bg-white/20'}`}
          animate={isActive ? { height: [4, 4 + h * 4, 4], opacity: [0.3, 1, 0.3] } : { height: 4, opacity: 0.2 }}
          transition={{ duration: 0.8 + i * 0.05, repeat: Infinity, ease: 'easeInOut', delay: i * 0.06 }}
        />
      ))}
    </div>
  )
}

// ─── Main Dashboard Page ───────────────────────────────────────────────

export function DashboardPage(): JSX.Element {
  const voice = useVoice()
  const {
    voiceListening,
    detectorRunning,
    wsConnected,
    aiState,
    sttText,
    responseText,
    isRemote,
    toggleDetector,
  } = voice
  const [userName, setUserName] = useState(getStoredUserName)

  // ── Dashboard view mode: 'network' (existing) or 'humanoid' (new) ──
  const [dashboardView, setDashboardView] = useState<'network' | 'humanoid'>('network')

  const toggleDashboardView = useCallback(() => {
    setDashboardView(prev => {
      const next = prev === 'network' ? 'humanoid' : 'network'
      try { localStorage.setItem('barq_dashboard_view', next) } catch { /* ignore */ }
      return next
    })
  }, [])

  // ── Clickable agent node state ───────────────────────────────────
  const [activeAgent, setActiveAgent] = useState<string | null>(null)
  const focusTargetRef = useRef<Vector3 | null>(null) as MutableRefObject<Vector3 | null>

  const activeAgentColor = useMemo(
    () => (activeAgent ? AGENT_COLORS[activeAgent] : '#00E5FF') ?? '#00E5FF',
    [activeAgent],
  )

  const onSelectAgent = useCallback((label: string) => { setActiveAgent(label) }, [setActiveAgent])
  // Persisted agent chat history (survives page reloads)
  const [agentHistory, setAgentHistory] = useState<AgentHistoryMap>(loadAgentHistory)
  const [historyLoaded, setHistoryLoaded] = useState(false)

  // On mount, try loading from backend first (cross-device sync)
  useEffect(() => {
    loadAgentHistoryFromBackend().then((backendHistory) => {
      if (backendHistory && Object.keys(backendHistory).length > 0) {
        setAgentHistory(backendHistory)
      }
      setHistoryLoaded(true)
    })
  }, [])

  // Persist to localStorage + backend whenever history changes (debounced)
  useEffect(() => {
    if (!historyLoaded) return
    const timer = setTimeout(() => {
      saveAgentHistory(agentHistory)
      syncAgentHistoryToBackend(agentHistory)
    }, 300)
    return () => clearTimeout(timer)
  }, [agentHistory, historyLoaded])
  const currentMessages = useMemo<AgentChatMessage[]>(
    () => activeAgent ? (agentHistory[activeAgent] ?? []) : [],
    [activeAgent, agentHistory],
  )
  const [agentInput, setAgentInput] = useState('')
  const [agentLoading, setAgentLoading] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  const agentInputRef = useRef<HTMLTextAreaElement>(null!)
  const agentInputRefValue = useRef('')
  const messagesEndRef = useRef<HTMLDivElement>(null!)
  const lastRequestRef = useRef<{ goal: string; time: number } | null>(null)

  useEffect(() => { agentInputRefValue.current = agentInput }, [agentInput])
  useEffect(() => { if (activeAgent) { const timer = setTimeout(() => agentInputRef.current?.focus(), 350); return () => clearTimeout(timer) } }, [activeAgent])
  useEffect(() => { if (messagesEndRef.current) messagesEndRef.current.scrollTop = messagesEndRef.current.scrollHeight }, [currentMessages, agentLoading])

  const sendAgentMessage = useCallback(async () => {
    const text = agentInputRefValue.current.trim()
    if (!text || text === '?' || text.length < 2 || !activeAgent || agentLoading) return
    
    // Dedup: prevent sending the same goal twice within 3 seconds
    const goal = `[${activeAgent}] ${text}`
    const now = Date.now()
    if (lastRequestRef.current && lastRequestRef.current.goal === goal && now - lastRequestRef.current.time < 3000) return
    lastRequestRef.current = { goal, time: now }

    setAgentInput('')
    agentInputRefValue.current = ''
    setAgentHistory(prev => ({ ...prev, [activeAgent]: [...(prev[activeAgent] ?? []), { role: 'user' as const, content: text }] }))
    setAgentLoading(true)
    try {
      const resp = await api('/agent/execute', { goal })
      let result: string
      if (resp && typeof resp === 'object') {
        const r = resp as Record<string, unknown>
        result = String(r.result ?? r.detail ?? '')
        if (!result || result === 'No response' || result === '') {
          result = r.result ? String(r.result) : ''
          if (!result) result = 'No response'
        }
      } else {
        result = String(resp ?? 'No response')
      }
      setAgentHistory(prev => ({ ...prev, [activeAgent]: [...(prev[activeAgent] ?? []), { role: 'assistant' as const, content: result }] }))
    } catch {
      setAgentHistory(prev => ({ ...prev, [activeAgent]: [...(prev[activeAgent] ?? []), { role: 'assistant' as const, content: 'Failed to reach agent. Is the backend running?' }] }))
    } finally { setAgentLoading(false) }
  }, [activeAgent, agentLoading])

  const handleClearHistory = useCallback(() => {
    setConfirmClear(true)
  }, [])

  const executeClearHistory = useCallback(() => {
    if (!activeAgent) return
    setConfirmClear(false)
    setAgentHistory(prev => {
      const next = { ...prev }
      delete next[activeAgent]
      return next
    })
  }, [activeAgent])

  const cancelClearHistory = useCallback(() => {
    setConfirmClear(false)
  }, [])

  // Reset confirmClear when switching agents
  useEffect(() => {
    startTransition(() => {
      setConfirmClear(false)
    })
  }, [activeAgent])

  const weather = useWeatherData()
  const [weatherInput, setWeatherInput] = useState('')
  const [editingCity, setEditingCity] = useState(false)
  const weatherInputRef = useRef<HTMLInputElement>(null!)

  const startEditCity = useCallback(() => {
    setWeatherInput(weather?.city ?? '')
    setEditingCity(true)
  }, [weather?.city])

  useEffect(() => {
    if (editingCity && weatherInputRef.current) {
      startTransition(() => {
        weatherInputRef.current.focus()
      })
    }
  }, [editingCity])

  const commitCity = useCallback(async () => {
    const city = weatherInput.trim()
    if (!city) { setEditingCity(false); return }
    try {
      setEditingCity(false)
      await api('/voice/weather-city', { city })
      console.log('[Weather] City set to:', city)
      window.location.reload()
    } catch (err) {
      console.error('[Weather] Failed to set city:', err)
      window.location.reload()
    }
  }, [weatherInput])

  const handleCityKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') commitCity()
    if (e.key === 'Escape') setEditingCity(false)
  }, [commitCity])

  const retryWeather = useCallback(() => {
    window.location.reload()
  }, [])

  // ── Detect location button handler ──────────────────────────────
  const [detectingLocation, setDetectingLocation] = useState(false)

  const handleDetectLocation = useCallback(async () => {
    setDetectingLocation(true)
    try {
      // Clear the attempted flag so geolocation will run
      localStorage.removeItem(LOCATION_ATTEMPTED_KEY)
      const city = await detectDeviceLocation()
      if (city) {
        await api('/voice/weather-city', { city })
        console.log('[Weather] Location detected:', city)
        window.location.reload()
      } else {
        console.warn('[Weather] Location detection returned no city')
        setDetectingLocation(false)
      }
    } catch (err) {
      console.error('[Weather] Location detection failed:', err)
      setDetectingLocation(false)
    }
  }, [])

  // ── Listen for profile updates ───────────────────────────────────
  useEffect(() => {
    const handler = (): void => { setUserName(getStoredUserName()) }
    window.addEventListener('barq:profile-updated', handler)
    return () => window.removeEventListener('barq:profile-updated', handler)
  }, [])

  // ── Greeting ─────────────────────────────────────────────────────
  const greeting = getGreetingForDisplay(sttText, responseText, aiState, userName)
  const subGreeting = getSubGreeting(sttText, responseText, aiState, weather)

  // ── Note: Voice state (detectorRunning, aiState, etc.) and the WebSocket
  //    connection are managed globally by VoiceContext, not per-page.

  // ═══════════════════════════════════════════════════════════════════
  //  GLOBAL STATE: systemLoad, drag-drop, radial menu, transfers
  // ═══════════════════════════════════════════════════════════════════

  const [systemLoad, setSystemLoad] = useState(35)
  const [isDraggingFile, setIsDraggingFile] = useState(false)
  const [activeRadialMenu, setActiveRadialMenu] = useState<string | null>(null)
  const [activeTransfers, setActiveTransfers] = useState<{ id: string; from: [number, number, number]; to: [number, number, number]; color: string; progress: number }[]>([])

  // ── Simulate system load oscillation ──────────────────────────────
  useEffect(() => {
    const interval = setInterval(() => {
      setSystemLoad(prev => {
        const delta = (Math.random() - 0.5) * 20
        return Math.max(5, Math.min(95, prev + delta))
      })
    }, 3000)
    return () => clearInterval(interval)
  }, [])

  // ── Simulate periodic data transfer sparks ───────────────────────
  useEffect(() => {
    const interval = setInterval(() => {
      const randomNode = AGENT_COLORS_KEYS[Math.floor(Math.random() * AGENT_COLORS_KEYS.length)]
      const nodeData = AGENT_NODES_LIST.find(n => n.label === randomNode)
      if (!nodeData) return
      const sparkId = `spark-${Date.now()}`
      const color = nodeData.color
      setActiveTransfers(prev => [...prev, { id: sparkId, from: [0, 0, 0], to: nodeData.position, color, progress: 0 }])
      // Animate progress to 1 over 1.5s
      const startTime = performance.now()
      const animFrame = () => {
        const elapsed = performance.now() - startTime
        const progress = Math.min(elapsed / 1500, 1)
        setActiveTransfers(prev => prev.map(s => s.id === sparkId ? { ...s, progress } : s))
        if (progress < 1) requestAnimationFrame(animFrame)
        else {
          // Remove spark after completion
          setActiveTransfers(prev => prev.filter(s => s.id !== sparkId))
        }
      }
      requestAnimationFrame(animFrame)
    }, 4000)
    return () => clearInterval(interval)
  }, [])

  // ── Radial menu handlers ─────────────────────────────────────────
  const onContextMenu = useCallback((label: string) => {
    setActiveRadialMenu(label)
  }, [setActiveRadialMenu])
  const onCloseRadialMenu = useCallback(() => {
    setActiveRadialMenu(null)
  }, [])

  const onReturnToCore = useCallback(() => {
    setActiveAgent(null)
    setActiveRadialMenu(null)
    focusTargetRef.current = null
  }, [setActiveAgent, setActiveRadialMenu])

  // ── Radial action handlers (QUICK_PROMPTS defined at module level) ──

  const handleRadialAction = useCallback(async (label: string, action: 'quick-execute' | 'view-details' | 'share-link') => {
    onCloseRadialMenu()

    if (action === 'quick-execute') {
      const prompt = QUICK_PROMPTS[label] ?? `Quick action for ${label}`
      try {
        const resp = await api('/agent/execute', { goal: `[${label}] ${prompt}` })
        const result = resp && typeof resp === 'object'
          ? ((resp as Record<string, unknown>).result ?? JSON.stringify(resp))
          : String(resp ?? 'No response')
        console.log(`⚡ ${label} result:`, String(result).slice(0, 200))
      } catch {
        console.warn(`⚡ ${label} execution failed — backend unreachable`)
      }
    }

    if (action === 'view-details') {
      try {
        const resp = await api('/agent/plan', { goal: `Show capabilities and context for agent ${label}`, context: `${label} agent` })
        if (resp && typeof resp === 'object') {
          const plan = (resp as Record<string, unknown>).plan ?? (resp as Record<string, unknown>).steps ?? JSON.stringify(resp)
          console.log(`📄 ${label}:`, String(plan).slice(0, 200))
        }
      } catch {
        console.warn(`📄 Could not retrieve ${label} details from backend`)
      }
    }

    if (action === 'share-link') {
      const shareText = `🤖 BARQ Agent: ${label}\nContext: Active agent in BARQ v2.0 Spatial Node Network\nColor: ${AGENT_COLORS[label] ?? '#00E5FF'}`
      try {
        await navigator.clipboard.writeText(shareText)
        console.log(`🔗 Copied ${label} profile to clipboard`)
      } catch {
        console.warn(`🔗 Could not copy ${label} profile`)
      }
    }
  }, [onCloseRadialMenu])

  // ── Particle quality control ────────────────────────────────────
  const [quality, setQuality] = useState<QualityLevel>(getStoredQuality)
  const [showQualityMenu, setShowQualityMenu] = useState(false)
  const qualityRef = useRef<HTMLDivElement>(null!)

  // Close quality menu on outside click
  useEffect(() => {
    if (!showQualityMenu) return
    const handler = (e: MouseEvent) => {
      if (qualityRef.current && !qualityRef.current.contains(e.target as Node)) {
        setShowQualityMenu(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [showQualityMenu])

  const handleQualityChange = useCallback((ql: QualityLevel) => {
    setQuality(ql)
    setStoredQuality(ql)
    setShowQualityMenu(false)
  }, [])

  // ── Dynamic Chart (Generative UI) ───────────────────────────────
  const [chartSchema, setChartSchema] = useState<DynamicChartSchema | null>(null)
  const [chartQuery, setChartQuery] = useState('')
  const [chartLoading, setChartLoading] = useState(false)
  const [chartError, setChartError] = useState('')
  const [showChartInput, setShowChartInput] = useState(false)

  const handleChartQuery = useCallback(async () => {
    const q = chartQuery.trim()
    if (!q || chartLoading) return
    setChartLoading(true)
    setChartError('')
    try {
      const resp = await api<{ schema?: DynamicChartSchema; interpretation?: string }>('/analytics/dynamic-chart', { query: q })
      if (resp?.schema) {
        setChartSchema(resp.schema)
      } else {
        setChartError('No chart data returned')
      }
    } catch {
      setChartError('Failed to generate chart. Is the backend running?')
    } finally {
      setChartLoading(false)
    }
  }, [chartQuery, chartLoading])

  const chartInputRef = useRef<HTMLInputElement>(null!)
  useEffect(() => {
    if (showChartInput && chartInputRef.current) chartInputRef.current.focus()
  }, [showChartInput])

  // ── Drag-and-drop handlers ───────────────────────────────────────
  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation(); setIsDraggingFile(true)
  }, [])
  const handleDragOver = useCallback((e: React.DragEvent) => { e.preventDefault(); e.stopPropagation() }, [])
  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation(); setIsDraggingFile(false)
  }, [])

  // ── Render ────────────────────────────────────────────────────────────
  const isVoiceActive = voiceListening && detectorRunning

  return (
    <div
      className="fixed inset-0 w-screen h-screen overflow-hidden bg-[radial-gradient(ellipse_at_center,_var(--tw-gradient-stops))] from-blue-900 via-slate-900 to-black"
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      {/* ── 3D Canvas (switches between network and humanoid views) ─── */}
      <div className="absolute inset-0">
        <Suspense fallback={
          <div className="w-full h-full flex items-center justify-center">
            <div className="relative flex items-center justify-center">
              <div className="w-16 h-16 border-2 border-cyan-400/20 border-t-cyan-400 rounded-full animate-spin" />
              <div className="absolute w-24 h-24 border border-cyan-400/10 rounded-full animate-ping" style={{ animationDuration: '2s' }} />
            </div>
          </div>
        }>
          {dashboardView === 'humanoid' ? (
            <HumanoidErrorBoundary onFallback={toggleDashboardView}>
              <HumanoidDashboard />
            </HumanoidErrorBoundary>
          ) : (
            <ParticleSphere3D
              activeAgent={activeAgent}
              onSelectAgent={onSelectAgent}
              focusTargetRef={focusTargetRef}
              onReturnToCore={onReturnToCore}
              systemLoad={systemLoad}
              activeTransfers={activeTransfers}
              onContextMenu={onContextMenu}
              activeRadialMenu={activeRadialMenu}
              onCloseRadialMenu={onCloseRadialMenu}
              onRadialAction={handleRadialAction}
              quality={quality}
            />
          )}
        </Suspense>
      </div>

      {/* ── Subtle vignette overlay ───────────────────────────────── */}
      <div className="absolute inset-0 pointer-events-none" style={{ background: 'radial-gradient(ellipse at center, transparent 50%, rgba(0,0,0,0.4) 100%)' }} />

      {/* ═══ VIEW TOGGLE (top-right, above HUD) ═══ */}
      <div className="absolute top-6 right-20 z-30">
        <button
          onClick={toggleDashboardView}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg backdrop-blur-md bg-white/5 border border-white/10 hover:bg-white/10 hover:border-cyan-500/30 transition-all duration-300 group"
          title={`Switch to ${dashboardView === 'network' ? 'Humanoid' : 'Network'} view`}
        >
          <div className={`w-1.5 h-1.5 rounded-full transition-colors duration-300 ${dashboardView === 'humanoid' ? 'bg-cyan-400 shadow-[0_0_6px_rgba(0,229,255,0.5)]' : 'bg-white/20'}`} />
          <span className="text-[9px] font-mono text-white/40 tracking-[0.15em] uppercase group-hover:text-white/60 transition-colors duration-300">
            {dashboardView === 'network' ? 'Network' : 'Humanoid'}
          </span>
          <span className="text-[8px] font-mono text-white/20">|</span>
          <span className="text-[9px] font-mono text-white/25 tracking-wider group-hover:text-cyan-300/60 transition-colors duration-300">
            {dashboardView === 'network' ? 'Humanoid' : 'Network'}
          </span>
        </button>
      </div>

      {/* ═══ WORKSPACE SIDE-PANEL ═══ */}
      <AnimatePresence>
        {activeAgent && (
          <motion.div
            initial={{ opacity: 0, x: 80 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 80 }}
            transition={{ duration: 0.35, ease: 'easeOut' }}
            className="absolute right-0 top-0 h-full w-[380px] z-20 flex flex-col"
            style={{ borderLeft: `3px solid ${activeAgentColor}`, boxShadow: `-4px 0 24px ${activeAgentColor}15` }}
          >
            <div className="absolute inset-0 backdrop-blur-md bg-slate-900/40" />
            <div className="relative z-10 flex flex-col h-full p-8">
              {/* Header */}
              <div className="flex items-start justify-between mb-8">
                <div>
                  <p className="text-[10px] font-sans text-white/30 tracking-[0.2em] uppercase font-medium mb-1">Agent Workspace</p>
                  <h2 className="text-2xl font-sans font-bold text-white/90 tracking-tight">{activeAgent.toUpperCase()}</h2>
                </div>
                <div className="flex items-center gap-2 mt-2">
                  <button
                    onClick={handleClearHistory}
                    disabled={currentMessages.length === 0 || confirmClear}
                    className="flex items-center justify-center w-6 h-6 rounded-lg text-white/20 hover:text-red-400 hover:bg-red-500/10 disabled:opacity-20 disabled:cursor-not-allowed transition-all duration-200"
                    title="Clear this agent's chat history"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                  <div className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: activeAgentColor, boxShadow: `0 0 12px ${activeAgentColor}60` }} />
                </div>
              </div>

              {/* Chat */}
              <div className="flex-1 rounded-xl bg-white/[0.03] border border-white/[0.06] flex flex-col overflow-hidden">
                <div className="flex-1 overflow-y-auto p-4 space-y-3 scrollbar-thin" ref={messagesEndRef}>
                  {confirmClear ? (
                    <div className="flex flex-col items-center justify-center h-full gap-4 px-6">
                      <div className="w-10 h-10 rounded-full bg-red-500/15 flex items-center justify-center border border-red-500/20">
                        <Trash2 className="w-5 h-5 text-red-400" />
                      </div>
                      <div className="text-center">
                        <p className="text-sm font-sans text-white/80 font-medium mb-1">Clear chat history?</p>
                        <p className="text-[11px] font-sans text-white/40 leading-relaxed">
                          This will permanently remove all messages for <span className="text-white/60 font-medium">{activeAgent}</span>.
                        </p>
                      </div>
                      <div className="flex gap-3 w-full max-w-[200px]">
                        <button
                          onClick={cancelClearHistory}
                          className="flex-1 px-3 py-2 rounded-lg text-xs font-sans font-medium text-white/50 border border-white/10 bg-white/5 hover:bg-white/10 hover:text-white/70 transition-all duration-200"
                        >
                          Cancel
                        </button>
                        <button
                          onClick={executeClearHistory}
                          className="flex-1 px-3 py-2 rounded-lg text-xs font-sans font-medium text-red-300 border border-red-500/30 bg-red-500/10 hover:bg-red-500/20 transition-all duration-200"
                        >
                          Clear
                        </button>
                      </div>
                    </div>
                  ) : currentMessages.length === 0 ? (
                    <div className="flex items-center justify-center h-full">
                      <p className="text-sm font-sans text-white/20 italic text-center max-w-[200px]">Ask {activeAgent} anything to get started</p>
                    </div>
                  ) : (
                    currentMessages.map((msg, i) => (
                      <motion.div key={i} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="flex items-start gap-2.5">
                        {msg.role === 'assistant' ? (
                          <Bot className="w-4 h-4 mt-0.5 shrink-0" style={{ color: activeAgentColor, filter: `drop-shadow(0 0 4px ${activeAgentColor}60)` }} />
                        ) : (
                          <User className="w-4 h-4 mt-0.5 shrink-0 text-white/40" />
                        )}
                        <p className="text-sm font-sans text-white/70 leading-relaxed">{msg.content}</p>
                      </motion.div>
                    ))
                  )}
                  {agentLoading && (
                    <div className="flex items-center gap-2.5">
                      <Bot className="w-4 h-4 shrink-0" style={{ color: activeAgentColor, filter: `drop-shadow(0 0 4px ${activeAgentColor}60)` }} />
                      <div className="flex gap-1">
                        <span className="w-1.5 h-1.5 rounded-full animate-bounce" style={{ backgroundColor: activeAgentColor, opacity: 0.6, animationDelay: '0ms' }} />
                        <span className="w-1.5 h-1.5 rounded-full animate-bounce" style={{ backgroundColor: activeAgentColor, opacity: 0.6, animationDelay: '150ms' }} />
                        <span className="w-1.5 h-1.5 rounded-full animate-bounce" style={{ backgroundColor: activeAgentColor, opacity: 0.6, animationDelay: '300ms' }} />
                      </div>
                    </div>
                  )}
                </div>

                {/* Chat input */}
                <div className="p-3 border-t border-white/[0.06]">
                  <div className="flex items-start gap-2">
                    <textarea ref={agentInputRef} value={agentInput}
                      onChange={(e) => { setAgentInput(e.target.value); e.target.style.height = 'auto'; e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px' }}
                      onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendAgentMessage(); e.currentTarget.style.height = 'auto' } }}
                      placeholder={`Ask ${activeAgent} anything...`}
                      className="flex-1 bg-white/[0.04] border border-white/[0.08] rounded-lg px-3 py-2.5 text-sm font-sans text-white/70 placeholder:text-white/20 outline-none focus:border-cyan-500/30 focus:bg-white/[0.06] transition-all duration-200 resize-none min-h-[36px] max-h-[120px] leading-relaxed overflow-y-auto"
                      disabled={agentLoading} rows={1}
                    />
                    <button onClick={sendAgentMessage} disabled={!agentInput.trim() || agentLoading}
                      className="flex items-center justify-center w-9 h-9 rounded-lg bg-cyan-500/20 border border-cyan-500/20 text-cyan-400 hover:bg-cyan-500/30 disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-200 shrink-0 mt-0.5"
                      style={{
                        boxShadow: agentInput.trim() && !agentLoading
                          ? `0 0 12px ${activeAgentColor}30`
                          : 'none',
                      }}
                    >
                      {agentLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                    </button>
                  </div>
                </div>
              </div>

              {/* Return button */}
              <button onClick={onReturnToCore} className="mt-6 flex items-center gap-2 px-4 py-2.5 rounded-lg bg-white/5 hover:bg-white/10 border border-white/10 hover:border-white/20 text-white/50 hover:text-white/80 transition-all duration-200 group">
                <ArrowLeft className="w-4 h-4 transition-transform duration-200 group-hover:-translate-x-0.5" />
                <span className="text-xs font-sans font-medium tracking-wide">Return to Core</span>
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* ═══ TOP-LEFT: Weather + Greeting ═══ */}
      <div className="absolute top-8 left-24 z-10">
        <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6, ease: 'easeOut' }} className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <Cloud className="w-4 h-4 text-cyan-300/60" />
            {weather ? (
              <span className="text-lg font-sans font-light text-white/80 tracking-wide tabular-nums">{Math.round(weather.temperature_c)}°<span className="text-white/40 text-sm">C</span></span>
            ) : (
              <span className="text-lg font-sans font-light text-white/40 cursor-pointer hover:text-white/60 transition-colors duration-200" onClick={retryWeather} title="Click to retry">--°</span>
            )}
            <span className="w-px h-4 bg-white/10" />
            {editingCity ? (
              <input
                ref={weatherInputRef}
                value={weatherInput}
                onChange={(e) => setWeatherInput(e.target.value)}
                onKeyDown={handleCityKeyDown}
                onBlur={commitCity}
                className="w-20 bg-white/5 border border-white/20 rounded px-1.5 py-0.5 text-[10px] font-sans text-white/80 uppercase tracking-[0.15em] font-medium outline-none focus:border-cyan-400/50"
                placeholder="City..."
              />
            ) : (
              <>
                {/* Location source indicator */}
                {weather && weather.source === 'auto' && (
                  <span title="Auto-detected from your location">
                    <Crosshair className="w-3 h-3 text-emerald-400/70 shrink-0" />
                  </span>
                )}
                {weather && weather.source === 'manual' && (
                  <span title="Manually set by you">
                    <MapPin className="w-3 h-3 text-cyan-400/70 shrink-0" />
                  </span>
                )}
                <span
                  className="text-[10px] font-sans text-white/40 uppercase tracking-[0.15em] font-medium cursor-pointer hover:text-cyan-300/80 transition-colors duration-200"
                  onClick={startEditCity}
                  title="Click to change city"
                >{weather?.city ?? (weather === null ? 'Unavailable' : 'Loading...')}</span>
                {/* Detect location button */}
                {weather && !editingCity && (
                  <button
                    onClick={handleDetectLocation}
                    disabled={detectingLocation}
                    className="flex items-center justify-center w-4 h-4 text-white/30 hover:text-cyan-300/80 transition-all duration-200 disabled:opacity-50"
                    title="Detect my location"
                  >
                    {detectingLocation ? (
                      <Loader2 className="w-3 h-3 animate-spin" />
                    ) : (
                      <LocateFixed className="w-3 h-3" />
                    )}
                  </button>
                )}
              </>
            )}
          </div>
          <div className="mt-2">
            <h1 className="text-4xl font-sans font-bold text-white/90 tracking-tight leading-none">{greeting}</h1>
            <p className="text-sm font-sans text-white/40 mt-1.5 font-light tracking-wide max-w-[300px] line-clamp-1">{subGreeting}</p>
          </div>
          <AnimatePresence mode="wait">
            {sttText && (
              <motion.div key="stt" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.2 }} className="mt-3 flex items-start gap-2">
                <span className="flex items-center gap-1 shrink-0 mt-0.5">
                  <User className="w-3 h-3 text-cyan-300/60" />
                  <span className="relative flex w-1.5 h-1.5"><span className="animate-ping absolute inset-0 rounded-full bg-cyan-400 opacity-50" /><span className="relative rounded-full w-1.5 h-1.5 bg-cyan-400" /></span>
                </span>
                <span className="text-sm font-sans text-cyan-200/80 italic leading-relaxed">&ldquo;{sttText}&rdquo;</span>
              </motion.div>
            )}
            {!sttText && responseText && (
              <motion.div key="response" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.2 }} className="mt-3 flex items-start gap-2">
                <span className="flex items-center gap-1 shrink-0 mt-0.5">
                  <Bot className="w-3 h-3 text-emerald-300/60" />
                  <span className="relative flex w-1.5 h-1.5"><span className="animate-ping absolute inset-0 rounded-full bg-emerald-400 opacity-40" /><span className="relative rounded-full w-1.5 h-1.5 bg-emerald-400" /></span>
                </span>
                <span className="text-sm font-sans text-emerald-200/80 leading-relaxed max-w-[320px]">
                  {responseText}
                  {(aiState === 'responding' || aiState === 'thinking') && <span className="inline-block w-1 h-4 ml-0.5 bg-emerald-400/60 animate-pulse align-text-bottom" />}
                </span>
              </motion.div>
            )}
          </AnimatePresence>
          <div className="flex items-center gap-3 mt-4">
            {['CORE', 'VISION', 'AUDIO', 'NET'].map((mod, i) => (
              <div key={mod} className="flex items-center gap-1.5">
                <span className={`w-1.5 h-1.5 rounded-full ${i !== 2 ? 'bg-cyan-400/60 shadow-[0_0_5px_rgba(34,211,238,0.3)]' : 'bg-white/10'}`} />
                <span className={`text-[9px] font-sans font-medium tracking-wider ${i !== 2 ? 'text-cyan-300/50' : 'text-white/20'}`}>{mod}</span>
              </div>
            ))}
            <span className={`w-1.5 h-1.5 rounded-full transition-all duration-300 ${wsConnected ? 'bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.5)]' : 'bg-amber-500/50'}`} title={wsConnected ? 'Connected' : 'Reconnecting...'} />
          </div>
        </motion.div>
      </div>

      {/* ═══ LIVE CAPTIONS OVERLAY ═══ */}
      <LiveCaptions
        sttText={sttText}
        responseText={responseText}
        isSpeaking={aiState === 'responding'}
        isProcessing={aiState === 'thinking'}
        conversationActive={voiceListening && detectorRunning}
      />

      {/* ═══ BOTTOM-CENTER: Voice Pill ═══ */}
      <div className="absolute bottom-24 left-1/2 -translate-x-1/2 z-50">
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6, delay: 0.2, ease: 'easeOut' }}
          className="flex items-center gap-4 px-5 py-3 rounded-2xl backdrop-blur-md bg-white/5 border border-white/10 shadow-xl"
        >
          <button onClick={toggleDetector}
            className={`flex items-center justify-center w-8 h-8 rounded-full transition-all duration-300 ${!detectorRunning ? 'bg-red-500/20 text-red-400 hover:bg-red-500/30' : isVoiceActive ? 'bg-cyan-500/20 text-cyan-400 hover:bg-cyan-500/30' : 'bg-white/5 text-white/40 hover:bg-white/10 hover:text-white/60'}`}
            title={detectorRunning ? 'Mute microphone' : 'Unmute microphone'}
          >{detectorRunning ? <Mic className="w-4 h-4" /> : <MicOff className="w-4 h-4" />}</button>
          <div className="w-px h-6 bg-white/10" />
          <div className="flex flex-col">
            <div className="flex items-center gap-2">
              <span className={`text-xs font-sans font-bold tracking-[0.15em] uppercase transition-all duration-300 ${!detectorRunning ? 'text-red-400' : isVoiceActive ? 'text-cyan-300' : 'text-white/40'}`}>
                {!detectorRunning ? 'MUTED' : isVoiceActive ? 'ON AIR' : 'STANDBY'}
              </span>
              {isRemote && (
                <span className="flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-cyan-500/10 border border-cyan-500/20">
                  <Cloud className="w-2.5 h-2.5 text-cyan-400/70" />
                  <span className="text-[8px] font-sans font-bold tracking-wider text-cyan-400/70 uppercase">Cloud</span>
                </span>
              )}
              {(isVoiceActive || aiState === 'thinking' || aiState === 'responding') && (
                <span className="relative flex w-2 h-2">
                  <span className={`animate-ping absolute inset-0 rounded-full ${aiState === 'responding' ? 'bg-emerald-400' : aiState === 'thinking' ? 'bg-amber-400' : 'bg-cyan-400'} opacity-50`} />
                  <span className={`relative rounded-full w-2 h-2 ${aiState === 'responding' ? 'bg-emerald-400' : aiState === 'thinking' ? 'bg-amber-400' : 'bg-cyan-400'}`} />
                </span>
              )}
            </div>
            <span className="text-[10px] font-sans text-white/25 tracking-wide">
              {aiState === 'responding' ? 'BARQ is speaking' : aiState === 'thinking' ? 'Processing...' : isVoiceActive ? 'Listening for commands' : detectorRunning ? 'Waiting for wake word' : 'Microphone disabled'}
            </span>
          </div>
          <div className="w-px h-6 bg-white/10" />
          <AudioWaveformBars isActive={isVoiceActive || aiState === 'responding'} />
        </motion.div>
      </div>

      {/* ── Bottom-left branding ──────────────────────────────────── */}
      <div className="absolute bottom-8 left-8 z-10">
        <motion.p initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.6, delay: 0.4 }}
          className="text-[10px] font-sans text-white/15 tracking-[0.2em] uppercase font-medium">B.A.R.Q · Agent Network</motion.p>
      </div>

      {/* ═══ SYSTEM LOAD + QUALITY CONTROL ═══ */}
      <div className="absolute bottom-8 right-8 z-30 flex items-center gap-2">
        {/* System load bar */}
        <div className="w-16 h-1 rounded-full bg-white/5 overflow-hidden">
          <motion.div
            className="h-full rounded-full"
            style={{ backgroundColor: systemLoad > 70 ? '#FBBF24' : '#4FC3F7' }}
            animate={{ width: `${systemLoad}%` }}
            transition={{ duration: 0.5, ease: 'easeOut' }}
          />
        </div>
        <span className="text-[9px] font-mono text-white/30 tabular-nums">{Math.round(systemLoad)}%</span>

        {/* Quality control gear */}
        <div className="relative" ref={qualityRef}>
          <button
            onClick={() => setShowQualityMenu(prev => !prev)}
            className="flex items-center justify-center w-5 h-5 rounded text-white/25 hover:text-cyan-300/70 hover:bg-white/5 transition-all duration-200"
            title="Particle quality"
          >
            <Settings2 className="w-3.5 h-3.5" />
          </button>

          {showQualityMenu && (
            <motion.div
              initial={{ opacity: 0, y: 6, scale: 0.95 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              transition={{ duration: 0.15 }}
              className="absolute bottom-6 right-0 mb-2 p-1.5 rounded-xl backdrop-blur-xl bg-black/80 border border-white/10 shadow-2xl min-w-[140px]"
            >
              <p className="text-[8px] font-sans text-white/25 tracking-[0.15em] uppercase font-medium px-2 pb-1.5 pt-0.5">
                3D Quality
              </p>
              <div className="space-y-0.5">
                {(['ultra', 'high', 'medium', 'low', 'potato'] as const).map((ql) => {
                  const preset = QUALITY_PRESETS[ql]
                  const isActive = quality === ql
                  return (
                    <button
                      key={ql}
                      onClick={() => handleQualityChange(ql)}
                      className={`w-full flex items-center justify-between px-2 py-1.5 rounded-lg text-xs transition-all duration-150 ${isActive ? 'bg-cyan-500/15 text-cyan-300' : 'text-white/40 hover:text-white/70 hover:bg-white/5'}`}
                    >
                      <div className="flex items-center gap-1.5">
                        <span className={`w-1.5 h-1.5 rounded-full ${
                          ql === 'ultra' ? 'bg-purple-400' :
                          ql === 'high' ? 'bg-cyan-400' :
                          ql === 'medium' ? 'bg-amber-400' :
                          ql === 'low' ? 'bg-orange-400' :
                          'bg-red-400'
                        }`} />
                        <span className="font-sans font-medium">{preset.label}</span>
                      </div>
                      <span className="text-[9px] font-mono opacity-60">{preset.particles.toLocaleString()}</span>
                    </button>
                  )
                })}
              </div>
            </motion.div>
          )}
        </div>
      </div>


      {/* ═══ CONTEXT DROP ZONE (viewport-scaled orbital ring) ═══ */}
      <AnimatePresence>
        {isDraggingFile && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.3 }}
            className="fixed inset-0 z-50 pointer-events-none"
          >
            <div
              className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full border-[1px] border-dashed border-cyan-400/30 animate-[spin_12s_linear_infinite]"
              style={{ width: '82vmin', height: '82vmin' }}
            />
            <div
              className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full border border-cyan-400/10 animate-ping"
              style={{ width: '72vmin', height: '72vmin', animationDuration: '4s' }}
            />
            <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 flex flex-col items-center gap-1">
              <p className="text-[10px] font-sans tracking-[0.3em] font-light text-cyan-400/70">DROP TO INGEST</p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* ═══ DYNAMIC CHART WIDGET (Bottom-right) ═══ */}
      <div className="absolute bottom-8 right-[160px] z-30 flex flex-col items-end gap-2">
        <AnimatePresence>
          {showChartInput && (
            <motion.div
              initial={{ opacity: 0, y: 12, scale: 0.95 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 12, scale: 0.95 }}
              transition={{ duration: 0.2, ease: 'easeOut' }}
              className="w-[320px] rounded-2xl backdrop-blur-xl bg-black/70 border border-white/10 shadow-2xl overflow-hidden"
            >
              {/* Query Input */}
              <div className="p-3 border-b border-white/[0.06]">
                <div className="flex items-center gap-2">
                  <input
                    ref={chartInputRef}
                    value={chartQuery}
                    onChange={(e) => setChartQuery(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') handleChartQuery() }}
                    placeholder="Ask for a chart... (e.g. 'Show career funnel')"
                    className="flex-1 bg-white/[0.04] border border-white/[0.08] rounded-lg px-3 py-2 text-xs font-sans text-white/70 placeholder:text-white/20 outline-none focus:border-cyan-500/30 focus:bg-white/[0.06] transition-all duration-200"
                    disabled={chartLoading}
                  />
                  <button
                    onClick={handleChartQuery}
                    disabled={!chartQuery.trim() || chartLoading}
                    className="flex items-center justify-center w-8 h-8 rounded-lg bg-cyan-500/15 border border-cyan-500/20 text-cyan-400 hover:bg-cyan-500/25 disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-200 shrink-0"
                  >
                    {chartLoading ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Send className="w-3.5 h-3.5" />
                    )}
                  </button>
                </div>
                {/* Suggested queries */}
                <div className="flex gap-1.5 mt-2 flex-wrap">
                  {['Career funnel', 'Social performance', 'Revenue trend', 'Platform breakdown'].map((suggestion) => (
                    <button
                      key={suggestion}
                      onClick={() => { setChartQuery(suggestion); setShowChartInput(true) }}
                      className="text-[9px] px-1.5 py-0.5 rounded-md bg-white/[0.03] border border-white/[0.06] text-white/30 hover:text-cyan-300/60 hover:border-cyan-500/20 transition-all duration-200 font-sans"
                    >
                      {suggestion}
                    </button>
                  ))}
                </div>
              </div>

              {/* Chart Display */}
              <div className="p-3 min-h-[60px]">
                {chartError && (
                  <p className="text-xs font-sans text-red-400/70 text-center py-2">{chartError}</p>
                )}
                {chartLoading && !chartError && (
                  <div className="flex items-center justify-center py-6">
                    <Loader2 className="w-5 h-5 text-cyan-400/50 animate-spin" />
                  </div>
                )}
                {chartSchema && !chartLoading && (
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <h3 className="text-xs font-sans font-medium text-white/60 tracking-wide">{chartSchema.title}</h3>
                      <button
                        onClick={() => { setChartSchema(null); setChartQuery('') }}
                        className="text-white/20 hover:text-red-400/70 transition-colors duration-200"
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </div>
                    <DynamicChart schema={chartSchema} />
                  </div>
                )}
                {!chartSchema && !chartLoading && !chartError && (
                  <p className="text-[10px] font-sans text-white/20 italic text-center py-4">
                    Ask a question about your data
                  </p>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Toggle button */}
        <button
          onClick={() => setShowChartInput(prev => !prev)}
          className={`flex items-center justify-center w-7 h-7 rounded-full transition-all duration-200 ${
            showChartInput
              ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/20 shadow-[0_0_12px_rgba(34,211,238,0.15)]'
              : 'bg-white/5 text-white/30 border border-white/10 hover:bg-white/10 hover:text-cyan-300/60'
          }`}
          title="Analytics Chart"
        >
          <ChartIcon className="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  )
}

// ─── Module-level helpers (used by the component) ──────────────────────

interface TLHelper { id: string; label: string; color: string; position: [number, number, number]; description: string }

const AGENT_COLORS: Record<string, string> = {
  Strategist: '#00E5FF', Researcher: '#A855F7', 'Chief of Staff': '#FBBF24',
  Finance: '#34D399', Memory: '#F472B6', Vision: '#60A5FA', Analytics: '#FB923C', Coding: '#2DD4BF',
}

const QUICK_PROMPTS: Record<string, string> = {
  Strategist: 'Scan the current environment and provide a strategic overview',
  Researcher: 'Summarize the latest developments in our active research areas',
  'Chief of Staff': 'Check the status of all active workflows and flag any blockers',
  Finance: 'Provide a quick budget health check and spending summary',
  Memory: 'Summarize recent context and important facts from memory',
  Vision: 'Analyze the current screen content and describe what you see',
  Analytics: 'Pull the latest metrics and highlight any anomalies',
  Coding: 'Review the current codebase state and suggest improvements',
}

const AGENT_COLORS_KEYS = Object.keys(AGENT_COLORS)

const AGENT_NODES_LIST: TLHelper[] = [
  { id: 'strategist', label: 'Strategist', color: '#00E5FF', position: [5.0, 1.8, 1.0], description: 'Planning & Strategy' },
  { id: 'researcher', label: 'Researcher', color: '#A855F7', position: [2.0, 4.5, 2.0], description: 'Deep Research' },
  { id: 'chief-of-staff', label: 'Chief of Staff', color: '#FBBF24', position: [-3.5, 4.0, 1.5], description: 'Coordination' },
  { id: 'finance', label: 'Finance', color: '#34D399', position: [-5.5, 0.0, -1.0], description: 'Budget & Tracking' },
  { id: 'memory', label: 'Memory', color: '#F472B6', position: [-3.0, -3.8, 2.0], description: 'Context & Recall' },
  { id: 'vision', label: 'Vision', color: '#60A5FA', position: [3.5, -3.5, 2.5], description: 'Computer Vision' },
  { id: 'analytics', label: 'Analytics', color: '#FB923C', position: [0.5, -5.5, -1.5], description: 'Data Insights' },
  { id: 'coding', label: 'Coding', color: '#2DD4BF', position: [1.5, 5.0, -2.0], description: 'Code Generation' },
]
