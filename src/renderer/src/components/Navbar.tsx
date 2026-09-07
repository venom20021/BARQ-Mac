import { useState, useEffect, useCallback, useRef, startTransition } from 'react'
import { createPortal } from 'react-dom'
import { useLocation } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { LayoutDashboard, StickyNote, ImageIcon, Smartphone, Settings, Mic } from 'lucide-react'

export type NavTab = 'DASHBOARD' | 'NOTES' | 'GALLERY' | 'PHONE' | 'SETTINGS' | 'VOICE'

export type AIState = 'idle' | 'listening' | 'thinking' | 'responding'

interface NavbarProps {
  activeTab: NavTab
  onTabChange: (tab: NavTab) => void
}

const tabs: { id: NavTab; label: string; icon: typeof LayoutDashboard }[] = [
  { id: 'DASHBOARD', label: 'Command', icon: LayoutDashboard },
  { id: 'NOTES', label: 'Notes', icon: StickyNote },
  { id: 'GALLERY', label: 'Gallery', icon: ImageIcon },
  { id: 'PHONE', label: 'Mobile', icon: Smartphone },
  { id: 'VOICE', label: 'Voice', icon: Mic },
  { id: 'SETTINGS', label: 'Settings', icon: Settings },
]

// ─── Dashboard route detection ───────────────────────────────────────

const DASHBOARD_ROUTES = new Set(['/', '/dashboard'])

function isDashboardRoute(pathname: string): boolean {
  return DASHBOARD_ROUTES.has(pathname)
}

// ─── Shared tab button rendering ─────────────────────────────────────

function renderTabButtons(
  tabs: { id: NavTab; label: string; icon: typeof LayoutDashboard }[],
  activeTab: NavTab,
  onTabChange: (tab: NavTab) => void,
): JSX.Element[] {
  return tabs.map((tab) => {
    const Icon = tab.icon
    const isActive = activeTab === tab.id
    return (
      <button
        key={tab.id}
        onClick={() => onTabChange(tab.id)}
        className={`relative flex flex-col items-center gap-0.5 px-4 py-2 text-[10px] font-medium tracking-[0.12em] uppercase transition-colors duration-200 group ${
          isActive
            ? 'text-cyan-50'
            : 'text-slate-400 hover:text-cyan-50'
        }`}
      >
        <Icon className={`w-3.5 h-3.5 transition-colors duration-200 ${
          isActive ? 'text-cyan-50' : 'text-slate-400 group-hover:text-cyan-50'
        }`} />
        <span>{tab.label}</span>

        {/* Active glowing dot indicator */}
        {isActive && (
          <span className="absolute -bottom-0.5 left-1/2 -translate-x-1/2 w-1.5 h-1.5 rounded-full bg-cyan-400 shadow-[0_0_8px_rgba(34,211,238,0.7)]" />
        )}
      </button>
    )
  })
}

export function Navbar({
  activeTab,
  onTabChange,
}: NavbarProps): JSX.Element {
  const location = useLocation()
  const isDashboard = isDashboardRoute(location.pathname)

  // ── Auto-hide state (only applies to non-dashboard routes) ────────

  const [isVisible, setIsVisible] = useState(isDashboard)
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Dashboard: always visible. Non-dashboard: start hidden.
  useEffect(() => {
    startTransition(() => {
      setIsVisible(isDashboard)
    })
  }, [isDashboard])

  // ── Mouse tracking: reveal/hide based on cursor Y position ────────

  const handleMouseMove = useCallback((e: MouseEvent) => {
    if (isDashboard) return

    if (e.clientY < 15) {
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current)
        hideTimerRef.current = null
      }
      setIsVisible(true)
    } else if (e.clientY > 100) {
      if (!hideTimerRef.current) {
        hideTimerRef.current = setTimeout(() => {
          setIsVisible(false)
          hideTimerRef.current = null
        }, 800)
      }
    }
  }, [isDashboard])

  const handleMouseEnter = useCallback(() => {
    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current)
      hideTimerRef.current = null
    }
  }, [])

  const handleMouseLeave = useCallback(() => {
    if (isDashboard) return

    if (!hideTimerRef.current) {
      hideTimerRef.current = setTimeout(() => {
        setIsVisible(false)
        hideTimerRef.current = null
      }, 800)
    }
  }, [isDashboard])

  useEffect(() => {
    window.addEventListener('mousemove', handleMouseMove)
    return () => window.removeEventListener('mousemove', handleMouseMove)
  }, [handleMouseMove])

  // ── Render ────────────────────────────────────────────────────────
  
  const navContent = (
    <>
      {!isDashboard && (
        <div className="fixed top-0 left-0 right-0 h-[15px] z-50" />
      )}

      {/* ── Non-dashboard: animated auto-hide navbar ──────────────── */}
      {/* 
        CRITICAL FIX: Framer Motion overwrites Tailwind's `-translate-x-1/2`.
        We must pass `x: "-50%"` directly into Framer Motion's animation states 
        and remove `-translate-x-1/2` from the Tailwind classes. Added `w-max`.
      */}
      <AnimatePresence>
        {!isDashboard && isVisible && (
          <motion.nav
            key="auto-nav"
            initial={{ y: -80, x: "-50%", opacity: 0 }}
            animate={{ y: 0, x: "-50%", opacity: 1 }}
            exit={{ y: -80, x: "-50%", opacity: 0 }}
            transition={{
              y: { type: 'spring', stiffness: 300, damping: 30 },
              opacity: { duration: 0.15 },
            }}
            onMouseEnter={handleMouseEnter}
            onMouseLeave={handleMouseLeave}
            className="fixed top-7 left-1/2 z-[60] pointer-events-auto w-max"
          >
            <div className="flex items-center gap-1 bg-slate-950/60 backdrop-blur-md border border-white/10 rounded-full px-2 py-1 shadow-2xl">
              {renderTabButtons(tabs, activeTab, onTabChange)}
            </div>
          </motion.nav>
        )}
      </AnimatePresence>

      {/* ── Dashboard: always-visible static navbar ────────────────── */}
      {isDashboard && (
        <nav
          key="static-nav"
          className="fixed top-7 left-1/2 -translate-x-1/2 z-[60] pointer-events-auto w-max"
        >
          <div className="flex items-center gap-1 bg-slate-950/60 backdrop-blur-md border border-white/10 rounded-full px-2 py-1 shadow-2xl">
            {renderTabButtons(tabs, activeTab, onTabChange)}
          </div>
        </nav>
      )}
    </>
  );

  return typeof document !== 'undefined' ? createPortal(navContent, document.body) : navContent;
}