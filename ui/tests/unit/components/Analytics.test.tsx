// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Analytics from '../../../src/pages/Analytics'
import { renderWithRouter } from '../../helpers/render'
import type {
  AnalyticsSummary,
  AnalyticsUser,
  SearchableWorkspace,
} from '../../../src/lib/api'

vi.mock('../../../src/lib/api', () => ({
  getAnalyticsSummary: vi.fn(),
  getAnalyticsUsers: vi.fn(),
  getAnalyticsWorkspace: vi.fn(),
  getSearchableWorkspaces: vi.fn(),
}))

import {
  getAnalyticsSummary,
  getAnalyticsUsers,
  getSearchableWorkspaces,
} from '../../../src/lib/api'

const mockSummary = vi.mocked(getAnalyticsSummary)
const mockUsers = vi.mocked(getAnalyticsUsers)
const mockSearchable = vi.mocked(getSearchableWorkspaces)

function buildSummary(overrides: Partial<AnalyticsSummary> = {}): AnalyticsSummary {
  return {
    total_cost_usd: 142.5,
    total_queries: 3847,
    total_input_tokens: 45_000_000,
    total_output_tokens: 12_000_000,
    by_model: {
      'us.anthropic.claude-sonnet-4-6': { queries: 2100, cost_usd: 85.3 },
      'us.anthropic.claude-haiku-4-5-20251001-v1:0': { queries: 1747, cost_usd: 12.2 },
    },
    by_workspace: [
      { workspace_id: 'ws-1', name: 'Engineering', queries: 2000, cost_usd: 95.0 },
      { workspace_id: 'ws-2', name: 'Data Science', queries: 800, cost_usd: 32.5 },
    ],
    daily_trend: [
      { date: '2026-04-01', queries: 120, cost_usd: 4.5 },
      { date: '2026-04-02', queries: 140, cost_usd: 5.2 },
    ],
    ...overrides,
  }
}

function buildUser(overrides: Partial<AnalyticsUser> = {}): AnalyticsUser {
  return {
    email: 'dev@company.com',
    queries: 450,
    cost_usd: 18.5,
    input_tokens: 5_400_000,
    output_tokens: 1_200_000,
    last_active: new Date().toISOString(),
    ...overrides,
  }
}

function buildWorkspaces(overrides: Partial<SearchableWorkspace>[] = []): SearchableWorkspace[] {
  if (overrides.length === 0) {
    return [
      { workspace_id: 'ws-1', name: 'Engineering', access_type: 'org_admin' },
    ]
  }
  return overrides.map((o, i) => ({
    workspace_id: `ws-${i}`,
    name: `Workspace ${i}`,
    access_type: 'member' as const,
    ...o,
  }))
}

beforeEach(() => {
  vi.clearAllMocks()
  mockSearchable.mockResolvedValue(buildWorkspaces())
})

describe('Analytics', () => {
  it('renders stat cards with summary data', async () => {
    mockSummary.mockResolvedValue(buildSummary())
    mockUsers.mockResolvedValue({ users: [buildUser()] })

    renderWithRouter(<Analytics />)

    await waitFor(() => {
      expect(screen.getByText('$142.50')).toBeInTheDocument()
    })
    expect(screen.getByText('3,847')).toBeInTheDocument()
    expect(screen.getByText('45.0M')).toBeInTheDocument()
  })

  it('renders daily trend chart', async () => {
    mockSummary.mockResolvedValue(buildSummary())
    mockUsers.mockResolvedValue({ users: [] })

    const { container } = renderWithRouter(<Analytics />)
    await waitFor(() => {
      expect(screen.getByText('Daily Cost Trend')).toBeInTheDocument()
    })
    // Recharts renders the line chart into an SVG
    expect(container.querySelector('svg')).not.toBeNull()
  })

  it('renders workspace cost breakdown', async () => {
    mockSummary.mockResolvedValue(buildSummary())
    mockUsers.mockResolvedValue({ users: [] })
    // Non-admin to avoid workspace filter dropdown rendering names too
    mockSearchable.mockResolvedValue([
      { workspace_id: 'ws-1', name: 'Engineering', access_type: 'member' },
    ])

    renderWithRouter(<Analytics />)
    await waitFor(() => {
      expect(screen.getByText('Cost by Workspace')).toBeInTheDocument()
    })
    expect(screen.getByText('Engineering')).toBeInTheDocument()
    expect(screen.getByText('Data Science')).toBeInTheDocument()
    expect(screen.getByText('$95.00')).toBeInTheDocument()
  })

  it('renders model cost breakdown and strips anthropic prefix', async () => {
    mockSummary.mockResolvedValue(buildSummary())
    mockUsers.mockResolvedValue({ users: [] })

    renderWithRouter(<Analytics />)
    await waitFor(() => {
      expect(screen.getByText('Cost by Model')).toBeInTheDocument()
    })
    expect(screen.getByText('claude-sonnet-4-6')).toBeInTheDocument()
    // Strips both us.anthropic. prefix and -v1:0 suffix
    expect(screen.getByText('claude-haiku-4-5-20251001')).toBeInTheDocument()
  })

  it('renders top users table', async () => {
    mockSummary.mockResolvedValue(buildSummary())
    mockUsers.mockResolvedValue({
      users: [
        buildUser({ email: 'alice@company.com', queries: 500, cost_usd: 20 }),
        buildUser({ email: 'bob@company.com', queries: 300, cost_usd: 12 }),
      ],
    })

    renderWithRouter(<Analytics />)
    await waitFor(() => {
      expect(screen.getByText('alice@company.com')).toBeInTheDocument()
    })
    expect(screen.getByText('bob@company.com')).toBeInTheDocument()
    expect(screen.getByText('500')).toBeInTheDocument()
  })

  it('shows loading state while fetching', () => {
    mockSummary.mockReturnValue(new Promise(() => {}))
    mockUsers.mockReturnValue(new Promise(() => {}))

    const { container } = renderWithRouter(<Analytics />)
    expect(container.querySelector('.animate-spin')).not.toBeNull()
  })

  it('handles error state', async () => {
    mockSummary.mockRejectedValue(new Error('Boom'))
    mockUsers.mockResolvedValue({ users: [] })

    renderWithRouter(<Analytics />)
    await waitFor(() => {
      expect(screen.getByText('Failed to load analytics')).toBeInTheDocument()
    })
    expect(screen.getByText('Boom')).toBeInTheDocument()
  })

  it('refetches when date range changes', async () => {
    mockSummary.mockResolvedValue(buildSummary())
    mockUsers.mockResolvedValue({ users: [] })

    renderWithRouter(<Analytics />)
    await waitFor(() => {
      expect(mockSummary).toHaveBeenCalledTimes(1)
    })

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '90d' }))

    await waitFor(() => {
      expect(mockSummary).toHaveBeenCalledTimes(2)
    })
  })

  it('shows workspace filter for org admins', async () => {
    mockSummary.mockResolvedValue(buildSummary())
    mockUsers.mockResolvedValue({ users: [] })
    mockSearchable.mockResolvedValue([
      { workspace_id: 'ws-1', name: 'Engineering', access_type: 'org_admin' },
      { workspace_id: 'ws-2', name: 'Data Science', access_type: 'org_admin' },
    ])

    renderWithRouter(<Analytics />)
    await waitFor(() => {
      expect(screen.getByLabelText('Filter by workspace')).toBeInTheDocument()
    })
  })

  it('hides workspace filter for non-admins', async () => {
    mockSummary.mockResolvedValue(buildSummary())
    mockUsers.mockResolvedValue({ users: [] })
    mockSearchable.mockResolvedValue([
      { workspace_id: 'ws-1', name: 'Engineering', access_type: 'member' },
    ])

    renderWithRouter(<Analytics />)
    await waitFor(() => {
      expect(screen.getByText('Analytics')).toBeInTheDocument()
    })
    expect(screen.queryByLabelText('Filter by workspace')).not.toBeInTheDocument()
  })

  it('shows empty daily trend when no activity', async () => {
    mockSummary.mockResolvedValue(buildSummary({ daily_trend: [], total_queries: 0 }))
    mockUsers.mockResolvedValue({ users: [] })

    renderWithRouter(<Analytics />)
    await waitFor(() => {
      expect(screen.getByText('No activity in this range.')).toBeInTheDocument()
    })
  })
})
