/**
 * Backend configuration utility for the renderer.
 *
 * Provides HTTP and WebSocket base URLs by fetching config from
 * the main process via IPC.  The config tells the renderer whether
 * the app is in local mode (Python sidecar on localhost) or remote
 * mode (cloud BARQ instance).
 *
 * IMPORTANT: Config is NOT cached — every call fetches fresh from the
 * main process.  This is safe because the main process's
 * ``getBackendConfig()`` is a synchronous property read (no IPC overhead).
 * Not caching ensures that when the auto-remote probe completes and
 * sets ``remoteMode = true`` in the main process, the renderer picks
 * up the change immediately rather than serving a stale localhost config.
 *
 * Usage:
 *   import { getBackendConfig } from '../utils/backendConfig'
 *   const config = await getBackendConfig()
 *   // config.httpUrl  → "http://sai-prabhat-HP-All-in-One-22-dd0xxx.local:8956" or "http://127.0.0.1:8956"
 *   // config.wsUrl    → "ws://sai-prabhat-HP-All-in-One-22-dd0xxx.local:8956" or "ws://127.0.0.1:8956"
 *   // config.isRemote → true | false
 */

export interface BackendConfig {
  httpUrl: string
  wsUrl: string
  isRemote: boolean
}

// No caching — always fetch fresh from main process.
// The IPC call reads a synchronous property so it's effectively instant.

/**
 * Default fallback config (local mode) — used only when IPC fails.
 */
const DEFAULT_CONFIG: BackendConfig = {
  httpUrl: 'http://127.0.0.1:8956',
  wsUrl: 'ws://127.0.0.1:8956',
  isRemote: false,
}

/**
 * Get the backend configuration (HTTP URL, WS URL, remote mode status).
 * Always fetches fresh from the main process — no stale cache.
 */
export async function getBackendConfig(): Promise<BackendConfig> {
  try {
    const resp = await window.barq?.python.getConfig()
    if (resp?.success && resp.data) {
      return {
        httpUrl: resp.data.httpUrl,
        wsUrl: resp.data.wsUrl,
        isRemote: resp.data.isRemote,
      }
    }
  } catch {
    // Fall through to defaults
  }

  return DEFAULT_CONFIG
}

/**
 * Synchronous HTTP URL getter — returns the default since we don't cache.
 * For a sync read, call ``getBackendConfig()`` early and store the result.
 */
export function getSyncHttpUrl(): string {
  return DEFAULT_CONFIG.httpUrl
}

/**
 * Synchronous WS URL getter — returns the default since we don't cache.
 * For remote mode, use ``const config = await getBackendConfig()``.
 */
export function getSyncWsUrl(): string {
  return DEFAULT_CONFIG.wsUrl
}

/**
 * Switch to remote mode at runtime (no restart needed).
 */
export async function setRemoteMode(enabled: boolean, url?: string): Promise<BackendConfig> {
  try {
    const resp = await window.barq?.python.setRemoteMode(enabled, url)
    if (resp?.success && resp.data) {
      return {
        httpUrl: resp.data.httpUrl,
        wsUrl: resp.data.wsUrl,
        isRemote: resp.data.isRemote,
      }
    }
  } catch {
    // ignore
  }
  return getBackendConfig()
}
