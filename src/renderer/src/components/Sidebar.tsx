import { useState, useEffect, useRef, useCallback } from 'react'
import {
  LayoutDashboard, Briefcase, Video, BarChart3, Settings,
  FolderOpen, Terminal, Monitor, Globe, Smartphone, Search,
  FileText, MessageSquare, Palette, PanelRightOpen,
  Cpu, Eye, Zap, BookOpen, GitBranch, Activity, Workflow, Link2,
} from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { NotificationCenter } from './NotificationCenter'
import { PythonHealthBadge } from './PythonHealthBadge'

interface SidebarProps {
  currentRoute: string
  onNavigate: (route: string) => void
}

interface NavItemDef {
  path: string
  label: string
  icon: typeof LayoutDashboard
}

const navSections: { label: string; items: NavItemDef[] }[] = [
  {
    label: 'Overview',
    items: [
      { path: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
      { path: '/analytics', label: 'Analytics', icon: BarChart3 },
    ],
  },
  {
    label: 'Workspace',
    items: [
      { path: '/files', label: 'Files', icon: FolderOpen },
      { path: '/dev', label: 'Dev', icon: Terminal },
      { path: '/system', label: 'System', icon: Monitor },
      { path: '/web', label: 'Web', icon: Globe },
      { path: '/phone', label: 'Phone', icon: Smartphone },
      { path: '/research', label: 'Research', icon: Search },
      { path: '/docs', label: 'Docs', icon: FileText },
    ],
  },
  {
    label: 'Automation',
    items: [
      { path: '/jobs', label: 'Jobs', icon: Briefcase },
      { path: '/content', label: 'Social', icon: Video },
      { path: '/workflows', label: 'Workflows', icon: Workflow },
    ],
  },
  {
    label: 'Tools',
    items: [
      { path: '/chat', label: 'Chat', icon: MessageSquare },
      { path: '/memory', label: 'Notes & Storage', icon: BookOpen },
      { path: '/brain', label: 'Knowledge Graph', icon: GitBranch },
      { path: '/unified-knowledge', label: 'Unified Knowledge', icon: Link2 },
      { path: '/agent', label: 'Agent', icon: Cpu },
      { path: '/vision', label: 'Vision', icon: Eye },
      { path: '/evolution', label: 'Evolution', icon: Activity },
      { path: '/apis', label: 'APIs', icon: Zap },
      { path: '/widgets', label: 'Widgets', icon: Palette },
      { path: '/settings', label: 'Settings', icon: Settings },
    ],
  },
]

// ─── Dock size presets ──────────────────────────────────────────────

type DockSize = 'sm' | 'md' | 'lg'

const DOCK_SIZE_KEY = 'barq_dock_size'

const DOCK_SIZES: Record<DockSize, { btn: string; icon: string; px: string; py: string }> = {
  sm: { btn: 'w-6 h-6', icon: 'w-2.5 h-2.5', px: 'px-1.5', py: 'py-0.5' },
  md: { btn: 'w-8 h-8', icon: 'w-3.5 h-3.5', px: 'px-2', py: 'py-1' },
  lg: { btn: 'w-10 h-10', icon: 'w-4 h-4', px: 'px-3', py: 'py-1.5' },
}

const SIZE_LABELS: Record<DockSize, string> = { sm: 'S', md: 'M', lg: 'L' }

function getStoredDockSize(): DockSize {
  try {
    const stored = localStorage.getItem(DOCK_SIZE_KEY)
    if (stored === 'sm' || stored === 'md' || stored === 'lg') return stored
  } catch { /* ignore */ }
  return 'md'
}

export function Sidebar({ currentRoute, onNavigate }: SidebarProps): JSX.Element {
  const scrollRef = useRef<HTMLDivElement>(null)
  const [dockSize, setDockSize] = useState<DockSize>(getStoredDockSize)
  const [isVisible, setIsVisible] = useState(true)
  const hideTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const startupTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const isOverDockRef = useRef(false)
  const size = DOCK_SIZES[dockSize]
  // macOS dock magnification: track which item is hovered (-1 = none)
  const [hoveredIndex, setHoveredIndex] = useState(-1)

  // ── Auto-hide after 3s on initial load (guarantees hide even without mouse movement) ─
  useEffect(() => {
    startupTimerRef.current = setTimeout(() => {
      setIsVisible(false)
    }, 3000)
    return () => {
      if (startupTimerRef.current) clearTimeout(startupTimerRef.current)
    }
  }, [])

  // ── Broadcast visibility changes so the navbar can intensify its blur ─
  useEffect(() => {
    window.dispatchEvent(
      new CustomEvent('barq:dock-visibility', { detail: { visible: isVisible } }),
    )
  }, [isVisible])

  // ── macOS-style auto-hide: show when mouse nears bottom edge ─────
  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      const nearBottom = window.innerHeight - e.clientY <= 10
      if (nearBottom) {
        // Cancel startup timer on first interaction
        if (startupTimerRef.current) {
          clearTimeout(startupTimerRef.current)
          startupTimerRef.current = null
        }
        if (hideTimeoutRef.current) {
          clearTimeout(hideTimeoutRef.current)
          hideTimeoutRef.current = null
        }
        setIsVisible(true)
      } else if (!isOverDockRef.current) {
        if (!hideTimeoutRef.current) {
          hideTimeoutRef.current = setTimeout(() => {
            setIsVisible(false)
            hideTimeoutRef.current = null
          }, 800)
        }
      }
    }

    window.addEventListener('mousemove', handleMouseMove)
    return () => {
      window.removeEventListener('mousemove', handleMouseMove)
      if (hideTimeoutRef.current) clearTimeout(hideTimeoutRef.current)
    }
  }, [])

  const handleMouseEnter = useCallback(() => {
    isOverDockRef.current = true
    // Cancel startup timer if user hovers dock before 3s elapses
    if (startupTimerRef.current) {
      clearTimeout(startupTimerRef.current)
      startupTimerRef.current = null
    }
    if (hideTimeoutRef.current) {
      clearTimeout(hideTimeoutRef.current)
      hideTimeoutRef.current = null
    }
    setIsVisible(true)
  }, [])

  const handleMouseLeave = useCallback(() => {
    isOverDockRef.current = false
    hideTimeoutRef.current = setTimeout(() => {
      setIsVisible(false)
      hideTimeoutRef.current = null
    }, 400)
  }, [])

  // ── Cycle dock size ───────────────────────────────────────────────
  const cycleDockSize = useCallback(() => {
    setDockSize((prev) => {
      const next: DockSize = prev === 'sm' ? 'md' : prev === 'md' ? 'lg' : 'sm'
      try { localStorage.setItem(DOCK_SIZE_KEY, next) } catch { /* ignore */ }
      return next
    })
  }, [])

  // Flatten all nav items
  const flattenedItems: NavItemDef[] = []
  navSections.forEach((section) => {
    section.items.forEach((item) => {
      flattenedItems.push(item)
    })
  })

  return (
    <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-40 max-w-[calc(100vw-32px)]">
      <AnimatePresence>
        {isVisible && (
          <motion.div
            initial={{ opacity: 0, y: 40 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 40 }}
            transition={{ duration: 0.2, ease: 'easeOut' }}
            onMouseEnter={handleMouseEnter}
            onMouseLeave={handleMouseLeave}
            className={`flex items-center gap-0.5 ${size.px} ${size.py} rounded-xl backdrop-blur-2xl bg-void-900/70 border border-white/[0.06] shadow-2xl`}
          >
            {/* Nav items — macOS dock magnification on hover */}
            <div
              ref={scrollRef}
              className="flex items-center gap-0.5 overflow-x-auto flex-1 min-w-0"
              onMouseLeave={() => setHoveredIndex(-1)}
            >
              {flattenedItems.map((item, i) => {
                const isActive = currentRoute === item.path
                const Icon = item.icon

                // macOS fisheye magnification: scale based on distance from hovered item
                let scale = 1
                if (hoveredIndex !== -1) {
                  const dist = Math.abs(i - hoveredIndex)
                  if (dist === 0) scale = 1.5
                  else if (dist === 1) scale = 1.25
                  else if (dist === 2) scale = 1.1
                }

                return (
                  <button
                    key={item.path}
                    onClick={() => onNavigate(item.path)}
                    onMouseEnter={() => setHoveredIndex(i)}
                    className={`relative flex items-center justify-center rounded-lg transition-all duration-150 ease-out group`}
                    title={item.label}
                    style={{
                      width: `calc(${size.btn.split(' ')[0].replace('w-', '')} * 0.25rem * ${scale})`,
                      height: `calc(${size.btn.split(' ')[1].replace('h-', '')} * 0.25rem * ${scale})`,
                      transition: 'width 0.15s ease-out, height 0.15s ease-out',
                    }}
                  >
                    <Icon
                      className={`transition-all duration-200 ${
                        isActive
                          ? 'text-cyan-300 drop-shadow-[0_0_6px_rgba(34,211,238,0.4)]'
                          : 'text-white/30 group-hover:text-white/60'
                      }`}
                      style={{
                        width: `calc(${size.icon.split(' ')[0].replace('w-', '')} * 0.25rem * ${scale})`,
                        height: `calc(${size.icon.split(' ')[1].replace('h-', '')} * 0.25rem * ${scale})`,
                        transition: 'width 0.15s ease-out, height 0.15s ease-out',
                      }}
                    />

                    {/* Active glow */}
                    {isActive && (
                      <motion.span
                        layoutId="dock-active-glow"
                        className="absolute inset-0 rounded-lg bg-cyan-400/10 shadow-[0_0_12px_rgba(34,211,238,0.15)]"
                      />
                    )}
                  </button>
                )
              })}
            </div>

            {/* Status area */}
            <div className="flex items-center gap-0.5 pl-2 ml-0.5 border-l border-white/[0.05] shrink-0">
              <PythonHealthBadge />
              <NotificationCenter />
              {/* Dock size cycle */}
              <button
                onClick={cycleDockSize}
                className="p-1 rounded-lg text-white/25 hover:text-cyan-300 hover:bg-white/5 transition-all text-[9px] font-mono font-semibold tracking-wide"
                title={`Dock size: ${SIZE_LABELS[dockSize]} — click to change`}
              >
                {SIZE_LABELS[dockSize]}
              </button>
              <button
                className="p-1 rounded-lg text-white/25 hover:text-cyan-300 hover:bg-white/5 transition-all"
                title="Quick Overlay (Ctrl+Shift+I)"
              >
                <PanelRightOpen className="w-3 h-3" />
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
