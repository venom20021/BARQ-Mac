// ─── BARQ Particle Companion ──────────────────────────────────────────
// Barrel export for the particle companion dashboard (replaces the humanoid
// dashboard). The scene is ported from the Lovable "particle AI companion"
// reference; behaviour is wired to BARQ's own voice stack via useVoice().

export { CompanionDashboard, default } from './CompanionDashboard'
export {
  createParticleScene,
  THEMES,
  type ParticleScene,
  type SceneLevels,
  type SceneOptions,
  type Theme,
  type ThemeKind,
  type VisualKind,
} from './particleScene'
export { MicAnalyser, getAudioContext, playTone, sfx } from './audio'
