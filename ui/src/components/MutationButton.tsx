// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { useRegionRole } from '../lib/regionRole'

/**
 * Button that auto-disables itself when the UI is in a passive HA region.
 *
 * Use this for any control that performs a mutating action: creating a
 * workspace, adding/removing a repo, connecting an OAuth integration,
 * triggering a sync, removing members, deleting documents.
 *
 * Visual behavior in passive mode:
 *   - disabled + greyed-out
 *   - title attribute swapped to a "read-only" explainer for hover help
 *
 * The backend (workspace-management lambda + each *-integration lambda)
 * also enforces the contract via shared.region_guard returning HTTP 403,
 * so this component is the UX layer of a belt-and-suspenders defense.
 */
type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  children: ReactNode
  /** Custom passive-mode tooltip; defaults to a sensible generic one. */
  passiveTitle?: string
}

export default function MutationButton({
  children,
  disabled,
  title,
  passiveTitle,
  ...rest
}: Props) {
  const { role } = useRegionRole()
  const isPassive = role === 'passive'
  return (
    <button
      {...rest}
      disabled={disabled || isPassive}
      title={
        isPassive
          ? passiveTitle ||
            'Disabled: this UI is served by the passive region. Failover to the active region to perform admin actions.'
          : title
      }
    >
      {children}
    </button>
  )
}
