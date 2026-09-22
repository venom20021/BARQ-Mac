import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('overlay', {
  // Listen for data updates from main process
  onUpdate: (callback: (data: OverlayUpdatePayload) => void) => {
    ipcRenderer.on('overlay:update', (_event, data) => callback(data))
  },

  // Listen for visibility toggle
  onToggle: (callback: (visible: boolean) => void) => {
    ipcRenderer.on('overlay:toggle', (_event, visible) => callback(visible))
  },

  // Toggle overlay visibility
  toggle: () => ipcRenderer.send('overlay:toggle'),
  show: () => ipcRenderer.send('overlay:show'),
  hide: () => ipcRenderer.send('overlay:hide'),

  // Request data refresh
  refresh: () => ipcRenderer.send('overlay:refresh'),

  // Open main window
  openMain: () => ipcRenderer.send('overlay:open-main'),

  // Remove listeners
  removeAllListeners: (channel: string) => {
    ipcRenderer.removeAllListeners(channel)
  }
})

export interface OverlayUpdatePayload {
  clock: {
    time: string
    date: string
    dayOfWeek: string
  }
  weather: {
    city: string
    temperature_c: number
    description: string
    icon: string
    humidity: number
    loaded: boolean
  } | null
  stats: {
    cpu_percent: number
    memory: { used_gb: number; total_gb: number; percent: number }
    disk: { used_gb: number; total_gb: number; percent: number }
    hostname: string
    uptime: string
    loaded: boolean
  } | null
  stocks: {
    ticker: string
    company: string
    current_price: number
    change_percent: number
    loaded: boolean
  } | null
}
