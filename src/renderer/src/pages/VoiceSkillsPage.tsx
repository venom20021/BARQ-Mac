import { useState } from 'react'
import {
  Mic, Navigation, Briefcase, Video, FolderOpen, Globe, Brain,
  MessageSquare, BookOpen, Settings, Cpu, Eye, Activity, Zap,
  BarChart3, FileText, Monitor, Search, Smartphone, Palette,
  ChevronDown, ChevronRight, Volume2, Workflow, GitBranch,
} from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'

// ─── Types ──────────────────────────────────────────────────────────────────

interface VoiceSkill {
  name: string
  command: string
  description: string
  example?: string
}

interface SkillCategory {
  id: string
  label: string
  icon: typeof Mic
  color: string
  bgColor: string
  skills: VoiceSkill[]
}

// ─── Skill Data ─────────────────────────────────────────────────────────────

const CATEGORIES: SkillCategory[] = [
  {
    id: 'navigation',
    label: 'Page Navigation',
    icon: Navigation,
    color: 'text-cyan-300',
    bgColor: 'bg-cyan-500/8 border-cyan-500/20',
    skills: [
      { name: 'Dashboard', command: 'open dashboard', description: 'Go to the main command center', example: '"Go to dashboard"' },
      { name: 'Analytics', command: 'open analytics', description: 'View insights and analytics', example: '"Show analytics"' },
      { name: 'Jobs', command: 'open jobs', description: 'Access the job scanner and applications', example: '"Open jobs"' },
      { name: 'Social', command: 'open social', description: 'View social media content and trends', example: '"Switch to social"' },
      { name: 'Files', command: 'open files', description: 'Browse workspace files', example: '"Go to files"' },
      { name: 'Dev', command: 'open dev', description: 'Access developer tools and terminal', example: '"Take me to dev"' },
      { name: 'System', command: 'open system', description: 'View system status and hardware', example: '"Show system"' },
      { name: 'Web', command: 'open web', description: 'Browse the web and check weather/stocks', example: '"Open web"' },
      { name: 'Phone', command: 'open phone', description: 'Access mobile integration', example: '"Go to phone"' },
      { name: 'Research', command: 'open research', description: 'Start deep research tasks', example: '"Switch to research"' },
      { name: 'Docs', command: 'open docs', description: 'Search documentation', example: '"Open docs"' },
      { name: 'Chat', command: 'open chat', description: 'Chat with BARQ AI', example: '"Go to chat"' },
      { name: 'Memory', command: 'open memory', description: 'Access notes and storage', example: '"Show memory"' },
      { name: 'Widgets', command: 'open widgets', description: 'View available widgets', example: '"Open widgets"' },
      { name: 'Settings', command: 'open settings', description: 'Configure BARQ preferences', example: '"Go to settings"' },
      { name: 'Agent', command: 'open agent', description: 'Access the AI agent system', example: '"Switch to agent"' },
      { name: 'Workflows', command: 'open workflows', description: 'Manage automated workflows', example: '"Open workflows"' },
      { name: 'Vision', command: 'open vision', description: 'Screen and camera analysis', example: '"Take me to vision"' },
      { name: 'Brain', command: 'open brain', description: 'View the knowledge graph', example: '"Show brain"' },
      { name: 'APIs', command: 'open apis', description: 'Browse public APIs and integrations', example: '"Open apis"' },
      { name: 'Evolution', command: 'open evolution', description: 'View AI learning progress', example: '"Switch to evolution"' },
      { name: 'Notes', command: 'open notes', description: 'Access the notes editor', example: '"Go to notes"' },
      { name: 'Gallery', command: 'open gallery', description: 'View images and media', example: '"Open gallery"' },
    ],
  },
  {
    id: 'jobs',
    label: 'Job Search & Applications',
    icon: Briefcase,
    color: 'text-emerald-300',
    bgColor: 'bg-emerald-500/8 border-emerald-500/20',
    skills: [
      { name: 'Scan Jobs', command: 'scan jobs', description: 'Trigger a scan of all configured job boards', example: '"Scan jobs"' },
      { name: 'Search Jobs', command: 'search for python jobs in London', description: 'Search for jobs with keywords and location', example: '"Search for remote React developer jobs"' },
      { name: 'Job Matches', command: 'show job matches', description: 'View your current job matches and scores', example: '"Show my job matches"' },
      { name: 'Job Details', command: 'show job details for job 5', description: 'Get full details for a specific job listing', example: '"What are the details for job 5?"' },
      { name: 'Apply Preview', command: 'preview application for job 3', description: 'Fill application form without submitting', example: '"Preview my application for job 3"' },
      { name: 'Application Status', command: 'show application status', description: 'View status of all job applications', example: '"Show my application status"' },
    ],
  },
  {
    id: 'social',
    label: 'Social Media & Content',
    icon: Video,
    color: 'text-pink-300',
    bgColor: 'bg-pink-500/8 border-pink-500/20',
    skills: [
      { name: 'Trending Topics', command: 'show trends', description: 'View current social media trending topics', example: '"What\'s trending on social media?"' },
      { name: 'Create Post', command: 'post about AI trends', description: 'Create and post social media content', example: '"Create a post about machine learning"' },
      { name: 'Schedule Post', command: 'schedule post for Friday', description: 'Schedule content for later', example: '"Schedule this post for tomorrow at 10am"' },
    ],
  },
  {
    id: 'memory',
    label: 'Memory & Notes',
    icon: BookOpen,
    color: 'text-amber-300',
    bgColor: 'bg-amber-500/8 border-amber-500/20',
    skills: [
      { name: 'Remember', command: 'remember that my birthday is March 15', description: 'Store a fact in long-term memory', example: '"Remember that my project deadline is next Friday"' },
      { name: 'Recall', command: 'recall my birthday', description: 'Search long-term memory for stored facts', example: '"What do you remember about my projects?"' },
      { name: 'List Memories', command: 'show my memories', description: 'View all stored memories', example: '"List all my memories"' },
      { name: 'Create Note', command: 'create a note about project ideas', description: 'Create a new note with title and content', example: '"Create a note titled Meeting Notes with content discussed roadmap"' },
      { name: 'List Notes', command: 'show my notes', description: 'View all saved notes', example: '"List my notes"' },
    ],
  },
  {
    id: 'brain',
    label: 'Knowledge & Brain',
    icon: Brain,
    color: 'text-purple-300',
    bgColor: 'bg-purple-500/8 border-purple-500/20',
    skills: [
      { name: 'Brain Summary', command: 'show knowledge graph', description: 'Get a summary of the knowledge brain', example: '"Show me the knowledge graph"' },
    ],
  },
  {
    id: 'web',
    label: 'Web & Research',
    icon: Globe,
    color: 'text-blue-300',
    bgColor: 'bg-blue-500/8 border-blue-500/20',
    skills: [
      { name: 'Web Search', command: 'search the web for AI news', description: 'Search the web via BARQ', example: '"Search for latest React tutorials"' },
      { name: 'Deep Research', command: 'research quantum computing', description: 'Start a deep research task', example: '"Research the impact of AI on healthcare"' },
      { name: 'Weather', command: 'weather in London', description: 'Check the weather in a city', example: '"What\'s the weather in New York?"' },
      { name: 'Stocks', command: 'show stocks', description: 'Check stock prices', example: '"Show me AAPL stock price"' },
    ],
  },
  {
    id: 'docs',
    label: 'Documentation',
    icon: FileText,
    color: 'text-sky-300',
    bgColor: 'bg-sky-500/8 border-sky-500/20',
    skills: [
      { name: 'Search Docs', command: 'search docs for API usage', description: 'Search BARQ documentation', example: '"Find docs about voice configuration"' },
    ],
  },
  {
    id: 'chat',
    label: 'Chat & Conversation',
    icon: MessageSquare,
    color: 'text-teal-300',
    bgColor: 'bg-teal-500/8 border-teal-500/20',
    skills: [
      { name: 'Send Message', command: 'what is the meaning of life', description: 'Send a message to BARQ chat AI', example: '"Explain how neural networks work"' },
    ],
  },
  {
    id: 'notifications',
    label: 'Notifications',
    icon: Zap,
    color: 'text-yellow-300',
    bgColor: 'bg-yellow-500/8 border-yellow-500/20',
    skills: [
      { name: 'Send Notification', command: 'send notification: meeting at 3pm', description: 'Send a notification via telegram, email, or desktop', example: '"Notify me when the scan is done"' },
    ],
  },
  {
    id: 'agent',
    label: 'Agent & Automation',
    icon: Cpu,
    color: 'text-orange-300',
    bgColor: 'bg-orange-500/8 border-orange-500/20',
    skills: [
      { name: 'Run Task', command: 'run agent to check my inbox', description: 'Execute an agent task autonomously', example: '"Agent, research and summarize today\'s AI news"' },
      { name: 'Run Workflow', command: 'run morning briefing', description: 'Execute a registered workflow', example: '"Run the weekly review workflow"' },
      { name: 'Morning Briefing', command: 'generate briefing', description: 'Generate the morning briefing report', example: '"Give me my morning briefing"' },
      { name: 'Weekly Review', command: 'generate weekly review', description: 'Generate the weekly review report', example: '"Generate my weekly review"' },
      { name: 'Workflow Status', command: 'show workflow status', description: 'Check status of a running workflow', example: '"What\'s the status of my briefing?"' },
    ],
  },
  {
    id: 'vision',
    label: 'Vision & Analysis',
    icon: Eye,
    color: 'text-rose-300',
    bgColor: 'bg-rose-500/8 border-rose-500/20',
    skills: [
      { name: 'Analyze Screen', command: 'what is on my screen', description: 'Analyze the current screen content', example: '"What do you see on my screen?"' },
      { name: 'Analyze Camera', command: 'look at the camera', description: 'Capture and analyze webcam', example: '"What can you see through the camera?"' },
    ],
  },
  {
    id: 'system',
    label: 'System & Hardware',
    icon: Monitor,
    color: 'text-slate-300',
    bgColor: 'bg-slate-500/8 border-slate-500/20',
    skills: [
      { name: 'System Status', command: 'show system status', description: 'View CPU, RAM, disk, and GPU status', example: '"How is my system performing?"' },
    ],
  },
  {
    id: 'analytics',
    label: 'Analytics',
    icon: BarChart3,
    color: 'text-indigo-300',
    bgColor: 'bg-indigo-500/8 border-indigo-500/20',
    skills: [
      { name: 'Activity Analytics', command: 'show activity analytics', description: 'View recent activity and usage analytics', example: '"Show my activity analytics"' },
      { name: 'Career Analytics', command: 'show career analytics', description: 'View career and job application analytics', example: '"Show my career analytics"' },
      { name: 'Social Analytics', command: 'show social analytics', description: 'View social media performance analytics', example: '"Show my social analytics"' },
    ],
  },
  {
    id: 'settings',
    label: 'Settings',
    icon: Settings,
    color: 'text-zinc-300',
    bgColor: 'bg-zinc-500/8 border-zinc-500/20',
    skills: [
      { name: 'Get Settings', command: 'show voice settings', description: 'View settings for a section', example: '"Show my voice settings"' },
      { name: 'Update Setting', command: 'set wake word sensitivity to high', description: 'Update a specific setting', example: '"Set voice language to Hindi"' },
    ],
  },
  {
    id: 'voice',
    label: 'Voice Control',
    icon: Volume2,
    color: 'text-cyan-200',
    bgColor: 'bg-cyan-500/5 border-cyan-500/10',
    skills: [
      { name: 'Start Listening', command: 'start voice', description: 'Start the voice wake word detector', example: '"Start listening"' },
      { name: 'Overlay', command: 'show overlay', description: 'Show or hide the quick overlay', example: '"Show overlay"' },
      { name: 'Diagnostics', command: 'show diagnostics', description: 'Show system diagnostics overlay', example: '"Show diagnostics"' },
    ],
  },
  {
    id: 'desktop',
    label: 'Desktop Control',
    icon: Workflow,
    color: 'text-lime-300',
    bgColor: 'bg-lime-500/8 border-lime-500/20',
    skills: [
      { name: 'Minimize Window', command: 'minimize window', description: 'Minimize the active window', example: '"Minimize Chrome"' },
      { name: 'Maximize Window', command: 'maximize window', description: 'Maximize the active window', example: '"Maximize this window"' },
      { name: 'Take Screenshot', command: 'take screenshot', description: 'Capture the screen', example: '"Take a screenshot"' },
      { name: 'Open File', command: 'open file /path/to/file', description: 'Open a file or application', example: '"Open my resume.pdf"' },
      { name: 'Launch App', command: 'launch Chrome', description: 'Launch an application', example: '"Open Spotify"' },
      { name: 'Media Control', command: 'play pause', description: 'Control media playback', example: '"Next track"' },
      { name: 'Volume', command: 'set volume to 50', description: 'Set system volume level', example: '"Mute the volume"' },
    ],
  },
  {
    id: 'second_brain',
    label: 'Second Brain',
    icon: BookOpen,
    color: 'text-violet-300',
    bgColor: 'bg-violet-500/8 border-violet-500/20',
    skills: [
      { name: 'Search Second Brain', command: 'search second brain for python', description: 'Search notes, code, bookmarks, and tasks', example: '"Search second brain for Flask"' },
      { name: 'List Items', command: 'show my notes', description: 'List items from Second Brain', example: '"Show my bookmarks"' },
      { name: 'Create Item', command: 'save to second brain', description: 'Create a new note, code snippet, or bookmark', example: '"Save to second brain: meeting notes about API design"' },
      { name: 'Chat with AI', command: 'ask second brain', description: 'Chat with Gemini AI grounded in your knowledge', example: '"Ask second brain what I know about React"' },
      { name: 'Sync Knowledge', command: 'sync knowledge', description: 'Sync BARQ and Second Brain data bidirectionally', example: '"Sync everything"' },
      { name: 'Sync Status', command: 'sync status', description: 'Check sync status and mapping stats', example: '"What is the sync status?"' },
      { name: 'Start Auto-Sync', command: 'start auto sync', description: 'Start automatic sync at configured interval', example: '"Start auto sync second brain"' },
      { name: 'Stop Auto-Sync', command: 'stop auto sync', description: 'Stop the automatic sync scheduler', example: '"Stop auto sync"' },
      { name: 'Connection Status', command: 'second brain status', description: 'Check if Second Brain is connected', example: '"Is second brain connected?"' },
      { name: 'Open Unified Knowledge', command: 'open unified knowledge', description: 'View combined knowledge from both systems', example: '"Show unified knowledge"' },
    ],
  },
]

// ─── Page Aliases Map ───────────────────────────────────────────────────────

const PAGE_ALIASES: Record<string, string[]> = {
  'Dashboard': ['home', 'command'],
  'Analytics': ['insights'],
  'Jobs': ['job', 'careers', 'career', 'recruitment'],
  'Social': ['content', 'social media'],
  'Files': ['file', 'explorer'],
  'Dev': ['developer', 'terminal', 'code'],
  'System': ['hardware', 'monitor'],
  'Web': ['browser', 'weather', 'stocks'],
  'Phone': ['mobile'],
  'Research': ['search', 'deep research'],
  'Docs': ['documents', 'documentation'],
  'Chat': ['conversation', 'talk'],
  'Memory': ['notes & storage', 'storage'],
  'Widgets': ['widget'],
  'Settings': ['preferences', 'config'],
  'Agent': ['ai agent'],
  'Workflows': ['workflow', 'automations'],
  'Vision': ['camera', 'screen analysis'],
  'Brain': ['knowledge', 'knowledge graph', 'graph'],
  'APIs': ['api', 'public apis', 'integrations'],
  'Evolution': ['evolve', 'learning'],
  'Notes': ['note', 'notepad'],
  'Gallery': ['images', 'photos'],
  'Unified Knowledge': ['second brain', 'knowledge base', 'sb'],
  'Second Brain': ['unified knowledge', 'sb', 'knowledge base'],
}

// ─── Component ──────────────────────────────────────────────────────────────

export function VoiceSkillsPage(): JSX.Element {
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set(CATEGORIES.map(c => c.id)))
  const [searchQuery, setSearchQuery] = useState('')

  const toggleCategory = (id: string): void => {
    setExpandedCategories((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const expandAll = (): void => setExpandedCategories(new Set(CATEGORIES.map(c => c.id)))
  const collapseAll = (): void => setExpandedCategories(new Set())

  const q = searchQuery.toLowerCase()
  const filteredCategories = CATEGORIES.map((cat) => ({
    ...cat,
    skills: cat.skills.filter(
      (s) =>
        !q ||
        s.name.toLowerCase().includes(q) ||
        s.command.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q) ||
        cat.label.toLowerCase().includes(q),
    ),
  })).filter((cat) => cat.skills.length > 0)

  const totalSkills = filteredCategories.reduce((sum, c) => sum + c.skills.length, 0)

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-6">
      {/* Header */}
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
        <h1 className="text-xl font-orbitron font-bold text-ghost tracking-wider flex items-center gap-3">
          <Mic className="w-6 h-6 text-cyan-400" />
          VOICE SKILLS
        </h1>
        <p className="text-sm font-rajdhani text-dim-400 mt-1">
          Complete reference for every voice command and navigation skill in BARQ
        </p>
      </motion.div>

      {/* Search + Controls */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.05 }}
        className="flex items-center gap-3"
      >
        <div className="flex-1 relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-dim-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search skills, commands, or categories..."
            className="input-cyan text-sm pl-9 w-full"
          />
        </div>
        <button onClick={expandAll} className="btn-ghost-cyan text-xs whitespace-nowrap">
          Expand All
        </button>
        <button onClick={collapseAll} className="btn-ghost-cyan text-xs whitespace-nowrap">
          Collapse All
        </button>
        <span className="text-xs font-mono text-dim-500 whitespace-nowrap">
          {totalSkills} skills
        </span>
      </motion.div>

      {/* Quick Nav Reference */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
        className="glass-card"
      >
        <div className="flex items-center gap-2 mb-3">
          <Navigation className="w-5 h-5 text-cyan-300" />
          <h3 className="text-sm font-orbitron font-bold text-ghost tracking-wider">
            QUICK NAVIGATION REFERENCE
          </h3>
        </div>
        <p className="text-xs font-exo text-dim-400 mb-3">
          Say any page name directly or use a prefix: <span className="text-cyan-300">"open"</span>,{' '}
          <span className="text-cyan-300">"go to"</span>, <span className="text-cyan-300">"show"</span>,{' '}
          <span className="text-cyan-300">"switch to"</span>, <span className="text-cyan-300">"take me to"</span>
        </p>
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-1.5">
          {Object.entries(PAGE_ALIASES).map(([page, aliases]) => (
            <div key={page} className="bg-void-700/30 rounded-lg px-2 py-1.5 border border-cyan-500/5">
              <span className="text-xs font-rajdhani font-semibold text-ghost">{page}</span>
              <span className="text-[10px] font-exo text-dim-500 block">{aliases.join(', ')}</span>
            </div>
          ))}
        </div>
      </motion.div>

      {/* Skill Categories */}
      <div className="space-y-3">
        {filteredCategories.map((cat, catIdx) => {
          const Icon = cat.icon
          const isExpanded = expandedCategories.has(cat.id)
          return (
            <motion.div
              key={cat.id}
              initial={{ opacity: 0, y: 15 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.05 * catIdx }}
              className="glass-card overflow-hidden"
            >
              {/* Category Header */}
              <button
                onClick={() => toggleCategory(cat.id)}
                className="w-full flex items-center gap-3 px-4 py-3 hover:bg-white/[0.02] transition-colors"
              >
                <Icon className={`w-5 h-5 ${cat.color} flex-shrink-0`} />
                <span className="text-sm font-orbitron font-bold text-ghost tracking-wider flex-1 text-left">
                  {cat.label.toUpperCase()}
                </span>
                <span className="text-xs font-mono text-dim-500 mr-2">
                  {cat.skills.length} skill{cat.skills.length !== 1 ? 's' : ''}
                </span>
                {isExpanded ? (
                  <ChevronDown className="w-4 h-4 text-dim-500" />
                ) : (
                  <ChevronRight className="w-4 h-4 text-dim-500" />
                )}
              </button>

              {/* Skills List */}
              <AnimatePresence>
                {isExpanded && (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.2, ease: 'easeOut' }}
                    className="overflow-hidden"
                  >
                    <div className="px-4 pb-3 grid grid-cols-1 md:grid-cols-2 gap-2">
                      {cat.skills.map((skill) => (
                        <div
                          key={skill.name}
                          className={`rounded-lg p-3 border ${cat.bgColor} hover:scale-[1.01] transition-transform`}
                        >
                          <div className="flex items-start justify-between mb-1">
                            <span className="text-sm font-rajdhani font-semibold text-ghost">
                              {skill.name}
                            </span>
                          </div>
                          <p className="text-xs font-exo text-dim-300 mb-2">{skill.description}</p>
                          <div className="bg-void-900/40 rounded px-2 py-1.5 font-mono text-[11px] text-cyan-300/80">
                            "{skill.command}"
                          </div>
                          {skill.example && (
                            <p className="text-[10px] font-exo text-dim-500 mt-1.5 italic">
                              Example: {skill.example}
                            </p>
                          )}
                        </div>
                      ))}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </motion.div>
          )
        })}
      </div>

      {/* Footer Tips */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.3 }}
        className="glass-card"
      >
        <div className="flex items-center gap-2 mb-3">
          <Mic className="w-5 h-5 text-holographic" />
          <h3 className="text-sm font-orbitron font-bold text-ghost tracking-wider">VOICE TIPS</h3>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div className="bg-void-700/30 rounded-lg p-3 border border-cyan-500/5">
            <p className="text-xs font-rajdhani font-semibold text-ghost mb-1">Wake Word</p>
            <p className="text-xs font-exo text-dim-400">
              Say <span className="text-cyan-300">"Computer"</span> or your configured wake word to activate BARQ.
              The detector runs in the background and responds to your voice.
            </p>
          </div>
          <div className="bg-void-700/30 rounded-lg p-3 border border-cyan-500/5">
            <p className="text-xs font-rajdhani font-semibold text-ghost mb-1">Hands-Free Mode</p>
            <p className="text-xs font-exo text-dim-400">
              Enable hands-free mode in Settings to have multi-turn conversations
              without repeating the wake word. BARQ listens continuously between turns.
            </p>
          </div>
          <div className="bg-void-700/30 rounded-lg p-3 border border-cyan-500/5">
            <p className="text-xs font-rajdhani font-semibold text-ghost mb-1">Natural Language</p>
            <p className="text-xs font-exo text-dim-400">
              You don't need exact commands. BARQ understands natural language like
              <span className="text-cyan-300"> "what's trending?"</span> or{' '}
              <span className="text-cyan-300"> "help me find a job"</span>.
            </p>
          </div>
          <div className="bg-void-700/30 rounded-lg p-3 border border-cyan-500/5">
            <p className="text-xs font-rajdhani font-semibold text-ghost mb-1">Background Context</p>
            <p className="text-xs font-exo text-dim-400">
              On wake, BARQ automatically gathers system status, job scan results,
              weather, stocks, and news to provide contextual responses.
            </p>
          </div>
        </div>
      </motion.div>
    </div>
  )
}
