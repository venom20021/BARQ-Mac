import { useEffect, useState, useCallback } from 'react'
import { OverlayClock } from './OverlayClock'
import { OverlayWeather } from './OverlayWeather'
import { OverlayStats } from './OverlayStats'
import { OverlayStocks } from './OverlayStocks'

// Inline type to avoid cross-entry import from preload
interface OverlayUpdatePayload {
  weather?: {
    city: string
    temperature_c: number
    description: string
    icon: string
    humidity: number
    loaded: boolean
  } | null
  stats?: {
    cpu_percent: number
    memory: { used_gb: number; total_gb: number; percent: number }
    disk: { used_gb: number; total_gb: number; percent: number }
    hostname: string
    uptime: string
    loaded: boolean
  } | null
  stocks?: {
    ticker: string
    company: string
    current_price: number
    change_percent: number
    loaded: boolean
  } | null
}

const EMPTY_WEATHER = { city: '', temperature_c: 0, description: '', icon: '', humidity: 0, loaded: false }
const EMPTY_STATS = { cpu_percent: 0, memory: { used_gb: 0, total_gb: 0, percent: 0 }, disk: { used_gb: 0, total_gb: 0, percent: 0 }, hostname: '', uptime: '', loaded: false }
const EMPTY_STOCKS = { ticker: '---', company: '', current_price: 0, change_percent: 0, loaded: false }

export function OverlayApp(): JSX.Element {
  const [weather, setWeather] = useState(EMPTY_WEATHER)
  const [stats, setStats] = useState(EMPTY_STATS)
  const [stocks, setStocks] = useState(EMPTY_STOCKS)

  const handleUpdate = useCallback((data: OverlayUpdatePayload) => {
    if (data.weather) setWeather(data.weather)
    if (data.stats) setStats(data.stats)
    if (data.stocks) setStocks(data.stocks)
  }, [])

  useEffect(() => {
    // Listen for data pushed from main process
    const overlayApi = (window as unknown as { overlay?: { onUpdate: (cb: (data: OverlayUpdatePayload) => void) => void; onToggle: (cb: (v: boolean) => void) => void; refresh: () => void; removeAllListeners: (c: string) => void } }).overlay
    if (overlayApi) {
      overlayApi.onUpdate(handleUpdate)

      // Request initial data via IPC bridge
      overlayApi.refresh()
    }

    return () => {
      if (overlayApi) {
        overlayApi.removeAllListeners('overlay:update')
      }
    }
  }, [handleUpdate])

  const overlayApi = (window as unknown as {
    overlay?: {
      hide: () => void
      openMain: () => void
    }
  }).overlay

  // Dragging is native: `.overlay-container` uses -webkit-app-region: drag,
  // so the OS moves the window directly (zero IPC, no stickiness). Interactive
  // bits must opt out with -webkit-app-region: no-drag in styles.css.

  const handleClose = useCallback(() => {
    overlayApi?.hide()
  }, [overlayApi])

  // The clock panel is a no-drag zone (see styles.css) so it can receive
  // clicks/double-clicks while the rest of the panel drags the window.
  const handleOpenMain = useCallback(() => {
    overlayApi?.openMain()
  }, [overlayApi])

  return (
    <div className="overlay-container">
      {/* Drag handle area (visual affordance; whole panel drags natively) */}
      <div className="overlay-drag-handle">
        <div className="drag-handle-dots">
          <span /><span /><span />
        </div>
      </div>

      {/* Close button */}
      <button className="overlay-close-btn" onClick={handleClose} title="Hide overlay">
        ✕
      </button>

      {/* Widgets */}
      <div className="overlay-dblclick-zone" onDoubleClick={handleOpenMain} title="Double-click to open BARQ">
        <OverlayClock />
      </div>
      <OverlayWeather weather={weather} />
      <OverlayStats stats={stats} />
      <OverlayStocks stocks={stocks} />
    </div>
  )
}
