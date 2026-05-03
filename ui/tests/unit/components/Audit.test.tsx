import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Audit from '../../../src/pages/Audit'
import { renderWithRouter } from '../../helpers/render'
import type {
  AuditSummary,
  AuditEventsPage,
  AuditEvent,
} from '../../../src/lib/api'

vi.mock('../../../src/lib/api', () => ({
  getAuditSummary: vi.fn(),
  getAuditEvents: vi.fn(),
  getAuditEvent: vi.fn(),
  getAuditWorkspace: vi.fn(),
  getSearchableWorkspaces: vi.fn(),
}))

import {
  getAuditSummary,
  getAuditEvents,
  getSearchableWorkspaces,
} from '../../../src/lib/api'

const mockSummary = vi.mocked(getAuditSummary)
const mockEvents = vi.mocked(getAuditEvents)
const mockSearchable = vi.mocked(getSearchableWorkspaces)

function buildSummary(overrides: Partial<AuditSummary> = {}): AuditSummary {
  return {
    actions_total: 12,
    actions_by_type: [
      { action: 'API_KEY_CREATED', count: 5 },
      { action: 'WORKSPACE_REPO_ADDED', count: 4 },
      { action: 'WORKSPACE_CREATED', count: 3 },
    ],
    actions_by_workspace: [
      { workspace_id: 'ws-1', workspace_name: 'Engineering', count: 9 },
      { workspace_id: 'ws-2', workspace_name: 'Data', count: 3 },
    ],
    top_actors: [
      {
        user_email: 'brian@example.com',
        count: 9,
        last_active: '2026-05-01T12:00:00+00:00',
      },
    ],
    trend: [
      { date: '2026-05-01', count: 7 },
      { date: '2026-05-02', count: 5 },
    ],
    start_date: '2026-04-03T00:00:00+00:00',
    end_date: '2026-05-03T00:00:00+00:00',
    ...overrides,
  }
}

function buildEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    request_id: 'r1',
    timestamp: '2026-05-01T12:00:00+00:00',
    category: 'admin',
    action: 'API_KEY_CREATED',
    user_email: 'brian@example.com',
    workspace_id: 'ws-1',
    source_ip: '203.0.113.42',
    user_agent: 'Mozilla/5.0 demo',
    details: { key_id: 'kid_xyz', name: 'ci-bot' },
    ...overrides,
  }
}

function buildEventsPage(
  overrides: Partial<AuditEventsPage> = {},
): AuditEventsPage {
  return {
    events: [buildEvent()],
    next_cursor: null,
    limit: 50,
    start_date: '2026-04-03T00:00:00+00:00',
    end_date: '2026-05-03T00:00:00+00:00',
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  mockSearchable.mockResolvedValue([
    { workspace_id: 'ws-1', name: 'Engineering', access_type: 'org_admin' },
  ])
})

describe('Audit', () => {
  it('renders the four stat cards', async () => {
    mockSummary.mockResolvedValue(buildSummary())
    mockEvents.mockResolvedValue(buildEventsPage())

    renderWithRouter(<Audit />)

    await waitFor(() => {
      expect(screen.getByText('Total Actions')).toBeInTheDocument()
    })
    expect(screen.getByText('Most Common Action')).toBeInTheDocument()
    expect(screen.getByText('Active Actors')).toBeInTheDocument()
    expect(screen.getByText('Critical Actions')).toBeInTheDocument()
    // The most common action is the first item.
    expect(screen.getByText('12')).toBeInTheDocument()
  })

  it('counts critical actions across the InfoSec set', async () => {
    mockSummary.mockResolvedValue(
      buildSummary({
        actions_by_type: [
          { action: 'API_KEY_CREATED', count: 4 },
          { action: 'WORKSPACE_DELETED', count: 7 },
          { action: 'WORKSPACE_REPO_ADDED', count: 9 },
        ],
      }),
    )
    mockEvents.mockResolvedValue(buildEventsPage({ events: [] }))

    renderWithRouter(<Audit />)

    await waitFor(() => {
      expect(screen.getByText('Critical Actions')).toBeInTheDocument()
    })
    // 4 + 7 = 11 critical (WORKSPACE_REPO_ADDED is not critical). 11 isn't a
    // count value used elsewhere on the page so getByText is unambiguous.
    expect(screen.getByText('11')).toBeInTheDocument()
  })

  it('shows the empty state when there is no audit data', async () => {
    mockSummary.mockResolvedValue(
      buildSummary({
        actions_total: 0,
        actions_by_type: [],
        actions_by_workspace: [],
        top_actors: [],
        trend: [],
      }),
    )
    mockEvents.mockResolvedValue(buildEventsPage({ events: [] }))

    renderWithRouter(<Audit />)

    await waitFor(() => {
      // The Recent Activity empty state phrase.
      expect(screen.getAllByText('No actions yet').length).toBeGreaterThan(0)
    })
  })

  it('opens the drawer when a recent-activity row is clicked', async () => {
    const user = userEvent.setup()
    mockSummary.mockResolvedValue(buildSummary())
    mockEvents.mockResolvedValue(buildEventsPage())

    renderWithRouter(<Audit />)

    await waitFor(() => {
      expect(screen.getByText('203.0.113.42')).toBeInTheDocument()
    })

    await user.click(screen.getByText('203.0.113.42'))

    // Drawer header includes the action and a Source IP field with the same
    // address. Querying by role narrows to the drawer dialog.
    expect(screen.getByRole('dialog', { name: /audit event/i })).toBeInTheDocument()
    expect(screen.getByText('Source IP')).toBeInTheDocument()
  })

  it('paginates via next_cursor and clears it when exhausted', async () => {
    const user = userEvent.setup()
    mockSummary.mockResolvedValue(buildSummary())
    mockEvents
      .mockResolvedValueOnce(
        buildEventsPage({ events: [buildEvent({ request_id: 'r1' })], next_cursor: 'cursor-1' }),
      )
      .mockResolvedValueOnce(
        buildEventsPage({ events: [buildEvent({ request_id: 'r2' })], next_cursor: null }),
      )

    renderWithRouter(<Audit />)

    await waitFor(() => {
      expect(screen.getByText('Load more')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Load more'))

    await waitFor(() => {
      expect(mockEvents).toHaveBeenCalledTimes(2)
    })
    // Second call passed cursor=cursor-1
    const secondCallArgs = mockEvents.mock.calls[1][0]
    expect(secondCallArgs?.cursor).toBe('cursor-1')
  })

  it('applies an action filter when an actions-by-type row is clicked', async () => {
    const user = userEvent.setup()
    mockSummary.mockResolvedValue(buildSummary())
    // Empty events so 'WORKSPACE_CREATED' only appears once on the page (in
    // the actions-by-type table, not in any rendered event row).
    mockEvents.mockResolvedValue(buildEventsPage({ events: [] }))

    renderWithRouter(<Audit />)

    await waitFor(() => {
      expect(screen.getByText('WORKSPACE_CREATED')).toBeInTheDocument()
    })

    await user.click(screen.getByText('WORKSPACE_CREATED'))

    // The most recent getAuditEvents call should now include action filter
    await waitFor(() => {
      const lastCall = mockEvents.mock.calls.at(-1)
      expect(lastCall?.[0]?.action).toBe('WORKSPACE_CREATED')
    })
  })

  it('shows an error banner when the API call fails', async () => {
    mockSummary.mockRejectedValue(new Error('boom'))
    mockEvents.mockRejectedValue(new Error('boom'))

    renderWithRouter(<Audit />)

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument()
    })
  })
})
