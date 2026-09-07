import { useState, useEffect, useCallback, Suspense, lazy } from 'react'
import {
  MemoryRouter, Routes, Route, useNavigate, useLocation,
} from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { Sidebar } from './components/Sidebar'
import { AppErrorBoundary } from './components/AppErrorBoundary'
import { QuickOverlay} from './components/QuickOverlay'
import { StartupSequence } from './components/StartupSequence'
import { ApprovalModal } from './components/ApprovalModal'
import { TransientDiagnostics } from './components/TransientDiagnostics'
import { UpdateToast } from './components/UpdateToast'
import { Navbar } from './components/Navbar'
import type { NavTab } from './components/Navbar'
import { LiveCaptions } from './components/LiveCaptions'
import { DynamicContentPanel } from './components/DynamicContentPanel'
import type { RichContent } from './components/DynamicContentTypes'
import { ThemeProvider } from './contexts/ThemeContext'
import { VoiceProvider, useVoice } from './contexts/VoiceContext'
import { DashboardPage } from './pages/DashboardPage'
import { AnalyticsPage } from './pages/AnalyticsPage'
import { JobsPage } from './pages/JobsPage'
import { ContentPage } from './pages/ContentPage'
import { FilesPage } from './pages/FilesPage'
import { DevPage } from './pages/DevPage'
import { SystemPage } from './pages/SystemPage'
import { WebPage } from './pages/WebPage'
import { PhonePage } from './pages/PhonePage'
import { ResearchPage } from './pages/ResearchPage'
import { DocsPage } from './pages/DocsPage'
import { ChatPage } from './pages/ChatPage'
import { MemoryPage } from './pages/MemoryPage'
import { WidgetsPage } from './pages/WidgetsPage'
import { SettingsPage } from './pages/SettingsPage'
import { AgentPage } from './pages/AgentPage'
import { WorkflowsPage } from './pages/WorkflowsPage'
import { VisionPage } from './pages/VisionPage'
import { BrainPage } from './pages/BrainPage'
import { PublicApisPage } from './pages/PublicApisPage'
import { EvolutionPage } from './pages/EvolutionPage'
import { VoiceSkillsPage } from './pages/VoiceSkillsPage'
import { UnifiedKnowledgePage } from './pages/UnifiedKnowledgePage'

// Lazy loaded views for the main navbar tabs
const NotesView = lazy(() => import('./views/NotesView'))
const GalleryView = lazy(() => import('./views/GalleryView'))

// ─── Quick Command Router ──────────────────────────────────────────────────

function processQuickCommand(cmd: string, nav: (route: string) => void): void {
  // ── Page Navigation (all BARQ pages) ─────────────────────────────
  const navigateIfMatch = (): boolean => {
    const routeMap: Record<string, string> = {
      // Primary pages
      'dashboard': '/dashboard', 'home': '/dashboard', 'command': '/dashboard',
      'analytics': '/analytics', 'insights': '/analytics',
      'jobs': '/jobs', 'job': '/jobs', 'careers': '/jobs', 'career': '/jobs', 'recruitment': '/jobs',
      'social': '/content', 'content': '/content', 'social media': '/content',
      'files': '/files', 'file': '/files', 'explorer': '/files',
      'dev': '/dev', 'developer': '/dev', 'terminal': '/dev',
      'system': '/system', 'hardware': '/system', 'monitor': '/system',
      'web': '/web', 'browser': '/web', 'weather': '/web', 'stocks': '/web',
      'phone': '/phone', 'mobile': '/phone',
      'research': '/research', 'search': '/research', 'deep research': '/research',
      'docs': '/docs', 'documents': '/docs', 'documentation': '/docs',
      'chat': '/chat', 'conversation': '/chat', 'talk': '/chat',
      'memory': '/memory', 'notes & storage': '/memory', 'storage': '/memory',
      'widgets': '/widgets', 'widget': '/widgets',
      'settings': '/settings', 'preferences': '/settings', 'config': '/settings',
      'agent': '/agent', 'ai agent': '/agent', 'automation agent': '/agent',
      'workflows': '/workflows', 'workflow': '/workflows', 'automations': '/workflows',
      'vision': '/vision', 'camera': '/vision', 'screen analysis': '/vision',
      'brain': '/brain', 'knowledge': '/brain', 'knowledge graph': '/brain', 'graph': '/brain',
      'apis': '/apis', 'api': '/apis', 'public apis': '/apis', 'integrations': '/apis',
      'evolution': '/evolution', 'evolve': '/evolution', 'learning': '/evolution',
      'voice skills': '/voice-skills', 'voice': '/voice-skills', 'skills': '/voice-skills', 'voice commands': '/voice-skills', 'commands': '/voice-skills',
      'unified knowledge': '/unified-knowledge', 'unified': '/unified-knowledge', 'second brain': '/unified-knowledge',
      'notes': '/notes', 'note': '/notes',
      'gallery': '/gallery', 'images': '/gallery', 'photos': '/gallery',
    }

    for (const [key, route] of Object.entries(routeMap)) {
      if (cmd.includes(key)) {
        nav(route)
        return true
      }
    }
    return false
  }

  if (cmd.includes('open') || cmd.includes('navigate') || cmd.includes('go to') ||
      cmd.includes('show') || cmd.includes('switch to') || cmd.includes('take me to') ||
      cmd.includes('take me') || cmd.includes('page')) {
    if (navigateIfMatch()) return
  }

  // ── Direct navigation (without open/navigate prefix) ──────────────
  // e.g. "jobs" / "settings" / "brain"
  if (navigateIfMatch()) return

  // ── Feature-specific voice commands ───────────────────────────────
  if (cmd.includes('scan') && cmd.includes('job')) {
    void window.barq?.jobs.scan()
    return
  } else if (cmd.includes('trend') || cmd.includes('trending')) {
    void window.barq?.social.trends()
    return
  } else if (cmd.includes('create note') || cmd.includes('new note')) {
    nav('/notes')
    return
  } else if (cmd.includes('weather')) {
    const city = cmd.replace('weather', '').replace('in', '').trim() || 'London'
    nav(`/web?weather=${encodeURIComponent(city)}`)
    return
  } else if (cmd.includes('stock') || cmd.includes('price')) {
    nav('/web?tab=stocks')
    return
  } else if (cmd.includes('approval') && cmd.includes('clear')) {
    void window.barq?.system.command.clearApprovals()
    return
  } else if (cmd.includes('approval')) {
    nav('/settings')
    return
  } else if (cmd.includes('briefing') || cmd.includes('morning report')) {
    nav('/agent')
    return
  } else if (cmd.includes('weekly review') || cmd.includes('week review')) {
    nav('/agent')
    return
  } else if (cmd.includes('diagnostics') || cmd.includes('system status')) {
    window.dispatchEvent(
      new CustomEvent('barq:voice-command', { detail: { action: 'show_diagnostics' } })
    )
    return
  } else if (cmd.includes('overlay')) {
    if (cmd.includes('show')) {
      window.barq?.overlay.show()
    } else if (cmd.includes('hide')) {
      window.barq?.overlay.hide()
    } else {
      window.barq?.overlay.toggle()
    }
    return
  } else if (cmd.includes('voice') || cmd.includes('listen') || cmd.includes('wake word')) {
    void window.barq?.voice.start()
    return
  }

  // ── Second Brain Integration ────────────────────────────────────────
  if (cmd.includes('search second brain') || cmd.includes('search my notes') || cmd.includes('find in second brain')) {
    const query = cmd.replace(/search (second brain|my notes|find in second brain)/gi, '').trim()
    if (query) {
      void window.barq?.api('POST', '/second-brain/search', { query, mode: 'hybrid', limit: 10 })
    }
    nav('/unified-knowledge')
    return
  } else if (cmd.includes('sync knowledge') || cmd.includes('sync second brain') || cmd.includes('sync everything')) {
    void window.barq?.api('POST', '/second-brain/sync/full', { direction: 'both' })
    return
  } else if (cmd.includes('sync status') || cmd.includes('sync status')) {
    nav('/unified-knowledge')
    return
  } else if (cmd.includes('second brain') && (cmd.includes('start sync') || cmd.includes('auto sync'))) {
    void window.barq?.api('POST', '/second-brain/sync/auto/start', { interval_seconds: 300 })
    return
  } else if (cmd.includes('second brain') && cmd.includes('stop sync')) {
    void window.barq?.api('POST', '/second-brain/sync/auto/stop')
    return
  } else if (cmd.includes('second brain status') || cmd.includes('second brain connected')) {
    nav('/unified-knowledge')
    return
  } else if (cmd.includes('chat with second brain') || cmd.includes('ask second brain') || cmd.includes('second brain question')) {
    const question = cmd.replace(/(chat with |ask |question from )?second brain/gi, '').trim()
    if (question) {
      void window.barq?.api('POST', '/second-brain/chat', { message: question })
    }
    nav('/unified-knowledge')
    return
  } else {
    void window.barq?.voice.command(cmd)
  }
}

// ─── Page Transition Wrapper ───────────────────────────────────────────────

import type { Variants } from 'framer-motion'

const pageVariants: Variants = {
  initial: { opacity: 0, x: 20 },
  animate: { opacity: 1, x: 0 },
  exit: { opacity: 0, x: -20 },
}

function AnimatedPage({ children }: { children: React.ReactNode }): JSX.Element {
  return (
    <motion.div
      variants={pageVariants}
      initial="initial"
      animate="animate"
      exit="exit"
      transition={{ duration: 0.2, ease: 'easeOut' }}
      className="h-full"
    >
      {children}
    </motion.div>
  )
}

// ─── Tab View Wrapper (no animated transitions for tab views) ──────────────

function TabView({ children }: { children: React.ReactNode }): JSX.Element {
  return <div className="h-full">{children}</div>
}

// ─── Live Captions Wrapper (inside VoiceProvider) ──────────────────────────

function LiveCaptionsWrapper(): JSX.Element {
  const { sttText, responseText, aiState, voiceListening, richContent, clearRichContent } = useVoice()
  return (
    <>
      <DynamicContentPanel
        content={richContent}
        onDismiss={clearRichContent}
      />
      <LiveCaptions
        sttText={sttText}
        responseText={responseText}
        isSpeaking={aiState === 'responding'}
        isProcessing={aiState === 'thinking'}
        conversationActive={voiceListening || aiState !== 'idle'}
      />
    </>
  )
}

// ─── Map route to navbar tab ──────────────────────────────────────────────

function routeToTab(pathname: string): NavTab {
  if (pathname === '/' || pathname.startsWith('/dashboard')) return 'DASHBOARD'
  if (pathname.startsWith('/notes')) return 'NOTES'
  if (pathname.startsWith('/gallery')) return 'GALLERY'
  if (pathname.startsWith('/phone')) return 'PHONE'
  if (pathname.startsWith('/voice-skills')) return 'VOICE'
  if (pathname.startsWith('/settings')) return 'SETTINGS'
  return 'DASHBOARD'
}

// ─── App Content ───────────────────────────────────────────────────────────

function AppContent(): JSX.Element {
  const navigate = useNavigate()
  const location = useLocation()
  const [bootComplete, setBootComplete] = useState(false)
  const [quickOverlay, setQuickOverlay] = useState<{
    visible: boolean
    position: { x: number; y: number }
  }>({ visible: false, position: { x: 0, y: 0 } })
  const [recentCommands, setRecentCommands] = useState<string[]>([])

  const activeTab = routeToTab(location.pathname)

  const handleTabChange = useCallback((tab: NavTab) => {
    const routeMap: Record<NavTab, string> = {
      DASHBOARD: '/dashboard',
      NOTES: '/notes',
      GALLERY: '/gallery',
      PHONE: '/phone',
      SETTINGS: '/settings',
      VOICE: '/voice-skills',
    }
    navigate(routeMap[tab])
  }, [navigate])

  // Listen for barq:voice-command events from ChatPage and voice pipeline
  useEffect(() => {
    const handler = (e: CustomEvent<{ action: string }>): void => {
      const action = e.detail?.action
      if (action === 'clear_approvals') {
        void window.barq?.system.command.clearApprovals()
      } else if (action === 'overlay_show') {
        window.barq?.overlay.show()
      } else if (action === 'overlay_hide') {
        window.barq?.overlay.hide()
      } else if (action === 'overlay_toggle') {
        window.barq?.overlay.toggle()
      }
    }
    window.addEventListener('barq:voice-command', handler as EventListener)
    return () => window.removeEventListener('barq:voice-command', handler as EventListener)
  }, [])

  useEffect(() => {
    // Track cleanup functions returned by preload listeners
    const cleanups: (() => void)[] = []

    if (window.barq?.onNavigate) {
      const cleanup = window.barq.onNavigate((route: string) => navigate(route))
      if (typeof cleanup === 'function') cleanups.push(cleanup)
    }

    if (window.barq?.onQuickOverlay) {
      const cleanup = window.barq.onQuickOverlay((pos: { x: number; y: number }) => {
        setQuickOverlay({ visible: true, position: pos })
      })
      if (typeof cleanup === 'function') cleanups.push(cleanup)
    }

    const handleQuickCmd = (e: CustomEvent<{ command: string }>): void => {
      const cmd = e.detail.command.toLowerCase()
      setRecentCommands((prev) => [cmd, ...prev.slice(0, 9)])
      processQuickCommand(cmd, navigate)
    }
    window.addEventListener(
      'barq:quick-command',
      handleQuickCmd as EventListener,
    )
    return () => {
      window.removeEventListener(
        'barq:quick-command',
        handleQuickCmd as EventListener,
      )
      // Run all preload listener cleanups
      for (const cleanup of cleanups) {
        cleanup()
      }
    }
  }, [navigate])

  const handleQuickOverlayClose = useCallback((): void => {
    setQuickOverlay((prev) => ({ ...prev, visible: false }))
  }, [])

  const handleBootComplete = useCallback((): void => {
    setBootComplete(true)
  }, [])

  // ─── Render ─────────────────────────────────────────────────────────────

  return (
    <>
      {/* Startup Sequence */}
      <AnimatePresence>
        {!bootComplete && (
          <StartupSequence onComplete={handleBootComplete} />
        )}
      </AnimatePresence>

      {/* Radial gradient background (replaces ParticleField) */}
      <div className="fixed inset-0 bg-[radial-gradient(ellipse_at_center,var(--tw-gradient-stops))] from-zinc-950 via-black to-black pointer-events-none" />

      {/* Main layout — header is absolute, so content fills full screen */}
      <div className="relative z-10 h-screen">
        {/* Centered nav (floating) */}
        <Navbar
          activeTab={activeTab}
          onTabChange={handleTabChange}
        />

        {/* Content area: Sidebar (fixed dock) + Main */}
        <div className="h-full flex overflow-hidden">
          {/* Sidebar is fixed-position at bottom */}
          <Sidebar currentRoute={location.pathname} onNavigate={navigate} />

          <main className="flex-1 flex flex-col overflow-hidden pt-2">
            <div className="flex-1 overflow-y-auto relative">
              {/* Scanline overlay */}
              <div
                className="fixed inset-0 pointer-events-none z-50 opacity-[0.015]"
                style={{
                  backgroundImage:
                    'repeating-linear-gradient(transparent 0px, transparent 2px, rgba(0,240,255,0.02) 2px, rgba(0,240,255,0.02) 4px)',
                }}
              />

              {/* Page content with transitions — wrapped in an error boundary so a
                  render crash in any page shows a recovery screen instead of a
                  blank window. The key remounts the boundary on navigation, which
                  both drives AnimatePresence and clears any caught crash. */}
              <AnimatePresence mode="wait">
                <AppErrorBoundary
                  key={location.pathname}
                  variant="inline"
                  title="This view crashed"
                  onReset={() => navigate('/dashboard')}
                >
                <Routes location={location} key={location.pathname}>
                  {/* Main navbar tabs */}
                  <Route path="/" element={<TabView><DashboardPage /></TabView>} />
                  <Route path="/dashboard" element={<TabView><DashboardPage /></TabView>} />
                  <Route path="/notes" element={
                    <Suspense fallback={
                      <div className="flex h-full items-center justify-center">
                        <div className="w-5 h-5 border-2 border-cyan-500/30 border-t-cyan-400 rounded-full animate-spin" />
                      </div>
                    }>
                      <TabView><NotesView glassPanel="bg-zinc-950/40 backdrop-blur-xl border border-white/5 rounded-2xl shadow-xl" /></TabView>
                    </Suspense>
                  } />
                  <Route path="/gallery" element={
                    <Suspense fallback={
                      <div className="flex h-full items-center justify-center">
                        <div className="w-5 h-5 border-2 border-cyan-500/30 border-t-cyan-400 rounded-full animate-spin" />
                      </div>
                    }>
                      <TabView><GalleryView /></TabView>
                    </Suspense>
                  } />
                  <Route path="/phone" element={<TabView><PhonePage /></TabView>} />
                  <Route path="/settings" element={<TabView><SettingsPage /></TabView>} />

                  {/* Sidebar secondary pages */}
                  <Route path="/analytics" element={<AnimatedPage><AnalyticsPage /></AnimatedPage>} />
                  <Route path="/jobs" element={<AnimatedPage><JobsPage /></AnimatedPage>} />
                  <Route path="/content" element={<AnimatedPage><ContentPage /></AnimatedPage>} />
                  <Route path="/files" element={<AnimatedPage><FilesPage /></AnimatedPage>} />
                  <Route path="/dev" element={<AnimatedPage><DevPage /></AnimatedPage>} />
                  <Route path="/system" element={<AnimatedPage><SystemPage /></AnimatedPage>} />
                  <Route path="/web" element={<AnimatedPage><WebPage /></AnimatedPage>} />
                  <Route path="/research" element={<AnimatedPage><ResearchPage /></AnimatedPage>} />
                  <Route path="/docs" element={<AnimatedPage><DocsPage /></AnimatedPage>} />
                  <Route path="/chat" element={<AnimatedPage><ChatPage /></AnimatedPage>} />
                  <Route path="/memory" element={<AnimatedPage><MemoryPage /></AnimatedPage>} />
                  <Route path="/agent" element={<AnimatedPage><AgentPage /></AnimatedPage>} />
                  <Route path="/workflows" element={<AnimatedPage><WorkflowsPage /></AnimatedPage>} />
                  <Route path="/vision" element={<AnimatedPage><VisionPage /></AnimatedPage>} />
                  <Route path="/brain" element={<AnimatedPage><BrainPage /></AnimatedPage>} />
                  <Route path="/apis" element={<AnimatedPage><PublicApisPage /></AnimatedPage>} />
                  <Route path="/widgets" element={<AnimatedPage><WidgetsPage /></AnimatedPage>} />
                  <Route path="/evolution" element={<AnimatedPage><EvolutionPage /></AnimatedPage>} />
                  <Route path="/voice-skills" element={<AnimatedPage><VoiceSkillsPage /></AnimatedPage>} />
                  <Route path="/unified-knowledge" element={<AnimatedPage><UnifiedKnowledgePage /></AnimatedPage>} />
                </Routes>
                </AppErrorBoundary>
              </AnimatePresence>
            </div>
          </main>
        </div>
      </div>

      {/* Quick Overlay */}
      <QuickOverlay
        isVisible={quickOverlay.visible}
        onClose={handleQuickOverlayClose}
        position={quickOverlay.position}
        recentCommands={recentCommands}
      />

      {/* Approval Modal — triggered by dangerous voice commands */}
      <ApprovalModal />

      {/* Transient Diagnostics — auto-dismissing system stats overlay */}
      <TransientDiagnostics />

      {/* Update Toast — auto-update download progress / restart prompt */}
      <UpdateToast />

      {/* Live Captions — real-time STT + AI response subtitles */}
      <LiveCaptionsWrapper />
    </>
  )
}

// ─── Root App ──────────────────────────────────────────────────────────────

import { Agentation } from 'agentation'

function App(): JSX.Element {
  return (
    <ThemeProvider>
      <VoiceProvider>
      <MemoryRouter
        future={{
          v7_startTransition: true,
          v7_relativeSplatPath: true,
        }}
      >
        <AppContent />
        <Agentation />
      </MemoryRouter>
      </VoiceProvider>
    </ThemeProvider>
  )
}

export default App
