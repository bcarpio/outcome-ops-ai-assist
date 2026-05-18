/**
 * Region-role client. Queries /api/region/role once at app load to learn
 * whether this UI was served by the active or the passive region.
 *
 * Backend contract (workspace-management lambda GET /api/region/role):
 *   {
 *     "role": "active" | "passive",
 *     "region": "us-west-2",          // the AWS region serving this UI
 *     "primary_region": "us-west-2",
 *     "secondary_region": "us-east-2" // empty string when HA not deployed
 *   }
 *
 * Falls back to role="active" with empty region on fetch failure so the
 * UI never gets bricked by a network glitch. The backend region_guard
 * still returns 403 on mutations regardless of what the UI thinks.
 */

import { useEffect, useState } from 'react'

export interface RegionRole {
  role: 'active' | 'passive'
  region: string
  primaryRegion: string
  secondaryRegion: string
  loading: boolean
  /** True when fetch failed; UI should not gate buttons in this state. */
  unknown: boolean
}

const DEFAULT: RegionRole = {
  role: 'active',
  region: '',
  primaryRegion: '',
  secondaryRegion: '',
  loading: true,
  unknown: false,
}

let cached: RegionRole | null = null
let inflight: Promise<RegionRole> | null = null

async function fetchRegionRole(): Promise<RegionRole> {
  if (cached) return cached
  if (inflight) return inflight
  inflight = (async () => {
    try {
      const response = await fetch('/api/region/role', { credentials: 'same-origin' })
      if (!response.ok) {
        return { ...DEFAULT, loading: false, unknown: true }
      }
      const data = (await response.json()) as Record<string, unknown>
      const role = data.role === 'passive' ? 'passive' : 'active'
      cached = {
        role,
        region: typeof data.region === 'string' ? data.region : '',
        primaryRegion: typeof data.primary_region === 'string' ? data.primary_region : '',
        secondaryRegion: typeof data.secondary_region === 'string' ? data.secondary_region : '',
        loading: false,
        unknown: false,
      }
      return cached
    } catch {
      return { ...DEFAULT, loading: false, unknown: true }
    } finally {
      inflight = null
    }
  })()
  return inflight
}

/**
 * Read the cached region role. Components mount, fire the fetch once,
 * and re-render when it resolves.
 *
 * IMPORTANT: do NOT key any "is this safe to do?" UI decision on
 * `loading` or `unknown` — only gate on `role === 'passive'`. The
 * backend enforces the contract; the UI just hides buttons to avoid
 * confusing the operator.
 */
export function useRegionRole(): RegionRole {
  const [state, setState] = useState<RegionRole>(cached ?? DEFAULT)
  useEffect(() => {
    let cancelled = false
    fetchRegionRole().then(r => {
      if (!cancelled) setState(r)
    })
    return () => {
      cancelled = true
    }
  }, [])
  return state
}

/** Helper for non-React code paths (e.g. one-shot checks in event handlers). */
export async function getRegionRole(): Promise<RegionRole> {
  return fetchRegionRole()
}

/** Test-only: clear the module-level cache. */
export function _resetRegionRoleCacheForTests(): void {
  cached = null
  inflight = null
}
