import { useState, useEffect, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  GitBranch, Database, RefreshCw, Loader2, Search,
  ArrowRightLeft, Brain, BookOpen, Link2, Wifi, WifiOff,
  MessageSquare, Send, ChevronDown, ChevronRight, Zap,
  Clock, CheckCircle, AlertCircle, X, History,
  ToggleLeft, ToggleRight, Timer, BarChart3, Users,
} from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { api } from '../utils/api'
import { SecondBrainGraph } from '../components/SecondBrainGraph'
import ForceGraph2D from 'react-force-graph-2d'

// ─── Types ──────────────────────────────────────────────────────────────

type TabKey = 'barq' | 'second_brain' | 'unified'

interface TabDef {
  key: TabKey
  label: string
  icon: typeof Brain
  color: string
}

const TABS: TabDef[] = [
  { key: 'barq', label: 'BARQ Brains', icon: Brain, color: '#818cf8' },
  { key: 'second_brain', label: 'Second Brain', icon: BookOpen, color: '#8b5cf6' },
  { key: 'unified', label: 'Unified View', icon: Link2, color: '#22d3ee' },
]

interface SBStatus {
  connected: boolean
  enabled: boolean
  url?: string
  stats?: { total_items?: number; by_type?: Record<string, number>; embedded_items?: number }
}

interface SyncResult {
  status: string
  created?: number
  updated?: number
  items_processed?: number
  triplets_added?: number
  detail?: string
  to_items?: { created?: number; updated?: number; detail?: string }
  to_triplets?: { items_processed?: number; triplets_added?: number; detail?: string }
  direction?: string
}

interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
  sources?: { id: number; title: string; type: string; score: number }[]
}

interface SBSearchResult {
  item: { id: number; title: string; item_type: string; content: string; tags: string[] }
  score: number
  match_type: string
}

interface SyncStatus {
  auto_sync: {
    running: boolean
    interval_seconds: number
    next_sync_at: string | null
    error_count: number
  }
  mappings: {
    total: number
    by_brain: Record<string, number>
    by_direction: Record<string, number>
  }
  last_sync: {
    sync_type: string
    status: string
    items_synced: number
    items_created: number
    items_updated: number
    items_skipped: number
    errors: number
    started_at: string
    completed_at: string | null
  } | null
  recent_history: SyncHistoryEntry[]
}

interface SyncHistoryEntry {
  id: number
  sync_type: string
  status: string
  items_synced: number
  items_created: number
  items_updated: number
  items_skipped: number
  errors: number
  duration_seconds: number | null
  started_at: string
  completed_at: string | null
}

interface SyncProgress {
  active: boolean
  direction: string
  phase: string
  current: number
  total: number
}

// ─── Component ──────────────────────────────────────────────────────────

export function UnifiedKnowledgePage(): JSX.Element {
  const navigate = useNavigate()
  const [activeTab, setActiveTab] = useState<TabKey>('second_brain')
  const [sbStatus, setSbStatus] = useState<SBStatus | null>(null)
  const [sbLoading, setSbLoading] = useState(true)

  // ── Fetch Second Brain status ───────────────────────────────────────
  const fetchStatus = useCallback(async () => {
    setSbLoading(true)
    try {
      const resp = await api<SBStatus>('/api/second-brain/status')
      if (resp) setSbStatus(resp)
    } catch { /* ignore */ }
    setSbLoading(false)
  }, [])

  useEffect(() => {
    void fetchStatus()
  }, [fetchStatus])

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-4">
      {/* Header */}
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
        <h1 className="text-xl font-orbitron font-bold text-ghost tracking-wider flex items-center gap-3">
          <Link2 className="w-6 h-6 text-cyan-400" />
          UNIFIED KNOWLEDGE
        </h1>
        <p className="text-sm font-rajdhani text-dim-400 mt-1">
          Combined knowledge graph — BARQ brains + Second Brain, searchable and synced
        </p>
      </motion.div>

      {/* Connection Status + Sync Controls */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.05 }}
        className="flex items-center gap-3"
      >
        <StatusBadge status={sbStatus} loading={sbLoading} />
        <div className="flex-1" />
        <SyncControls onSyncComplete={fetchStatus} />
      </motion.div>

      {/* Sync Status Panel (collapsible) */}
      <SyncPanel onRefresh={fetchStatus} />

      {/* Tab Navigation */}
      <div className="flex flex-wrap gap-1 border-b border-zinc-800/60 pb-0">
        {TABS.map((tab) => {
          const Icon = tab.icon
          return (
            <button
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={`flex items-center gap-1.5 px-4 py-2 text-xs font-rajdhani font-semibold transition-all rounded-t-lg border-b-2
                ${activeTab === tab.key
                  ? 'bg-zinc-900/40'
                  : 'text-dim-400 hover:text-ghost hover:bg-zinc-900/20 border-transparent'
                }`}
              style={{
                borderBottomColor: activeTab === tab.key ? tab.color : 'transparent',
                color: activeTab === tab.key ? tab.color : undefined,
              }}
            >
              <Icon className="w-3.5 h-3.5" />
              {tab.label}
            </button>
          )
        })}
      </div>

      {/* Tab Content */}
      <AnimatePresence mode="wait">
        <motion.div
          key={activeTab}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.15 }}
        >
          {activeTab === 'barq' && <BarqBrainsTab />}
          {activeTab === 'second_brain' && <SecondBrainTab sbStatus={sbStatus} />}
          {activeTab === 'unified' && <UnifiedTab sbStatus={sbStatus} />}
        </motion.div>
      </AnimatePresence>
    </div>
  )
}

// ─── Status Badge ───────────────────────────────────────────────────────

function StatusBadge({ status, loading }: { status: SBStatus | null; loading: boolean }): JSX.Element {
  if (loading) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-zinc-900/60 border border-zinc-800/60">
        <Loader2 className="w-3 h-3 animate-spin text-zinc-500" />
        <span className="text-[10px] font-mono text-zinc-500">Checking connection...</span>
      </div>
    )
  }

  if (!status || !status.enabled) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-zinc-900/60 border border-zinc-800/60">
        <WifiOff className="w-3 h-3 text-zinc-500" />
        <span className="text-[10px] font-mono text-zinc-500">Second Brain disabled</span>
      </div>
    )
  }

  if (!status.connected) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-amber-500/10 border border-amber-500/20">
        <AlertCircle className="w-3 h-3 text-amber-400" />
        <span className="text-[10px] font-mono text-amber-400">Not connected — is Second Brain running?</span>
      </div>
    )
  }

  const itemCount = status.stats?.total_items ?? 0
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-emerald-500/10 border border-emerald-500/20">
      <Wifi className="w-3 h-3 text-emerald-400" />
      <span className="text-[10px] font-mono text-emerald-400">
        Connected · {itemCount} items
      </span>
    </div>
  )
}

// ─── Sync Controls ──────────────────────────────────────────────────────

function SyncControls({ onSyncComplete }: { onSyncComplete: () => void }): JSX.Element {
  const [syncing, setSyncing] = useState(false)
  const [lastResult, setLastResult] = useState<SyncResult | null>(null)
  const [showResult, setShowResult] = useState(false)
  const [syncProgress, setSyncProgress] = useState<SyncProgress | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  // Connect to sync WebSocket for real-time progress
  useEffect(() => {
    let ws: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout>

    const connect = () => {
      try {
        const wsUrl = 'ws://127.0.0.1:8956/api/second-brain/ws/sync'
        ws = new WebSocket(wsUrl)
        wsRef.current = ws

        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data)
            if (msg.type === 'sync_progress') {
              setSyncProgress({
                active: true,
                direction: msg.direction || 'both',
                phase: msg.phase || 'syncing',
                current: msg.current || 0,
                total: msg.total || 0,
              })
            } else if (msg.type === 'sync_completed') {
              setSyncProgress(null)
              onSyncComplete()
            } else if (msg.type === 'sync_error') {
              setSyncProgress(null)
            }
          } catch { /* ignore parse errors */ }
        }

        ws.onclose = () => {
          retryTimer = setTimeout(connect, 10000)
        }
        ws.onerror = () => {
          ws?.close()
        }
      } catch { /* WebSocket not available */ }
    }

    connect()
    return () => {
      clearTimeout(retryTimer)
      ws?.close()
    }
  }, [onSyncComplete])

  const handleSync = useCallback(async (direction: string) => {
    setSyncing(true)
    setLastResult(null)
    try {
      // Try WebSocket trigger first
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'trigger_sync', direction }))
      }
      const resp = await api<SyncResult>('/api/second-brain/sync/full', { direction })
      if (resp) {
        setLastResult(resp)
        setShowResult(true)
        setTimeout(() => setShowResult(false), 5000)
      }
      onSyncComplete()
    } catch { /* ignore */ }
    setSyncing(false)
  }, [onSyncComplete])

  const isActive = syncing || syncProgress?.active

  return (
    <div className="flex items-center gap-2 relative">
      {/* Real-time progress bar */}
      {syncProgress?.active && (
        <div className="absolute top-full left-0 right-0 mt-2 px-3 py-2 rounded-lg bg-zinc-900/95 border border-cyan-500/30 shadow-xl z-50">
          <div className="flex items-center gap-2 mb-1.5">
            <Loader2 className="w-3 h-3 animate-spin text-cyan-400" />
            <span className="text-[10px] font-mono text-cyan-400">
              Syncing {syncProgress.direction === 'both' ? 'bidirectional' : syncProgress.direction}...
            </span>
            <span className="text-[9px] font-mono text-zinc-500 ml-auto">
              {syncProgress.current}/{syncProgress.total}
            </span>
          </div>
          <div className="w-full h-1 rounded-full bg-zinc-800 overflow-hidden">
            <motion.div
              className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-purple-500"
              initial={{ width: 0 }}
              animate={{ width: syncProgress.total > 0 ? `${(syncProgress.current / syncProgress.total) * 100}%` : '100%' }}
              transition={{ duration: 0.3 }}
            />
          </div>
          <p className="text-[9px] font-mono text-zinc-500 mt-1">Phase: {syncProgress.phase}</p>
        </div>
      )}

      <button
        onClick={() => handleSync('to-items')}
        disabled={isActive}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-purple-500/10 text-purple-400 border border-purple-500/20 text-[10px] font-rajdhani font-semibold hover:bg-purple-500/20 transition-all disabled:opacity-40"
        title="Sync BARQ triplets → Second Brain items"
      >
        {isActive ? <Loader2 className="w-3 h-3 animate-spin" /> : <ArrowRightLeft className="w-3 h-3" />}
        BARQ → SB
      </button>
      <button
        onClick={() => handleSync('to-triplets')}
        disabled={isActive}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 text-[10px] font-rajdhani font-semibold hover:bg-cyan-500/20 transition-all disabled:opacity-40"
        title="Sync Second Brain items → BARQ triplets"
      >
        {isActive ? <Loader2 className="w-3 h-3 animate-spin" /> : <ArrowRightLeft className="w-3 h-3" />}
        SB → BARQ
      </button>
      <button
        onClick={() => handleSync('both')}
        disabled={isActive}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[10px] font-rajdhani font-semibold hover:bg-emerald-500/20 transition-all disabled:opacity-40"
        title="Full bidirectional sync"
      >
        {isActive ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
        Full Sync
      </button>

      {/* Result tooltip */}
      <AnimatePresence>
        {showResult && lastResult && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            className="absolute top-full right-0 mt-2 px-3 py-2 rounded-lg bg-zinc-900/95 border border-zinc-700/60 shadow-xl z-50 min-w-[200px]"
          >
            <div className="flex items-center gap-1.5 mb-1">
              <CheckCircle className="w-3 h-3 text-emerald-400" />
              <span className="text-[10px] font-mono text-emerald-400">Sync complete</span>
            </div>
            {lastResult.to_items && (
              <p className="text-[9px] font-mono text-zinc-400">
                → SB: {lastResult.to_items.created} created, {lastResult.to_items.updated} updated
              </p>
            )}
            {lastResult.to_triplets && (
              <p className="text-[9px] font-mono text-zinc-400">
                → BARQ: {lastResult.to_triplets.triplets_added} triplets from {lastResult.to_triplets.items_processed} items
              </p>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ─── Sync Panel ────────────────────────────────────────────────────────

function SyncPanel({ onRefresh }: { onRefresh: () => void }): JSX.Element {
  const [syncStatus, setSyncStatus] = useState<SyncStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [panelOpen, setPanelOpen] = useState(false)
  const [autoSyncStarting, setAutoSyncStarting] = useState(false)
  const [autoSyncStopping, setAutoSyncStopping] = useState(false)
  const [autoSyncInterval, setAutoSyncInterval] = useState(300)

  const fetchSyncStatus = useCallback(async () => {
    try {
      const resp = await api<SyncStatus>('/api/second-brain/sync/status')
      if (resp) setSyncStatus(resp)
    } catch { /* ignore */ }
    setLoading(false)
  }, [])

  useEffect(() => {
    void fetchSyncStatus()
    const interval = setInterval(fetchSyncStatus, 30000) // refresh every 30s
    return () => clearInterval(interval)
  }, [fetchSyncStatus])

  const handleStartAutoSync = useCallback(async () => {
    setAutoSyncStarting(true)
    try {
      await api('/api/second-brain/sync/auto/start', { interval_seconds: autoSyncInterval })
      await fetchSyncStatus()
    } catch { /* ignore */ }
    setAutoSyncStarting(false)
  }, [autoSyncInterval, fetchSyncStatus])

  const handleStopAutoSync = useCallback(async () => {
    setAutoSyncStopping(true)
    try {
      await api('/api/second-brain/sync/auto/stop')
      await fetchSyncStatus()
    } catch { /* ignore */ }
    setAutoSyncStopping(false)
  }, [fetchSyncStatus])

  const autoSync = syncStatus?.auto_sync
  const lastSync = syncStatus?.last_sync
  const mappings = syncStatus?.mappings
  const history = syncStatus?.recent_history || []

  const formatTime = (iso: string | null) => {
    if (!iso) return '—'
    const d = new Date(iso)
    const now = new Date()
    const diffMs = now.getTime() - d.getTime()
    const diffMin = Math.floor(diffMs / 60000)
    if (diffMin < 1) return 'just now'
    if (diffMin < 60) return `${diffMin}m ago`
    const diffHr = Math.floor(diffMin / 60)
    if (diffHr < 24) return `${diffHr}h ago`
    return d.toLocaleDateString()
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.1 }}
    >
      {/* Collapsed summary bar */}
      <button
        onClick={() => setPanelOpen(!panelOpen)}
        className="w-full flex items-center gap-3 px-4 py-2.5 rounded-xl glass-card hover:border-zinc-700/60 transition-all"
      >
        {/* Auto-sync indicator */}
        {autoSync?.running ? (
          <div className="flex items-center gap-1.5">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
            </span>
            <span className="text-[10px] font-mono text-emerald-400">Auto-sync ON</span>
          </div>
        ) : (
          <div className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-zinc-600" />
            <span className="text-[10px] font-mono text-zinc-500">Auto-sync OFF</span>
          </div>
        )}

        <div className="w-px h-4 bg-zinc-800" />

        {/* Mapping count */}
        <div className="flex items-center gap-1.5">
          <Users className="w-3 h-3 text-purple-400" />
          <span className="text-[10px] font-mono text-zinc-400">
            {mappings?.total ?? 0} mappings
          </span>
        </div>

        <div className="w-px h-4 bg-zinc-800" />

        {/* Last sync */}
        <div className="flex items-center gap-1.5">
          <Clock className="w-3 h-3 text-zinc-500" />
          <span className="text-[10px] font-mono text-zinc-500">
            Last: {lastSync ? formatTime(lastSync.started_at) : 'never'}
          </span>
          {lastSync && (
            <span className={`text-[9px] font-mono px-1.5 py-0.5 rounded ${
              lastSync.status === 'completed'
                ? 'bg-emerald-500/10 text-emerald-400'
                : lastSync.status === 'partial'
                  ? 'bg-amber-500/10 text-amber-400'
                  : 'bg-red-500/10 text-red-400'
            }`}>
              {lastSync.status}
            </span>
          )}
        </div>

        <div className="flex-1" />

        <span className="text-[10px] font-rajdhani text-zinc-500">
          {panelOpen ? 'Hide' : 'Details'}
        </span>
        {panelOpen ? (
          <ChevronDown className="w-3.5 h-3.5 text-zinc-500" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 text-zinc-500" />
        )}
      </button>

      {/* Expanded panel */}
      <AnimatePresence>
        {panelOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            <div className="mt-2 glass-card p-4 space-y-4">
              {loading ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="w-5 h-5 animate-spin text-dim-400" />
                </div>
              ) : (
                <>
                  {/* Auto-Sync Controls */}
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <Timer className="w-4 h-4 text-cyan-400" />
                      <span className="text-xs font-orbitron font-bold text-ghost tracking-wider">AUTO-SYNC</span>
                      {autoSync?.running && (
                        <span className="text-[9px] font-mono text-emerald-400 bg-emerald-500/10 px-1.5 py-0.5 rounded">
                          every {Math.round((autoSync.interval_seconds || 300) / 60)}m
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      {autoSync?.running ? (
                        <button
                          onClick={handleStopAutoSync}
                          disabled={autoSyncStopping}
                          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 border border-red-500/20 text-[10px] font-rajdhani font-semibold hover:bg-red-500/20 transition-all disabled:opacity-40"
                        >
                          {autoSyncStopping ? <Loader2 className="w-3 h-3 animate-spin" /> : <ToggleRight className="w-3 h-3" />}
                          Stop
                        </button>
                      ) : (
                        <>
                          <select
                            value={autoSyncInterval}
                            onChange={(e) => setAutoSyncInterval(Number(e.target.value))}
                            className="bg-void-700 rounded-lg px-2 py-1 text-[10px] font-mono text-ghost border border-zinc-800/60"
                          >
                            <option value={60}>1 min</option>
                            <option value={300}>5 min</option>
                            <option value={600}>10 min</option>
                            <option value={1800}>30 min</option>
                            <option value={3600}>1 hr</option>
                          </select>
                          <button
                            onClick={handleStartAutoSync}
                            disabled={autoSyncStarting}
                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[10px] font-rajdhani font-semibold hover:bg-emerald-500/20 transition-all disabled:opacity-40"
                          >
                            {autoSyncStarting ? <Loader2 className="w-3 h-3 animate-spin" /> : <ToggleLeft className="w-3 h-3" />}
                            Start
                          </button>
                        </>
                      )}
                    </div>
                  </div>

                  {/* Mapping Stats */}
                  {mappings && (
                    <div className="grid grid-cols-3 gap-3">
                      <div className="text-center p-3 rounded-lg bg-zinc-900/30 border border-zinc-800/40">
                        <p className="text-lg font-orbitron font-bold text-purple-400">{mappings.total}</p>
                        <p className="text-[9px] font-mono text-zinc-500 uppercase">Total Mappings</p>
                      </div>
                      <div className="text-center p-3 rounded-lg bg-zinc-900/30 border border-zinc-800/40">
                        <p className="text-lg font-orbitron font-bold text-cyan-400">
                          {Object.values(mappings.by_brain || {}).reduce((s: number, v: unknown) => s + (v as number), 0)}
                        </p>
                        <p className="text-[9px] font-mono text-zinc-500 uppercase">By Brain Type</p>
                      </div>
                      <div className="text-center p-3 rounded-lg bg-zinc-900/30 border border-zinc-800/40">
                        <p className="text-lg font-orbitron font-bold text-emerald-400">
                          {mappings.by_direction?.both ?? 0}
                        </p>
                        <p className="text-[9px] font-mono text-zinc-500 uppercase">Bidirectional</p>
                      </div>
                    </div>
                  )}

                  {/* Last Sync Result */}
                  {lastSync && (
                    <div className="p-3 rounded-lg bg-zinc-900/30 border border-zinc-800/40">
                      <div className="flex items-center gap-2 mb-2">
                        {lastSync.status === 'completed' ? (
                          <CheckCircle className="w-3.5 h-3.5 text-emerald-400" />
                        ) : lastSync.status === 'partial' ? (
                          <AlertCircle className="w-3.5 h-3.5 text-amber-400" />
                        ) : (
                          <AlertCircle className="w-3.5 h-3.5 text-red-400" />
                        )}
                        <span className="text-xs font-rajdhani font-semibold text-ghost">
                          Last Sync: {lastSync.sync_type}
                        </span>
                        <span className={`text-[9px] font-mono px-1.5 py-0.5 rounded ${
                          lastSync.status === 'completed' ? 'bg-emerald-500/10 text-emerald-400' : 'bg-red-500/10 text-red-400'
                        }`}>
                          {lastSync.status}
                        </span>
                        <span className="text-[9px] font-mono text-zinc-500 ml-auto">
                          {formatTime(lastSync.started_at)}
                        </span>
                      </div>
                      <div className="grid grid-cols-4 gap-2">
                        {[
                          { label: 'Synced', value: lastSync.items_synced, color: '#22d3ee' },
                          { label: 'Created', value: lastSync.items_created, color: '#4ade80' },
                          { label: 'Updated', value: lastSync.items_updated, color: '#fbbf24' },
                          { label: 'Skipped', value: lastSync.items_skipped, color: '#a78bfa' },
                        ].map((s) => (
                          <div key={s.label} className="text-center">
                            <p className="text-sm font-orbitron font-bold" style={{ color: s.color }}>{s.value}</p>
                            <p className="text-[8px] font-mono text-zinc-500 uppercase">{s.label}</p>
                          </div>
                        ))}
                      </div>
                      {lastSync.errors > 0 && (
                        <p className="text-[9px] font-mono text-red-400 mt-1.5">
                          {lastSync.errors} error{lastSync.errors > 1 ? 's' : ''} during sync
                        </p>
                      )}
                    </div>
                  )}

                  {/* Sync History */}
                  {history.length > 0 && (
                    <div>
                      <div className="flex items-center gap-2 mb-2">
                        <History className="w-3.5 h-3.5 text-zinc-400" />
                        <span className="text-[10px] font-orbitron font-bold text-zinc-400 tracking-wider">RECENT HISTORY</span>
                      </div>
                      <div className="space-y-1 max-h-40 overflow-y-auto">
                        {history.map((entry) => (
                          <div
                            key={entry.id}
                            className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg hover:bg-zinc-800/30 transition-colors"
                          >
                            <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                              entry.status === 'completed' ? 'bg-emerald-400'
                              : entry.status === 'partial' ? 'bg-amber-400'
                              : 'bg-red-400'
                            }`} />
                            <span className="text-[10px] font-mono text-zinc-400 shrink-0 w-16">
                              {entry.sync_type}
                            </span>
                            <span className="text-[10px] font-mono text-zinc-500 flex-1 truncate">
                              {entry.items_synced} synced, {entry.items_created} created
                              {entry.duration_seconds ? ` · ${entry.duration_seconds.toFixed(1)}s` : ''}
                            </span>
                            <span className="text-[9px] font-mono text-zinc-600 shrink-0">
                              {formatTime(entry.started_at)}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

// ─── BARQ Brains Tab ────────────────────────────────────────────────────

function BarqBrainsTab(): JSX.Element {
  const navigate = useNavigate()
  const [brains, setBrains] = useState<{ type: string; label: string; color: string; nodes: number; edges: number }[]>([])
  const [selectedBrain, setSelectedBrain] = useState('general')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    (async () => {
      try {
        const resp = await api<{ type: string; label: string; color: string; nodes: number; edges: number }[]>('/api/brain/list')
        if (resp && Array.isArray(resp)) setBrains(resp)
      } catch { /* ignore */ }
      setLoading(false)
    })()
  }, [])

  return (
    <div className="space-y-4">
      {/* Brain selector */}
      <div className="glass-card">
        <div className="flex items-center gap-2 mb-3">
          <Brain className="w-5 h-5 text-purple-400" />
          <h3 className="text-sm font-orbitron font-bold text-ghost tracking-wider">BARQ BRAINS</h3>
        </div>
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 animate-spin text-dim-400" />
          </div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
            {brains.map((brain) => (
              <button
                key={brain.type}
                onClick={() => setSelectedBrain(brain.type)}
                className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-left transition-all ${
                  selectedBrain === brain.type
                    ? 'bg-zinc-800/60'
                    : 'bg-zinc-900/30 hover:bg-zinc-800/30 border-zinc-800/40'
                }`}
                style={{
                  borderColor: selectedBrain === brain.type ? `${brain.color}40` : undefined,
                }}
              >
                <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: brain.color }} />
                <div className="min-w-0">
                  <p className="text-xs font-rajdhani font-semibold text-ghost truncate">{brain.label}</p>
                  <p className="text-[9px] font-mono text-zinc-500">{brain.nodes} nodes · {brain.edges} edges</p>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* BARQ brain graph info */}
      <div className="glass-card">
        <div className="flex items-center gap-2 mb-3">
          <GitBranch className="w-5 h-5 text-cyan-400" />
          <h3 className="text-sm font-orbitron font-bold text-ghost tracking-wider">
            {brains.find(b => b.type === selectedBrain)?.label || selectedBrain}
          </h3>
        </div>
        <p className="text-xs font-exo text-dim-400 mb-3">
          BARQ brains use triplet-based knowledge graphs (subject → relation → object).
          Full graph visualization is available on the dedicated{' '}           <button onClick={() => navigate('/brain')} className="text-cyan-400 hover:underline cursor-pointer">Brain page</button>.
        </p>
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: 'Nodes', value: brains.find(b => b.type === selectedBrain)?.nodes ?? 0, color: '#818cf8' },
            { label: 'Edges', value: brains.find(b => b.type === selectedBrain)?.edges ?? 0, color: '#22d3ee' },
            { label: 'Domains', value: brains.length, color: '#4ade80' },
          ].map((stat) => (
            <div key={stat.label} className="text-center p-3 rounded-lg bg-zinc-900/30 border border-zinc-800/40">
              <p className="text-lg font-orbitron font-bold" style={{ color: stat.color }}>{stat.value}</p>
              <p className="text-[9px] font-mono text-zinc-500 uppercase">{stat.label}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ─── Second Brain Tab ───────────────────────────────────────────────────

function SecondBrainTab({ sbStatus }: { sbStatus: SBStatus | null }): JSX.Element {
  const [searchQuery, setSearchQuery] = useState('')
  const [searchMode, setSearchMode] = useState('hybrid')
  const [searchResults, setSearchResults] = useState<SBSearchResult[]>([])
  const [searching, setSearching] = useState(false)
  const [items, setItems] = useState<{ id: number; title: string; item_type: string; tags: string[] }[]>([])
  const [itemsLoading, setItemsLoading] = useState(true)
  const [selectedItem, setSelectedItem] = useState<{ id: number; title: string; item_type: string; content: string; tags: string[] } | null>(null)

  // Chat state
  const [chatOpen, setChatOpen] = useState(false)
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([])
  const [chatInput, setChatInput] = useState('')
  const [chatSending, setChatSending] = useState(false)

  // ── Fetch items ─────────────────────────────────────────────────────
  useEffect(() => {
    (async () => {
      try {
        const resp = await api<{ id: number; title: string; item_type: string; tags: string[] }[]>('/api/second-brain/items?limit=100')
        if (resp && Array.isArray(resp)) setItems(resp)
      } catch { /* ignore */ }
      setItemsLoading(false)
    })()
  }, [])

  // ── Search ──────────────────────────────────────────────────────────
  const handleSearch = useCallback(async () => {
    if (!searchQuery.trim()) return
    setSearching(true)
    try {
      const resp = await api<SBSearchResult[]>('/api/second-brain/search', {
        query: searchQuery,
        mode: searchMode,
        limit: 20,
      })
      if (resp && Array.isArray(resp)) setSearchResults(resp)
    } catch { /* ignore */ }
    setSearching(false)
  }, [searchQuery, searchMode])

  // ── Chat ────────────────────────────────────────────────────────────
  const sendChat = useCallback(async () => {
    if (!chatInput.trim() || chatSending) return
    const msg = chatInput.trim()
    setChatInput('')
    setChatMessages((prev) => [...prev, { role: 'user', text: msg }])
    setChatSending(true)
    try {
      const resp = await api<{ reply: string; sources?: ChatMessage['sources'] }>('/api/second-brain/chat', {
        message: msg,
        history: chatMessages.slice(-10).map((m) => ({
          role: m.role === 'user' ? 'user' : 'model',
          parts: [m.text],
        })),
      })
      if (resp) {
        setChatMessages((prev) => [...prev, { role: 'assistant', text: resp.reply, sources: resp.sources }])
      }
    } catch {
      setChatMessages((prev) => [...prev, { role: 'assistant', text: 'Error: Could not reach Second Brain.' }])
    }
    setChatSending(false)
  }, [chatInput, chatSending, chatMessages])

  const connected = sbStatus?.connected && sbStatus?.enabled

  return (
    <div className="space-y-4">
      {/* Search */}
      <div className="glass-card">
        <div className="flex items-center gap-2 mb-3">
          <Search className="w-5 h-5 text-purple-400" />
          <h3 className="text-sm font-orbitron font-bold text-ghost tracking-wider">SEARCH SECOND BRAIN</h3>
        </div>
        <div className="flex gap-2">
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
            placeholder="Search notes, code, bookmarks..."
            className="input-cyan flex-1 text-sm"
            disabled={!connected}
          />
          <select
            value={searchMode}
            onChange={(e) => setSearchMode(e.target.value)}
            className="bg-void-700 rounded-lg px-2 py-1 text-xs text-ghost border border-cyan-500/10"
          >
            <option value="hybrid">Hybrid</option>
            <option value="semantic">Semantic</option>
            <option value="text">Text</option>
          </select>
          <button onClick={handleSearch} disabled={searching || !connected} className="btn-cyan text-sm whitespace-nowrap">
            {searching ? <Loader2 className="w-3 h-3 animate-spin inline" /> : null}
            Search
          </button>
        </div>

        {/* Search Results */}
        {searchResults.length > 0 && (
          <div className="mt-3 space-y-2 max-h-60 overflow-y-auto">
            {searchResults.map((r) => (
              <div
                key={r.item.id}
                onClick={() => setSelectedItem(r.item)}
                className="flex items-start gap-2 px-3 py-2 rounded-lg bg-zinc-900/30 border border-zinc-800/40 hover:border-purple-500/30 cursor-pointer transition-colors"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-rajdhani font-semibold text-ghost">{r.item.title}</span>
                    <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-400 border border-purple-500/20">
                      {r.item.item_type}
                    </span>
                  </div>
                  <p className="text-[10px] font-exo text-dim-400 truncate mt-0.5">
                    {r.item.content?.slice(0, 120)}
                  </p>
                </div>
                <span className="text-[9px] font-mono text-zinc-500 shrink-0">
                  {(r.score * 100).toFixed(0)}%
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Graph + Items side by side */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Graph */}
        <div className="lg:col-span-2 glass-card overflow-hidden flex flex-col" style={{ height: 480 }}>
          {connected ? (
            <SecondBrainGraph height={460} />
          ) : (
            <div className="flex items-center justify-center h-[400px]">
              <div className="text-center">
                <WifiOff className="w-8 h-8 text-zinc-600 mx-auto mb-2" />
                <p className="text-xs font-mono text-zinc-500">Second Brain not connected</p>
                <p className="text-[10px] font-mono text-zinc-600 mt-1">Enable in Settings → Second Brain</p>
              </div>
            </div>
          )}
        </div>

        {/* Items list */}
        <div className="glass-card">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-orbitron font-bold text-ghost tracking-wider">ITEMS</h3>
            <span className="text-[9px] font-mono text-zinc-500">{items.length}</span>
          </div>
          {itemsLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-5 h-5 animate-spin text-dim-400" />
            </div>
          ) : items.length === 0 ? (
            <div className="text-center py-8">
              <BookOpen className="w-6 h-6 text-zinc-600 mx-auto mb-2" />
              <p className="text-[10px] font-mono text-zinc-500">No items yet</p>
            </div>
          ) : (
            <div className="space-y-1 max-h-[360px] overflow-y-auto">
              {items.map((item) => (
                <div
                  key={item.id}
                  onClick={() => setSelectedItem(item as typeof selectedItem & { content: string })}
                  className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg hover:bg-zinc-800/30 cursor-pointer transition-colors"
                >
                  <span className="text-[9px] font-mono px-1.5 py-0.5 rounded shrink-0"
                    style={{
                      backgroundColor: item.item_type === 'note' ? '#4ade8018' : item.item_type === 'code' ? '#22d3ee18' : '#fbbf2418',
                      color: item.item_type === 'note' ? '#4ade80' : item.item_type === 'code' ? '#22d3ee' : '#fbbf24',
                    }}
                  >
                    {item.item_type}
                  </span>
                  <span className="text-xs font-rajdhani text-ghost truncate">{item.title}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Item Detail Modal */}
      <AnimatePresence>
        {selectedItem && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center p-4"
            onClick={() => setSelectedItem(null)}
          >
            <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" />
            <motion.div
              initial={{ scale: 0.95, y: 10 }}
              animate={{ scale: 1, y: 0 }}
              exit={{ scale: 0.95, y: 10 }}
              className="relative glass-card w-[600px] max-w-[90vw] max-h-[80vh] flex flex-col overflow-hidden"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-800/60">
                <div className="flex items-center gap-2">
                  <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-400 border border-purple-500/20">
                    {selectedItem.item_type}
                  </span>
                  <h3 className="text-sm font-rajdhani font-semibold text-ghost">{selectedItem.title}</h3>
                </div>
                <button onClick={() => setSelectedItem(null)} className="p-1 rounded text-zinc-500 hover:text-zinc-300">
                  <X className="w-4 h-4" />
                </button>
              </div>
              <div className="flex-1 overflow-y-auto p-4">
                <div className="flex flex-wrap gap-1 mb-3">
                  {selectedItem.tags?.map((tag) => (
                    <span key={tag} className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-400 border border-purple-500/15">
                      #{tag}
                    </span>
                  ))}
                </div>
                <pre className="text-xs font-mono text-dim-300 whitespace-pre-wrap leading-relaxed">
                  {selectedItem.content || 'No content'}
                </pre>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* AI Chat */}
      <div className="glass-card">
        <button
          onClick={() => setChatOpen(!chatOpen)}
          className="w-full flex items-center gap-2"
        >
          <MessageSquare className="w-5 h-5 text-purple-400" />
          <h3 className="text-sm font-orbitron font-bold text-ghost tracking-wider flex-1 text-left">AI CHAT</h3>
          <span className="text-[9px] font-mono text-zinc-500">Gemini · brain-grounded</span>
          {chatOpen ? <ChevronDown className="w-4 h-4 text-zinc-500" /> : <ChevronRight className="w-4 h-4 text-zinc-500" />}
        </button>
        <AnimatePresence>
          {chatOpen && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              className="overflow-hidden"
            >
              <div className="mt-3 space-y-2 max-h-64 overflow-y-auto">
                {chatMessages.length === 0 && (
                  <p className="text-[10px] font-mono text-zinc-600 text-center py-4">
                    Ask anything about your Second Brain knowledge base.
                  </p>
                )}
                {chatMessages.map((msg, i) => (
                  <div key={i} className={`px-3 py-2 rounded-lg text-xs font-exo ${msg.role === 'user' ? 'bg-purple-500/10 border border-purple-500/20 ml-8' : 'bg-zinc-900/30 border border-zinc-800/40 mr-8'}`}>
                    <p className="text-dim-200 whitespace-pre-wrap">{msg.text}</p>
                    {msg.sources && msg.sources.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {msg.sources.map((s) => (
                          <span key={s.id} className="text-[8px] font-mono px-1.5 py-0.5 rounded bg-zinc-800/60 text-zinc-400">
                            {s.title} ({(s.score * 100).toFixed(0)}%)
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
                {chatSending && (
                  <div className="px-3 py-2 rounded-lg bg-zinc-900/30 border border-zinc-800/40 mr-8">
                    <Loader2 className="w-3 h-3 animate-spin text-purple-400" />
                  </div>
                )}
              </div>
              <div className="flex gap-2 mt-3">
                <input
                  type="text"
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && sendChat()}
                  placeholder="Ask about your knowledge..."
                  className="input-cyan flex-1 text-sm"
                  disabled={!connected}
                />
                <button onClick={sendChat} disabled={chatSending || !connected} className="btn-cyan text-sm">
                  <Send className="w-3 h-3" />
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  )
}

// ─── Unified Tab ────────────────────────────────────────────────────────

interface BrainGraphNode {
  id: string
  label?: string
}
interface BrainGraphLink {
  source: string | BrainGraphNode
  target: string | BrainGraphNode
  relation?: string
  weight?: number
}

function UnifiedTab({ sbStatus }: { sbStatus: SBStatus | null }): JSX.Element {
  const navigate = useNavigate()
  const [unifiedSearch, setUnifiedSearch] = useState('')
  const [unifiedResults, setUnifiedResults] = useState<{ source: string; item: Record<string, unknown>; score: number }[]>([])
  const [searching, setSearching] = useState(false)
  const [barqStats, setBarqStats] = useState<{ nodes: number; edges: number; brains: number } | null>(null)
  const [barqGraphData, setBarqGraphData] = useState<{ nodes: BrainGraphNode[]; links: BrainGraphLink[] } | null>(null)
  const [barqGraphLoading, setBarqGraphLoading] = useState(true)
  const barqGraphRef = useRef<any>(null)

  useEffect(() => {
    (async () => {
      try {
        const [brains, graphResp] = await Promise.all([
          api<{ type: string; nodes: number; edges: number }[]>('/api/brain/list'),
          api<{ nodes: BrainGraphNode[]; links: BrainGraphLink[] }>('/api/brain/visualize'),
        ])
        if (brains && Array.isArray(brains)) {
          setBarqStats({
            nodes: brains.reduce((s, b) => s + (b.nodes || 0), 0),
            edges: brains.reduce((s, b) => s + (b.edges || 0), 0),
            brains: brains.length,
          })
        }
        if (graphResp && typeof graphResp === 'object' && 'nodes' in graphResp) {
          // Normalize source/target for react-force-graph-2d
          const data = graphResp as { nodes: BrainGraphNode[]; links: BrainGraphLink[] }
          data.links = data.links.map((l) => ({
            ...l,
            source: typeof l.source === 'object' ? (l.source as BrainGraphNode).id : l.source,
            target: typeof l.target === 'object' ? (l.target as BrainGraphNode).id : l.target,
          }))
          // Precompute node degrees for hub detection
          const degreeMap: Record<string, number> = {}
          for (const l of data.links) {
            const s = typeof l.source === 'string' ? l.source : (l.source as BrainGraphNode).id
            const t = typeof l.target === 'string' ? l.target : (l.target as BrainGraphNode).id
            degreeMap[s] = (degreeMap[s] || 0) + 1
            degreeMap[t] = (degreeMap[t] || 0) + 1
          }
          for (const n of data.nodes) {
            (n as any)._degree = degreeMap[n.id] || 0
          }
          setBarqGraphData(data)
        }
      } catch { /* ignore */ }
      setBarqGraphLoading(false)
    })()
  }, [])

  // Zoom to fit on load
  useEffect(() => {
    if (barqGraphData && barqGraphRef.current) {
      setTimeout(() => {
        try { barqGraphRef.current.zoomToFit(400, 30) } catch { /* ignore */ }
      }, 600)
    }
  }, [barqGraphData])

  const handleUnifiedSearch = useCallback(async () => {
    if (!unifiedSearch.trim()) return
    setSearching(true)
    try {
      // Search both systems in parallel
      const [sbResults] = await Promise.all([
        api<SBSearchResult[]>('/api/second-brain/search', { query: unifiedSearch, mode: 'hybrid', limit: 10 }),
        // BARQ search is voice-only currently — skip for now
      ])

      const combined: { source: string; item: Record<string, unknown>; score: number }[] = []

      if (sbResults && Array.isArray(sbResults)) {
        for (const r of sbResults) {
          combined.push({ source: 'Second Brain', item: r.item as unknown as Record<string, unknown>, score: r.score })
        }
      }

      combined.sort((a, b) => b.score - a.score)
      setUnifiedResults(combined)
    } catch { /* ignore */ }
    setSearching(false)
  }, [unifiedSearch])

  return (
    <div className="space-y-4">
      {/* Stats overview */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {[
          { label: 'BARQ Brains', value: barqStats?.brains ?? '—', color: '#818cf8', icon: Brain },
          { label: 'BARQ Nodes', value: barqStats?.nodes ?? '—', color: '#22d3ee', icon: GitBranch },
          { label: 'SB Items', value: sbStatus?.stats?.total_items ?? '—', color: '#8b5cf6', icon: BookOpen },
          { label: 'SB Embedded', value: sbStatus?.stats?.embedded_items ?? '—', color: '#4ade80', icon: Database },
        ].map((stat) => {
          const Icon = stat.icon
          return (
            <div key={stat.label} className="glass-card text-center py-3">
              <Icon className="w-5 h-5 mx-auto mb-1.5" style={{ color: stat.color }} />
              <p className="text-lg font-orbitron font-bold" style={{ color: stat.color }}>{stat.value}</p>
              <p className="text-[9px] font-mono text-zinc-500 uppercase">{stat.label}</p>
            </div>
          )
        })}
      </div>

      {/* Unified search */}
      <div className="glass-card">
        <div className="flex items-center gap-2 mb-3">
          <Search className="w-5 h-5 text-cyan-400" />
          <h3 className="text-sm font-orbitron font-bold text-ghost tracking-wider">UNIFIED SEARCH</h3>
          <span className="text-[9px] font-mono text-zinc-500">Searches both BARQ + Second Brain</span>
        </div>
        <div className="flex gap-2">
          <input
            type="text"
            value={unifiedSearch}
            onChange={(e) => setUnifiedSearch(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleUnifiedSearch()}
            placeholder="Search across all knowledge sources..."
            className="input-cyan flex-1 text-sm"
          />
          <button onClick={handleUnifiedSearch} disabled={searching} className="btn-cyan text-sm">
            {searching ? <Loader2 className="w-3 h-3 animate-spin inline" /> : null}
            Search All
          </button>
        </div>

        {unifiedResults.length > 0 && (
          <div className="mt-3 space-y-2 max-h-60 overflow-y-auto">
            {unifiedResults.map((r, i) => (
              <div key={i} className="flex items-start gap-3 px-3 py-2 rounded-lg bg-zinc-900/30 border border-zinc-800/40">
                <span className="text-[8px] font-mono px-1.5 py-0.5 rounded shrink-0 mt-0.5"
                  style={{
                    backgroundColor: r.source === 'BARQ' ? '#818cf818' : '#8b5cf618',
                    color: r.source === 'BARQ' ? '#818cf8' : '#8b5cf6',
                  }}
                >
                  {r.source}
                </span>
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-rajdhani font-semibold text-ghost">{String(r.item.title || r.item.entity || 'Unknown')}</p>
                  <p className="text-[10px] font-exo text-dim-400 truncate">{String(r.item.content || '').slice(0, 100)}</p>
                </div>
                <span className="text-[9px] font-mono text-zinc-500 shrink-0">{(r.score * 100).toFixed(0)}%</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Side-by-side mini graphs */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="glass-card overflow-hidden flex flex-col" style={{ height: 400 }}>
          <div className="px-3 py-2 border-b border-zinc-800/60 flex items-center justify-between shrink-0">
            <div className="flex items-center gap-2">
              <Brain className="w-4 h-4 text-purple-400" />
              <span className="text-xs font-orbitron font-bold text-ghost tracking-wider">BARQ BRAINS</span>
            </div>
            <div className="flex items-center gap-2">
              {barqGraphData && (
                <span className="text-[9px] font-mono text-zinc-500">
                  {barqGraphData.nodes.length} nodes · {barqGraphData.links.length} edges
                </span>
              )}
              <button
                onClick={() => navigate('/brain')}
                className="text-[9px] font-mono text-cyan-400 hover:text-cyan-300 transition-colors cursor-pointer"
              >
                Full view →
              </button>
            </div>
          </div>
          <div className="flex-1 relative">
            {barqGraphLoading ? (
              <div className="absolute inset-0 flex items-center justify-center">
                <Loader2 className="w-5 h-5 animate-spin text-purple-400" />
              </div>
            ) : barqGraphData && barqGraphData.nodes.length > 0 ? (
              <ForceGraph2D
                ref={barqGraphRef}
                graphData={barqGraphData}
                backgroundColor="rgba(13,17,23,0)"
                nodeRelSize={2}
                nodeCanvasObject={(node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
                  if (node.x == null || !Number.isFinite(node.x) || node.y == null || !Number.isFinite(node.y)) return
                  const isHub = (node._degree || 0) > 20
                  const r = isHub ? 3 / globalScale : 1.8 / globalScale
                  ctx.beginPath()
                  ctx.arc(node.x, node.y, r, 0, 2 * Math.PI)
                  ctx.fillStyle = isHub ? '#a78bfa' : '#818cf880'
                  ctx.fill()
                  if (isHub) {
                    const label = (node.label || node.id).slice(0, 16)
                    const fontSize = Math.max(4, 8 / globalScale)
                    ctx.font = `${fontSize}px Inter, sans-serif`
                    ctx.textAlign = 'center'
                    ctx.textBaseline = 'top'
                    ctx.fillStyle = '#e6edf380'
                    ctx.fillText(label, node.x, node.y + r + 1)
                  }
                }}
                nodeCanvasObjectMode={() => 'replace'}
                linkCanvasObject={(link: any, ctx: CanvasRenderingContext2D) => {
                  const src = typeof link.source === 'object' ? link.source : null
                  const tgt = typeof link.target === 'object' ? link.target : null
                  if (!src || !tgt || src.x == null || tgt.x == null) return
                  ctx.beginPath()
                  ctx.moveTo(src.x, src.y)
                  ctx.lineTo(tgt.x, tgt.y)
                  ctx.strokeStyle = 'rgba(129,140,248,0.12)'
                  ctx.lineWidth = 0.3
                  ctx.stroke()
                }}
                enableNodeDrag={false}
                enableZoomInteraction={true}
                enablePanInteraction={true}
                d3AlphaDecay={0.04}
                d3VelocityDecay={0.4}
              />
            ) : (
              <div className="absolute inset-0 flex items-center justify-center">
                <div className="text-center">
                  <GitBranch className="w-8 h-8 text-zinc-600 mx-auto mb-2" />
                  <p className="text-[10px] font-mono text-zinc-500">No graph data</p>
                  <button onClick={() => navigate('/brain')} className="text-[10px] font-mono text-cyan-400 hover:underline mt-1 cursor-pointer">Open Brain →</button>
                </div>
              </div>
            )}
          </div>
        </div>
        <div className="glass-card overflow-hidden flex flex-col" style={{ height: 400 }}>
          <div className="px-3 py-2 border-b border-zinc-800/60 flex items-center justify-between shrink-0">
            <div className="flex items-center gap-2">
              <BookOpen className="w-4 h-4 text-purple-400" />
              <span className="text-xs font-orbitron font-bold text-ghost tracking-wider">SECOND BRAIN</span>
            </div>
            <div className="flex items-center gap-2">
              {sbStatus?.stats && (
                <span className="text-[9px] font-mono text-zinc-500">
                  {sbStatus.stats.total_items} items
                </span>
              )}
              <button
                onClick={() => navigate('/unified-knowledge')}
                className="text-[9px] font-mono text-purple-400 hover:text-purple-300 transition-colors cursor-pointer"
              >
                Full view →
              </button>
            </div>
          </div>
          <div className="flex-1 relative">
            {sbStatus?.connected ? (
              <SecondBrainGraph height={360} showControls={false} />
            ) : (
              <div className="absolute inset-0 flex items-center justify-center">
                <div className="text-center">
                  <WifiOff className="w-8 h-8 text-zinc-600 mx-auto mb-2" />
                  <p className="text-[10px] font-mono text-zinc-500">Not connected</p>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export default UnifiedKnowledgePage
