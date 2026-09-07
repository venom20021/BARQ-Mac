import { Settings, Mic, Volume2, Key, Cpu, Bell, Briefcase, Video, Shield, Terminal, User, Cloud, Palette, Sunrise, Database } from 'lucide-react'

// ─── Types ───────────────────────────────────────────────────────────────

export interface SettingsSection {
  id: string
  label: string
  icon: typeof Settings
  description: string
}

export interface VoiceStatus {
  is_listening: boolean
  wake_word: string
  stt_model: string
  tts_model: string
  recent_commands: { transcript: string; created_at: string }[]
  wake_greeting_enabled?: boolean
  weather_city?: string
}

// ─── Constants ───────────────────────────────────────────────────────────

export const sections: SettingsSection[] = [
  { id: 'voice', label: 'Voice', icon: Mic, description: 'Wake word, language, speech settings' },
  { id: 'sounds', label: 'Sounds', icon: Volume2, description: 'Preview and toggle audio profiles' },
  { id: 'api', label: 'API Keys', icon: Key, description: 'Connect your accounts and services' },
  { id: 'cloud-llm', label: 'Cloud LLM', icon: Cpu, description: 'Ollama fallback and cloud AI settings' },
  { id: 'briefing', label: 'Morning Briefing', icon: Sunrise, description: 'Daily AI briefing schedule and delivery' },
  { id: 'notifications', label: 'Notifications', icon: Bell, description: 'Alerts and digest preferences' },
  { id: 'jobs', label: 'Job Search', icon: Briefcase, description: 'Job search preferences and filters' },
  { id: 'social', label: 'Social', icon: Video, description: 'Content creation and posting settings' },
  { id: 'security', label: 'Security', icon: Shield, description: 'Command whitelist and approvals' },
  { id: 'debug', label: 'Debug', icon: Terminal, description: 'Debug logging and diagnostics' },
  { id: 'profile', label: 'Profile', icon: User, description: 'Your name and personal details' },
  { id: 'connection', label: 'Connection', icon: Cloud, description: 'Local or cloud backend mode' },
  { id: 'second-brain', label: 'Second Brain', icon: Database, description: 'Knowledge integration and sync settings' },
  { id: 'ollama', label: 'Ollama LLM', icon: Cpu, description: 'Local LLM host, model, and connection settings' },
  { id: 'appearance', label: 'Appearance', icon: Palette, description: 'Theme and display settings' },
]

export const TTS_VOICES = [
  { value: 'aura-2-odysseus-en', label: 'Odysseus (Male — Deepgram)' },
  { value: 'aura-2-hera-en', label: 'Hera (Female — Deepgram)' },
  { value: 'aura-2-athena-en', label: 'Athena (Female — Deepgram)' },
  { value: 'aura-2-persephone-en', label: 'Persephone (Female — Deepgram)' },
  { value: 'aura-2-ares-en', label: 'Ares (Male — Deepgram)' },
  { value: 'aura-2-orion-en', label: 'Orion (Male — Deepgram)' },
  { value: 'aura-2-helios-en', label: 'Helios (Male — Deepgram)' },
  { value: 'aura-2-arcas-en', label: 'Arcas (Male — Deepgram)' },
  { value: 'aura-2-stella-en', label: 'Stella (Female — Deepgram)' },
  { value: 'aura-2-luna-en', label: 'Luna (Female — Deepgram)' },
  { value: 'aura-2-nova-en', label: 'Nova (Female — Deepgram)' },
  { value: 'aura-2-iris-en', label: 'Iris (Female — Deepgram)' },
  { value: 'aura-2-asteria-en', label: 'Asteria (Female — Deepgram)' },
  { value: 'aura-2-selene-en', label: 'Selene (Female — Deepgram)' },
  { value: 'aura-2-aphrodite-en', label: 'Aphrodite (Female — Deepgram)' },
  { value: 'aura-2-hades-en', label: 'Hades (Male — Deepgram)' },
  { value: 'aura-2-poseidon-en', label: 'Poseidon (Male — Deepgram)' },
  { value: 'aura-2-zeus-en', label: 'Zeus (Male — Deepgram)' },
  { value: 'aura-2-demetra-en', label: 'Demetra (Female — Deepgram)' },
]

export const SENSITIVITY_LEVELS = ['low', 'medium', 'high'] as const
