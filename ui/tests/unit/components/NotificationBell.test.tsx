// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import NotificationBell, { NOTIFICATION_POLL_MS } from '../../../src/components/NotificationBell'
import { renderWithRouter } from '../../helpers/render'

vi.mock('../../../src/lib/api', () => ({
  listNotifications: vi.fn(),
  dismissNotification: vi.fn(),
}))

import { listNotifications, dismissNotification, type Notification } from '../../../src/lib/api'

const mockList = vi.mocked(listNotifications)
const mockDismiss = vi.mocked(dismissNotification)

function makeNotification(overrides: Partial<Notification> = {}): Notification {
  return {
    notification_id: 'n-1',
    workspace_id: 'ws-1',
    type: 'repo_stalled',
    severity: 'warning',
    title: 'Repo ingestion stalled',
    message: 'Code maps are behind the latest commit.',
    repo_name: 'acme/api',
    added_by: 'user@example.com',
    reason: 'max_attempts',
    target_sha: 'abc123',
    failed_files: [],
    failed_files_total: 0,
    created_at: '2026-11-19T10:00:00Z',
    updated_at: '2026-11-19T10:00:00Z',
    ...overrides,
  } as Notification
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('NotificationBell', () => {
  it('renders nothing when there are no notifications', async () => {
    mockList.mockResolvedValue([])
    renderWithRouter(<NotificationBell />)
    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('button', { name: /notifications/i })).not.toBeInTheDocument()
  })

  it('shows a count badge for two notifications', async () => {
    mockList.mockResolvedValue([
      makeNotification(),
      makeNotification({ notification_id: 'n-2', title: 'Files failed', severity: 'error' }),
    ])
    renderWithRouter(<NotificationBell />)
    expect(await screen.findByRole('button', { name: 'Notifications (2)' })).toBeInTheDocument()
    expect(screen.getByTestId('notification-count')).toHaveTextContent('2')
  })

  it('lists details and truncates the failed-file list', async () => {
    const user = userEvent.setup()
    mockList.mockResolvedValue([
      makeNotification({
        severity: 'error',
        title: 'Files failed to ingest',
        failed_files: ['a.py', 'b.py', 'c.py', 'd.py', 'e.py', 'f.py'],
        failed_files_total: 8,
      }),
    ])
    renderWithRouter(<NotificationBell />)
    await user.click(await screen.findByRole('button', { name: 'Notifications (1)' }))

    expect(screen.getByText('Files failed to ingest')).toBeInTheDocument()
    expect(screen.getByText('ws-1 / acme/api')).toBeInTheDocument()
    expect(screen.getByText('Code maps are behind the latest commit.')).toBeInTheDocument()
    expect(screen.getByText('e.py')).toBeInTheDocument()
    expect(screen.queryByText('f.py')).not.toBeInTheDocument()
    expect(screen.getByText('and 3 more')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Workspace settings' })).toHaveAttribute(
      'href',
      '/workspaces/ws-1/settings',
    )
  })

  it('closes the panel when Escape is pressed', async () => {
    const user = userEvent.setup()
    mockList.mockResolvedValue([makeNotification()])
    renderWithRouter(<NotificationBell />)
    await user.click(await screen.findByRole('button', { name: 'Notifications (1)' }))
    expect(screen.getByRole('region', { name: 'Notifications' })).toBeInTheDocument()

    await user.keyboard('{Escape}')

    expect(screen.queryByRole('region', { name: 'Notifications' })).not.toBeInTheDocument()
  })

  it('closes the panel when the backdrop is clicked', async () => {
    const user = userEvent.setup()
    mockList.mockResolvedValue([makeNotification()])
    renderWithRouter(<NotificationBell />)
    await user.click(await screen.findByRole('button', { name: 'Notifications (1)' }))

    await user.click(screen.getByRole('button', { name: 'Close notifications' }))

    expect(screen.queryByRole('region', { name: 'Notifications' })).not.toBeInTheDocument()
  })

  it('dismiss calls the API and removes the item', async () => {
    const user = userEvent.setup()
    mockList.mockResolvedValue([
      makeNotification(),
      makeNotification({ notification_id: 'n-2', title: 'Second alert' }),
    ])
    mockDismiss.mockResolvedValue(undefined)
    renderWithRouter(<NotificationBell />)
    await user.click(await screen.findByRole('button', { name: 'Notifications (2)' }))

    await user.click(screen.getByRole('button', { name: 'Dismiss Repo ingestion stalled' }))

    expect(mockDismiss).toHaveBeenCalledWith('ws-1', 'n-1')
    await waitFor(() =>
      expect(screen.queryByText('Repo ingestion stalled')).not.toBeInTheDocument(),
    )
    expect(screen.getByText('Second alert')).toBeInTheDocument()
    expect(screen.getByTestId('notification-count')).toHaveTextContent('1')
  })

  it('restores the item and shows the error when dismiss fails', async () => {
    const user = userEvent.setup()
    mockList.mockResolvedValue([makeNotification()])
    mockDismiss.mockRejectedValue(new Error('Forbidden'))
    renderWithRouter(<NotificationBell />)
    await user.click(await screen.findByRole('button', { name: 'Notifications (1)' }))

    await user.click(screen.getByRole('button', { name: 'Dismiss Repo ingestion stalled' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Forbidden')
    expect(screen.getByText('Repo ingestion stalled')).toBeInTheDocument()
  })

  it('polls listNotifications again after 60 seconds', async () => {
    vi.useFakeTimers()
    mockList.mockResolvedValue([makeNotification()])
    renderWithRouter(<NotificationBell />)
    await act(async () => {
      await Promise.resolve()
    })
    expect(mockList).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_MS)
    })
    expect(mockList).toHaveBeenCalledTimes(2)
  })
})
