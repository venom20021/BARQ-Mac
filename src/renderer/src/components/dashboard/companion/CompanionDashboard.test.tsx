/**
 * CompanionDashboard tests.
 *
 * The companion view is a projection of BARQ's voice state onto a particle
 * scene, so what matters here is the projection: does each aiState produce the
 * right HUD status, and does the mic control reflect the backend detector
 * rather than a local guess? The scene itself needs WebGL (absent under
 * happy-dom), which also exercises the no-WebGL fallback path.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'

// ─── Mock BARQ's voice context (the source of truth we project from) ───

type AiState = 'idle' | 'listening' | 'thinking' | 'responding'

const voiceState: {
  aiState: AiState
  sttText: string
  responseText: string
  detectorRunning: boolean
  voiceListening: boolean
  wsConnected: boolean
  isRemote: boolean
  toggleDetector: ReturnType<typeof vi.fn>
} = {
  aiState: 'idle',
  sttText: '',
  responseText: '',
  detectorRunning: false,
  voiceListening: false,
  wsConnected: true,
  isRemote: false,
  toggleDetector: vi.fn(),
}

vi.mock('../../../contexts/VoiceContext', () => ({
  useVoice: () => voiceState,
}))

// Import AFTER the mock so the component picks it up.
import { CompanionDashboard } from './CompanionDashboard'

const renderCompanion = () => render(<CompanionDashboard />)

beforeEach(() => {
  voiceState.aiState = 'idle'
  voiceState.sttText = ''
  voiceState.responseText = ''
  voiceState.detectorRunning = false
  voiceState.voiceListening = false
  voiceState.wsConnected = true
  voiceState.isRemote = false
  voiceState.toggleDetector = vi.fn()
  window.localStorage.clear()
  // The scene needs WebGL; silence the expected console.error from the fallback.
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('CompanionDashboard status projection', () => {
  it.each([
    ['idle', 'STANDBY'],
    ['listening', 'LISTENING'],
    ['thinking', 'THINKING'],
    ['responding', 'SPEAKING'],
  ] as const)('maps aiState %s to %s', (aiState, label) => {
    voiceState.aiState = aiState
    renderCompanion()
    expect(screen.getByText(new RegExp(`STATUS:\\s*${label}`))).toBeInTheDocument()
  })

  it('reports the backend link and backend location', () => {
    voiceState.wsConnected = false
    voiceState.isRemote = true
    renderCompanion()
    expect(screen.getByText(/link down/i)).toBeInTheDocument()
    expect(screen.getByText(/lan backend/i)).toBeInTheDocument()
  })
})

describe('CompanionDashboard microphone control', () => {
  it('reflects the backend detector state, not local state', () => {
    voiceState.detectorRunning = false
    const { unmount } = renderCompanion()
    expect(screen.getByRole('button', { name: /start listening/i })).toBeInTheDocument()
    expect(screen.getByText(/microphone idle/i)).toBeInTheDocument()
    unmount()

    voiceState.detectorRunning = true
    voiceState.voiceListening = false
    renderCompanion()
    // Armed but not in a conversation — the two states must stay distinct.
    expect(screen.getByRole('button', { name: /stop listening/i })).toBeInTheDocument()
    expect(screen.getByText(/armed — waiting for wake word/i)).toBeInTheDocument()
  })

  it('calls the backend toggle when pressed', async () => {
    renderCompanion()
    screen.getByRole('button', { name: /start listening/i }).click()
    expect(voiceState.toggleDetector).toHaveBeenCalledTimes(1)
  })
})

describe('CompanionDashboard conversation surface', () => {
  it('shows the live transcript text in the caption', () => {
    voiceState.aiState = 'listening'
    voiceState.sttText = 'what is on my calendar'
    renderCompanion()
    expect(screen.getByText('what is on my calendar')).toBeInTheDocument()
  })

  it('prefers BARQ\'s reply once it is speaking', () => {
    voiceState.aiState = 'responding'
    voiceState.sttText = 'what is on my calendar'
    voiceState.responseText = 'Three things before noon.'
    renderCompanion()
    expect(screen.getByText('Three things before noon.')).toBeInTheDocument()
  })

  it('does not invent a caption when nothing has been said', () => {
    renderCompanion()
    expect(screen.queryByText(/processing/i)).not.toBeInTheDocument()
  })
})

describe('CompanionDashboard degradation', () => {
  it('renders the HUD even when the WebGL scene cannot start', () => {
    // happy-dom has no WebGL context, so createParticleScene throws here.
    renderCompanion()
    expect(screen.getByText('BARQ')).toBeInTheDocument()
    expect(screen.getByText(/webgl unavailable/i)).toBeInTheDocument()
  })

  it('renders the canvas element for the scene to attach to', () => {
    const { container } = renderCompanion()
    expect(container.querySelector('canvas')).toBeInTheDocument()
  })
})
