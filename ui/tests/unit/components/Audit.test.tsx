import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Audit from '../../../src/pages/Audit'
import { renderWithRouter } from '../../helpers/render'
import type {
  AuditSummary,
  AuditEventsPage,
  AuditEvent,
  AuditEventFilters,
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

// The Audit page makes two parallel getAuditEvents calls (admin + refusals
// fetched as bedrock/status=refusal). These helpers route each call to the
// right test fixture so a test can specify just the admin events or just
// the refusals without rewiring the whole mock chain.

const EMPTY_PAGE: AuditEventsPage = {
  events: [],
  next_cursor: null,
  limit: 25,
  start_date: '',
  end_date: '',
}

function isRefusalQuery(filters?: AuditEventFilters): boolean {
  return filters?.category === 'bedrock' && filters?.status === 'refusal'
}

/** Set a single admin response (re-used for "load more" repeats too). */
function setAdminEvents(page: AuditEventsPage, refusals: AuditEventsPage = EMPTY_PAGE) {
  mockEvents.mockImplementation(async (filters?: AuditEventFilters) => {
    return isRefusalQuery(filters) ? refusals : page
  })
}

/** Queue admin responses across multiple admin calls (e.g., pagination). */
function queueAdminPages(pages: AuditEventsPage[], refusals: AuditEventsPage = EMPTY_PAGE) {
  const queue = [...pages]
  mockEvents.mockImplementation(async (filters?: AuditEventFilters) => {
    if (isRefusalQuery(filters)) return refusals
    return queue.shift() ?? EMPTY_PAGE
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  mockSearchable.mockResolvedValue([
    { workspace_id: 'ws-1', name: 'Engineering', access_type: 'org_admin' },
  ])
  // Default: every call returns the canonical buildEventsPage; refusals
  // are empty unless the test opts in.
  setAdminEvents(buildEventsPage())
})

describe('Audit', () => {
  it('renders the four stat cards', async () => {
    mockSummary.mockResolvedValue(buildSummary())
    setAdminEvents(buildEventsPage())

    renderWithRouter(<Audit />)

    await waitFor(() => {
      expect(screen.getByText('Total Actions')).toBeInTheDocument()
    })
    expect(screen.getByText('Most Common Action')).toBeInTheDocument()
    expect(screen.getByText('Active Actors')).toBeInTheDocument()
    expect(screen.getByText('Critical Actions')).toBeInTheDocument()
    // Total Actions = summary.actions_total (12) + refusal count (0) = 12.
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
    setAdminEvents(buildEventsPage({ events: [] }))

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
    setAdminEvents(buildEventsPage({ events: [] }))

    renderWithRouter(<Audit />)

    await waitFor(() => {
      // The Recent Activity empty state phrase.
      expect(screen.getAllByText('No actions yet').length).toBeGreaterThan(0)
    })
  })

  it('opens the drawer when a recent-activity row is clicked', async () => {
    const user = userEvent.setup()
    mockSummary.mockResolvedValue(buildSummary())
    setAdminEvents(buildEventsPage())

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
    queueAdminPages([
      buildEventsPage({ events: [buildEvent({ request_id: 'r1' })], next_cursor: 'cursor-1' }),
      buildEventsPage({ events: [buildEvent({ request_id: 'r2' })], next_cursor: null }),
    ])

    renderWithRouter(<Audit />)

    await waitFor(() => {
      expect(screen.getByText('Load more')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Load more'))

    await waitFor(() => {
      // Second admin call should have happened (Load more click).
      const adminCalls = mockEvents.mock.calls.filter(
        ([args]) => args?.category !== 'bedrock' || args?.status !== 'refusal',
      )
      expect(adminCalls.length).toBe(2)
    })
    const adminCalls = mockEvents.mock.calls.filter(
      ([args]) => args?.category !== 'bedrock' || args?.status !== 'refusal',
    )
    expect(adminCalls[1]?.[0]?.cursor).toBe('cursor-1')
  })

  it('applies an action filter when an actions-by-type row is clicked', async () => {
    const user = userEvent.setup()
    mockSummary.mockResolvedValue(buildSummary())
    // Empty events so 'WORKSPACE_CREATED' only appears once on the page (in
    // the actions-by-type table, not in any rendered event row).
    setAdminEvents(buildEventsPage({ events: [] }))

    renderWithRouter(<Audit />)

    await waitFor(() => {
      expect(screen.getByText('WORKSPACE_CREATED')).toBeInTheDocument()
    })

    await user.click(screen.getByText('WORKSPACE_CREATED'))

    // The most recent admin call should include action filter.
    await waitFor(() => {
      const adminCalls = mockEvents.mock.calls.filter(
        ([args]) => args?.category !== 'bedrock' || args?.status !== 'refusal',
      )
      expect(adminCalls.at(-1)?.[0]?.action).toBe('WORKSPACE_CREATED')
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

  it('merges refusals inline with admin actions sorted newest-first', async () => {
    mockSummary.mockResolvedValue(buildSummary())
    setAdminEvents(
      buildEventsPage({
        events: [
          buildEvent({
            request_id: 'admin-1',
            timestamp: '2026-05-04T10:00:00+00:00',
          }),
        ],
      }),
      buildEventsPage({
        events: [
          {
            request_id: 'refusal-1',
            timestamp: '2026-05-04T11:00:00+00:00',
            category: 'bedrock',
            action: '',
            user_email: 'brian@example.com',
            workspace_id: '',
            source_ip: '24.253.92.153',
            user_agent: 'Mozilla/5.0',
            details: {},
            status: 'refusal',
            flagged_reason: 'test_refusal_trigger',
            lambda_name: 'chat',
            model_id: 'us.anthropic.claude-sonnet-4-6',
          },
        ],
        next_cursor: null,
      }),
    )

    renderWithRouter(<Audit />)

    await waitFor(() => {
      // Refusal pill renders the literal "REFUSAL" label (not the empty action).
      expect(screen.getByText('REFUSAL')).toBeInTheDocument()
    })

    // The refusal-specific call (category=bedrock, status=refusal) was made.
    const sawRefusalCall = mockEvents.mock.calls.some(([args]) =>
      args?.category === 'bedrock' && args?.status === 'refusal',
    )
    expect(sawRefusalCall).toBe(true)

    // Newest-first: refusal (11:00) renders above the admin row (10:00).
    const tbody = screen.getByText('REFUSAL').closest('tbody') as HTMLElement
    const rows = tbody.querySelectorAll('tr')
    expect(rows[0].textContent).toContain('REFUSAL')
  })

  it('refusals roll into the Critical Actions stat', async () => {
    // Build a summary with values that don't collide with the count we
    // expect on the Critical Actions card so getByText is unambiguous.
    mockSummary.mockResolvedValue(
      buildSummary({
        actions_total: 100,
        actions_by_type: [{ action: 'API_KEY_CREATED', count: 2 }], // critical
        actions_by_workspace: [],
        top_actors: [],
        trend: [],
      }),
    )
    // 5 refusals → 2 + 5 = 7 critical (a value that doesn't appear elsewhere).
    setAdminEvents(
      buildEventsPage({ events: [] }),
      buildEventsPage({
        events: Array.from({ length: 5 }, (_, i) => ({
          ...buildEvent({ request_id: `refusal-${i}` }),
          status: 'refusal',
          action: '',
          category: 'bedrock',
        })) as AuditEvent[],
        next_cursor: null,
      }),
    )

    renderWithRouter(<Audit />)

    await waitFor(() => {
      expect(screen.getByText('Critical Actions')).toBeInTheDocument()
    })
    // 2 admin-critical (API_KEY_CREATED) + 5 refusals = 7.
    expect(screen.getByText('7')).toBeInTheDocument()
  })
})
