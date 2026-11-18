// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Settings from '../../../src/pages/Settings'
import { renderWithRouter } from '../../helpers/render'

vi.mock('../../../src/lib/api', () => ({
  getCurrentUser: vi.fn(),
  listApiKeys: vi.fn(),
  createApiKey: vi.fn(),
  revokeApiKey: vi.fn(),
}))

import {
  getCurrentUser,
  listApiKeys,
  createApiKey,
  revokeApiKey,
} from '../../../src/lib/api'

const mockGetCurrentUser = vi.mocked(getCurrentUser)
const mockListApiKeys = vi.mocked(listApiKeys)
const mockCreateApiKey = vi.mocked(createApiKey)
const mockRevokeApiKey = vi.mocked(revokeApiKey)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Settings', () => {
  it('renders profile and API keys sections', async () => {
    mockGetCurrentUser.mockResolvedValue({ email: 'user@example.com', name: 'Test User' })
    mockListApiKeys.mockResolvedValue([])

    renderWithRouter(<Settings />)

    await waitFor(() => {
      expect(screen.getByText('Test User')).toBeInTheDocument()
    })
    expect(screen.getByText('user@example.com')).toBeInTheDocument()
    expect(screen.getByText('Profile')).toBeInTheDocument()
    expect(screen.getByText('API Keys')).toBeInTheDocument()
    expect(screen.getByText('No API keys yet')).toBeInTheDocument()
  })

  it('shows loading state', () => {
    mockGetCurrentUser.mockReturnValue(new Promise(() => {}))
    mockListApiKeys.mockReturnValue(new Promise(() => {}))

    renderWithRouter(<Settings />)

    expect(screen.getByText('Settings')).toBeInTheDocument()
  })

  it('displays active API keys', async () => {
    mockGetCurrentUser.mockResolvedValue({ email: 'user@example.com' })
    mockListApiKeys.mockResolvedValue([
      {
        key_id: 'key-1',
        key_prefix: 'ooa_abc12345',
        name: 'Claude Code - Laptop',
        created_at: '2026-01-15T00:00:00Z',
        status: 'active' as const,
      },
    ])

    renderWithRouter(<Settings />)

    await waitFor(() => {
      expect(screen.getByText('Claude Code - Laptop')).toBeInTheDocument()
    })
    expect(screen.getByText('ooa_abc12345...')).toBeInTheDocument()
    expect(screen.getByText('Active Keys (1)')).toBeInTheDocument()
  })

  it('opens create API key modal', async () => {
    mockGetCurrentUser.mockResolvedValue({ email: 'user@example.com' })
    mockListApiKeys.mockResolvedValue([])

    renderWithRouter(<Settings />)
    await waitFor(() => expect(mockListApiKeys).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /create api key/i }))

    expect(screen.getByPlaceholderText('e.g., Claude Code - Laptop')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^create key$/i })).toBeInTheDocument()
  })

  it('creates an API key through the modal', async () => {
    mockGetCurrentUser.mockResolvedValue({ email: 'user@example.com' })
    mockListApiKeys.mockResolvedValue([])
    mockCreateApiKey.mockResolvedValue({
      key_id: 'key-new',
      key: 'ooa_abc123def456abc123def456abc123def456abcd',
      key_prefix: 'ooa_abc12345',
      name: 'My Key',
      created_at: '2026-03-10T00:00:00Z',
    })

    renderWithRouter(<Settings />)
    await waitFor(() => expect(mockListApiKeys).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /create api key/i }))
    await user.type(screen.getByPlaceholderText('e.g., Claude Code - Laptop'), 'My Key')
    await user.click(screen.getByRole('button', { name: /^create key$/i }))

    await waitFor(() => {
      expect(mockCreateApiKey).toHaveBeenCalledWith({ name: 'My Key' })
    })
    await waitFor(() => {
      expect(screen.getByText(/this key will only be shown once/i)).toBeInTheDocument()
    })
    // Key is displayed in an input field, not as text
    const keyInput = screen.getByDisplayValue('ooa_abc123def456abc123def456abc123def456abcd')
    expect(keyInput).toBeInTheDocument()
  })

  it('shows revoke confirmation inline', async () => {
    mockGetCurrentUser.mockResolvedValue({ email: 'user@example.com' })
    mockListApiKeys.mockResolvedValue([
      {
        key_id: 'key-1',
        key_prefix: 'ooa_abc12345',
        name: 'Test Key',
        created_at: '2026-01-15T00:00:00Z',
        status: 'active' as const,
      },
    ])
    mockRevokeApiKey.mockResolvedValue(undefined)

    renderWithRouter(<Settings />)
    await waitFor(() => expect(screen.getByText('Test Key')).toBeInTheDocument())

    const user = userEvent.setup()
    await user.click(screen.getByTitle('Revoke key'))

    expect(screen.getByText('Revoke this key?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /confirm/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument()
  })

  it('revokes an API key', async () => {
    mockGetCurrentUser.mockResolvedValue({ email: 'user@example.com' })
    mockListApiKeys.mockResolvedValue([
      {
        key_id: 'key-1',
        key_prefix: 'ooa_abc12345',
        name: 'Test Key',
        created_at: '2026-01-15T00:00:00Z',
        status: 'active' as const,
      },
    ])
    mockRevokeApiKey.mockResolvedValue(undefined)

    renderWithRouter(<Settings />)
    await waitFor(() => expect(screen.getByText('Test Key')).toBeInTheDocument())

    const user = userEvent.setup()
    await user.click(screen.getByTitle('Revoke key'))
    await user.click(screen.getByRole('button', { name: /confirm/i }))

    await waitFor(() => {
      expect(mockRevokeApiKey).toHaveBeenCalledWith('key-1')
    })
  })

  it('shows connection guide', async () => {
    mockGetCurrentUser.mockResolvedValue({ email: 'user@example.com' })
    mockListApiKeys.mockResolvedValue([])

    renderWithRouter(<Settings />)
    await waitFor(() => expect(mockListApiKeys).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByText('Connection Guide'))

    expect(screen.getByText('Claude Code (CLI)')).toBeInTheDocument()
    expect(screen.getByText('VS Code (GitHub Copilot)')).toBeInTheDocument()
    expect(screen.getByText('Cursor')).toBeInTheDocument()
  })

  it('shows error state', async () => {
    mockGetCurrentUser.mockRejectedValue(new Error('Network error'))
    mockListApiKeys.mockRejectedValue(new Error('Network error'))

    renderWithRouter(<Settings />)

    await waitFor(() => {
      expect(screen.getByText('Network error')).toBeInTheDocument()
    })
  })
})
