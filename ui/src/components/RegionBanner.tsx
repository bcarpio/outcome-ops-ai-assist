import { AlertTriangle } from 'lucide-react'
import { useRegionRole } from '../lib/regionRole'

/**
 * Sticky top banner that appears only when this UI is served by the
 * passive region of a multi-region (HA) deployment. Tells the operator
 * that mutating actions are disabled and reads still work.
 *
 * The banner is the operator-facing half of a belt-and-suspenders
 * contract: the workspace-management Lambda also returns HTTP 403 on
 * any mutating route via shared.region_guard, so the banner cannot be
 * the only thing standing between a passive region and a bad write —
 * but it is the thing that prevents the operator from being surprised.
 */
export default function RegionBanner() {
  const { role, region, primaryRegion } = useRegionRole()
  if (role !== 'passive') return null

  return (
    <div
      role="status"
      aria-live="polite"
      className="bg-amber-100 border-b-2 border-amber-400 text-amber-900 px-4 py-2 flex items-center gap-2 text-sm"
    >
      <AlertTriangle size={16} className="flex-shrink-0" />
      <span>
        <strong>Read-only mode.</strong> This UI is served by the passive region
        {region ? ` (${region})` : ''}. Admin actions (creating workspaces,
        adding repositories, connecting integrations, syncing, removing data)
        are disabled. Chat, search, audit viewing, and analytics continue to
        work.
        {primaryRegion ? (
          <>
            {' '}
            Active region: <code className="font-mono">{primaryRegion}</code>.
          </>
        ) : null}
      </span>
    </div>
  )
}
