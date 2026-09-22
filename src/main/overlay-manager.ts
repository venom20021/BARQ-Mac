import { BrowserWindow, screen, ipcMain, globalShortcut } from 'electron'
import { join } from 'path'
import { is } from '@electron-toolkit/utils'
import { pythonBridge } from './python-bridge'

/**
 * BARQ Desktop Overlay Manager
 *
 * Manages a transparent, always-on-top BrowserWindow that floats on the
 * real desktop showing clock, weather, system stats, and stock ticker.
 *
 * Architecture:
 *   - Overlay is a separate BrowserWindow with transparent background
 *   - Main process polls Python backend for weather, stats, stocks
 *   - Pushes updates to overlay via webContents.send('overlay:update')
 *   - Overlay polls at: stats every 15s, weather every 5min, stocks every 2min
 */

let overlayWindow: BrowserWindow | null = null
let overlayVisible = false
let userHasMoved = false // true once the user drags the overlay; suppresses auto re-snap on show
let programmaticMove = false // set while we call setPosition so 'moved' ignores it
let statsInterval: ReturnType<typeof setInterval> | null = null
let weatherInterval: ReturnType<typeof setInterval> | null = null
let stocksInterval: ReturnType<typeof setInterval> | null = null

// ─── Settings (loaded from user preferences) ───────────────────────────────

let weatherCity = 'London'
let stockTicker = 'AAPL'

// ─── Window Management ─────────────────────────────────────────────────────

/** Move the overlay without the 'moved' handler mistaking it for a user drag. */
function setOverlayPosition(win: BrowserWindow, x: number, y: number): void {
  programmaticMove = true
  win.setPosition(x, y)
  setImmediate(() => {
    programmaticMove = false
  })
}

function getOverlayPosition(): { x: number; y: number } {
  const cursor = screen.getCursorScreenPoint()
  const displays = screen.getAllDisplays()
  // Pick the display the cursor is on, or fall back to primary
  const display = displays.find((d) => {
    const { x, y, width, height } = d.workArea
    return cursor.x >= x && cursor.x <= x + width && cursor.y >= y && cursor.y <= y + height
  }) || screen.getPrimaryDisplay()

  const { x, y, width, height } = display.workArea
  const overlayW = 544
  const overlayH = 154
  const margin = 16

  return {
    x: x + width - overlayW - margin,
    y: y + height - overlayH - margin,
  }
}

export function createOverlayWindow(): BrowserWindow {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    return overlayWindow
  }

  const pos = getOverlayPosition()

  overlayWindow = new BrowserWindow({
    width: 544,
    height: 154,
    x: pos.x,
    y: pos.y,
    transparent: true,
    frame: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    show: false,
    hasShadow: false,
    type: 'toolbar',
    focusable: false,
    webPreferences: {
      preload: join(__dirname, '../preload/overlay.js'),
      sandbox: false,
      nodeIntegration: false,
      contextIsolation: true,
      backgroundThrottling: false,
    },
  })

  // Load overlay HTML
  if (is.dev && process.env['ELECTRON_RENDERER_URL']) {
    overlayWindow.loadURL(`${process.env['ELECTRON_RENDERER_URL'].replace('/index.html', '')}/overlay.html`)
  } else {
    overlayWindow.loadFile(join(__dirname, '../renderer/overlay.html'))
  }

  overlayWindow.on('closed', () => {
    overlayWindow = null
    overlayVisible = false
  })

  // Fires whenever the window moves — native drags arrive here with
  // programmaticMove === false, so we remember the user's spot and clamp
  // the window fully back inside the work area (no cropped edges).
  overlayWindow.on('moved', () => {
    if (!overlayWindow || overlayWindow.isDestroyed()) return
    if (programmaticMove) return
    userHasMoved = true
    const bounds = overlayWindow.getBounds()
    const display = screen.getDisplayNearestPoint({
      x: Math.round(bounds.x + bounds.width / 2),
      y: Math.round(bounds.y + bounds.height / 2),
    })
    const wa = display.workArea
    const x = Math.max(wa.x, Math.min(bounds.x, wa.x + wa.width - bounds.width))
    const y = Math.max(wa.y, Math.min(bounds.y, wa.y + wa.height - bounds.height))
    if (x !== bounds.x || y !== bounds.y) {
      setOverlayPosition(overlayWindow, x, y)
    }
  })

  return overlayWindow
}

// ─── Visibility ────────────────────────────────────────────────────────────

// Lazy settings load — Python sidecar is guaranteed ready when overlay is first shown
let _settingsLoaded = false

async function ensureSettingsLoaded(): Promise<void> {
  if (_settingsLoaded) return
  _settingsLoaded = true
  await loadSettings()
}

export async function showOverlay(): Promise<void> {
  const win = overlayWindow || createOverlayWindow()
  if (win.isDestroyed()) return

  // Show window immediately — no waiting for backend. If the user dragged it
  // before, keep their position; otherwise snap to bottom-right of the display.
  if (!userHasMoved) {
    const pos = getOverlayPosition()
    setOverlayPosition(win, pos.x, pos.y)
  }
  win.showInactive()
  overlayVisible = true
  notifyToggle(true)

  // Load settings + start polling in background (may wait for Python sidecar)
  await ensureSettingsLoaded()
  startPolling()
}

export function hideOverlay(): void {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.hide()
  }
  overlayVisible = false
  stopPolling()
  notifyToggle(false)
}

export function toggleOverlay(): void {
  if (overlayVisible) {
    hideOverlay()
  } else {
    showOverlay()
  }
}

export function isOverlayVisible(): boolean {
  return overlayVisible
}

// ─── Data Polling ──────────────────────────────────────────────────────────

async function pollWeather(): Promise<void> {
  try {
    const result = await pythonBridge.request(`/web/weather?city=${encodeURIComponent(weatherCity)}`)
    if (result && typeof result === 'object') {
      const w = result as { city?: string; temperature_c?: number; description?: string; icon?: string; humidity?: number }
      if (w.city) {
        pushUpdate({
          weather: {
            city: w.city,
            temperature_c: w.temperature_c || 0,
            description: w.description || '',
            icon: w.icon || '01d',
            humidity: w.humidity || 0,
            loaded: true,
          },
        })
      }
    }
  } catch {
    // Silently retry next cycle
  }
}

async function pollStats(): Promise<void> {
  try {
    const result = await pythonBridge.request('/system/status')
    if (result && typeof result === 'object') {
      const s = result as { cpu_percent?: number; memory?: { used_gb: number; total_gb: number; percent: number }; disk?: { used_gb: number; total_gb: number; percent: number }; hostname?: string; uptime?: string }
      if (s.cpu_percent !== undefined) {
        pushUpdate({
          stats: {
            cpu_percent: s.cpu_percent,
            memory: s.memory || { used_gb: 0, total_gb: 8, percent: 0 },
            disk: s.disk || { used_gb: 0, total_gb: 256, percent: 0 },
            hostname: s.hostname || 'localhost',
            uptime: s.uptime || '',
            loaded: true,
          },
        })
      }
    }
  } catch {
    // Silently retry next cycle
  }
}

async function pollStocks(): Promise<void> {
  try {
    const result = await pythonBridge.request(`/web/stocks/${encodeURIComponent(stockTicker)}?period=1d`)
    if (result && typeof result === 'object') {
      const s = result as { ticker?: string; company?: string; current_price?: number; change_percent?: number }
      if (s.ticker) {
        pushUpdate({
          stocks: {
            ticker: s.ticker,
            company: s.company || s.ticker,
            current_price: s.current_price || 0,
            change_percent: s.change_percent || 0,
            loaded: true,
          },
        })
      }
    }
  } catch {
    // Silently retry next cycle
  }
}

function startPolling(): void {
  if (statsInterval) return // Already polling

  // Immediate first fetch
  pollWeather()
  pollStats()
  pollStocks()

  // Stats every 15 seconds
  statsInterval = setInterval(() => pollStats(), 15_000)

  // Weather every 5 minutes
  weatherInterval = setInterval(() => pollWeather(), 300_000)

  // Stocks every 2 minutes
  stocksInterval = setInterval(() => pollStocks(), 120_000)
}

function stopPolling(): void {
  if (statsInterval) {
    clearInterval(statsInterval)
    statsInterval = null
  }
  if (weatherInterval) {
    clearInterval(weatherInterval)
    weatherInterval = null
  }
  if (stocksInterval) {
    clearInterval(stocksInterval)
    stocksInterval = null
  }
}

// ─── Push data to overlay window ───────────────────────────────────────────

interface UpdatePayload {
  weather?: {
    city: string
    temperature_c: number
    description: string
    icon: string
    humidity: number
    loaded: boolean
  }
  stats?: {
    cpu_percent: number
    memory: { used_gb: number; total_gb: number; percent: number }
    disk: { used_gb: number; total_gb: number; percent: number }
    hostname: string
    uptime: string
    loaded: boolean
  }
  stocks?: {
    ticker: string
    company: string
    current_price: number
    change_percent: number
    loaded: boolean
  }
}

function pushUpdate(payload: UpdatePayload): void {
  if (overlayWindow && !overlayWindow.isDestroyed() && overlayVisible) {
    overlayWindow.webContents.send('overlay:update', payload)
  }
}

function notifyToggle(visible: boolean): void {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.webContents.send('overlay:toggle', visible)
  }
}

// ─── Load user settings ────────────────────────────────────────────────────

async function loadSettings(): Promise<void> {
  try {
    const result = await pythonBridge.request('/voice/status')
    if (result && typeof result === 'object') {
      const status = result as { weather_city?: string }
      if (status.weather_city) {
        weatherCity = status.weather_city
      }
    }
  } catch {
    weatherCity = 'London'
  }
}

// ─── Init / Cleanup ────────────────────────────────────────────────────────

// Settings loading is moved to showOverlay() so Python sidecar is guaranteed ready

let _displayMetricsListenerRegistered = false

export function initOverlayManager(): void {
  // Register IPC handlers for the overlay
  // Register display-metrics-changed listener once
  if (!_displayMetricsListenerRegistered) {
    screen.on('display-metrics-changed', () => {
      if (overlayWindow && !overlayWindow.isDestroyed() && overlayVisible) {
        if (userHasMoved) {
          // Respect the user's chosen spot; just pull them back on-screen
          const b = overlayWindow.getBounds()
          const d = screen.getDisplayNearestPoint({
            x: Math.round(b.x + b.width / 2),
            y: Math.round(b.y + b.height / 2),
          })
          const wa = d.workArea
          setOverlayPosition(
            overlayWindow,
            Math.max(wa.x, Math.min(b.x, wa.x + wa.width - b.width)),
            Math.max(wa.y, Math.min(b.y, wa.y + wa.height - b.height))
          )
        } else {
          const newPos = getOverlayPosition()
          setOverlayPosition(overlayWindow, newPos.x, newPos.y)
        }
      }
    })
    _displayMetricsListenerRegistered = true
  }
  ipcMain.on('overlay:toggle', () => toggleOverlay())
  ipcMain.on('overlay:show', () => showOverlay())
  ipcMain.on('overlay:hide', () => hideOverlay())

  // NOTE: 'overlay:move-to' removed — dragging is now native via
  // -webkit-app-region: drag (renderer OverlayApp), which eliminated the
  // IPC-round-trip stickiness. Position clamping happens on 'moved' above.

  ipcMain.on('overlay:refresh', () => {
    if (overlayVisible) {
      pollWeather()
      pollStats()
      pollStocks()
    }
  })

  ipcMain.on('overlay:open-main', () => {
    const wins = BrowserWindow.getAllWindows().filter(w => !w.isDestroyed())
    const mainWin = wins.find(w => w !== overlayWindow)
    if (mainWin) {
      if (mainWin.isMinimized()) mainWin.restore()
      mainWin.show()
      mainWin.focus()
    }
  })

  // Register global shortcut: Ctrl+Shift+O (⌘+Shift+O on macOS)
  const registered = globalShortcut.register('CommandOrControl+Shift+O', () => {
    console.log('[Overlay] Global shortcut Ctrl+Shift+O triggered')
    toggleOverlay()
  })
  if (registered) {
    console.log('[Overlay] Global shortcut Ctrl+Shift+O registered successfully')
  } else {
    console.warn('[Overlay] Failed to register global shortcut Ctrl+Shift+O — possible conflict')
  }
}

export function destroyOverlayManager(): void {
  stopPolling()
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.close()
  }
  overlayWindow = null
  overlayVisible = false
}
