import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ShareDialog from '../../../src/components/ShareDialog'
import { createSearchableWorkspace } from '../../helpers/mocks'

vi.mock('../../../src/lib/api', () => ({
  getConversation: vi.fn(),
  shareConversation: vi.fn(),
}))

import {
  getConversation,
  shareConversation,
} from '../../../src/lib/api'

const mockGetConversation = vi.mocked(getConversation)
const mockShareConversation = vi.mocked(shareConversation)

const workspaces = [
  createSearchableWorkspace({ workspace_id: 'ws-1', name: 'Workspace A', access_type: 'member' }),
  createSearchableWorkspace({ workspace_id: 'ws-2', name: 'Workspace B', access_type: 'org_admin' }),
  createSearchableWorkspace({ workspace_id: 'ws-3', name: 'Shared WS', access_type: 'shared' }),
]

const defaultProps = {
  conversationId: 'conv-1',
  workspaces,
  onClose: vi.fn(),
  onShareUpdate: vi.fn(),
}

beforeEach(() => {
  vi.clearAllMocks()
  mockGetConversation.mockResolvedValue({
    conversation: {
      conversation_id: 'conv-1',
      title: 'Test',
      message_count: 0,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
      shared_workspace_ids: [],
      is_owner: true,
    },
    messages: [],
  })
  mockShareConversation.mockResolvedValue({
    shared_workspace_ids: ['ws-1'],
    share_url: '/chat/conv-1',
  })
})

describe('ShareDialog', () => {
  it('renders loading state initially', () => {
    mockGetConversation.mockReturnValue(new Promise(() => {}))
    render(<ShareDialog {...defaultProps} />)
    expect(document.querySelector('.animate-spin')).toBeInTheDocument()
  })

  it('renders workspace checkboxes after loading', async () => {
    render(<ShareDialog {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Workspace A')).toBeInTheDocument()
    })
    expect(screen.getByText('Workspace B')).toBeInTheDocument()
    // Shared workspace should NOT be shown
    expect(screen.queryByText('Shared WS')).not.toBeInTheDocument()
  })

  it('pre-checks already shared workspaces', async () => {
    mockGetConversation.mockResolvedValue({
      conversation: {
        conversation_id: 'conv-1',
        title: 'Test',
        message_count: 0,
        created_at: '2024-01-01',
        updated_at: '2024-01-01',
        shared_workspace_ids: ['ws-1'],
        is_owner: true,
      },
      messages: [],
    })

    render(<ShareDialog {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Workspace A')).toBeInTheDocument()
    })

    const checkboxes = screen.getAllByRole('checkbox')
    const wsACheckbox = checkboxes.find(
      cb => cb.closest('label')?.textContent?.includes('Workspace A')
    ) as HTMLInputElement
    expect(wsACheckbox.checked).toBe(true)
  })

  it('calls onClose when close button clicked', async () => {
    render(<ShareDialog {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Share Conversation')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const closeButton = screen.getByText('Share Conversation').parentElement!.querySelector('button')!
    await user.click(closeButton)

    expect(defaultProps.onClose).toHaveBeenCalled()
  })

  it('calls onClose when overlay clicked', async () => {
    render(<ShareDialog {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Share Conversation')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const overlay = document.querySelector('.fixed.inset-0')!
    await user.click(overlay)

    expect(defaultProps.onClose).toHaveBeenCalled()
  })

  it('calls shareConversation with selected workspaces on save', async () => {
    render(<ShareDialog {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Workspace A')).toBeInTheDocument()
    })

    const user = userEvent.setup()

    // Check Workspace A
    const checkboxes = screen.getAllByRole('checkbox')
    const wsACheckbox = checkboxes.find(
      cb => cb.closest('label')?.textContent?.includes('Workspace A')
    )!
    await user.click(wsACheckbox)

    // Click Save
    await user.click(screen.getByText('Save'))

    await waitFor(() => {
      expect(mockShareConversation).toHaveBeenCalledWith('conv-1', ['ws-1'])
    })
  })

  it('shows Remove All Sharing button when conversation is shared', async () => {
    mockGetConversation.mockResolvedValue({
      conversation: {
        conversation_id: 'conv-1',
        title: 'Test',
        message_count: 0,
        created_at: '2024-01-01',
        updated_at: '2024-01-01',
        shared_workspace_ids: ['ws-1'],
        is_owner: true,
      },
      messages: [],
    })

    render(<ShareDialog {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Remove All Sharing')).toBeInTheDocument()
    })
  })

  it('does not show Remove All Sharing when not shared', async () => {
    render(<ShareDialog {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Workspace A')).toBeInTheDocument()
    })
    expect(screen.queryByText('Remove All Sharing')).not.toBeInTheDocument()
  })

  it('calls shareConversation with empty array on Remove All', async () => {
    mockGetConversation.mockResolvedValue({
      conversation: {
        conversation_id: 'conv-1',
        title: 'Test',
        message_count: 0,
        created_at: '2024-01-01',
        updated_at: '2024-01-01',
        shared_workspace_ids: ['ws-1'],
        is_owner: true,
      },
      messages: [],
    })
    mockShareConversation.mockResolvedValue({
      shared_workspace_ids: [],
      share_url: '/chat/conv-1',
    })

    render(<ShareDialog {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Remove All Sharing')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Remove All Sharing'))

    await waitFor(() => {
      expect(mockShareConversation).toHaveBeenCalledWith('conv-1', [])
    })
  })

  it('shows Copy Share Link button', async () => {
    render(<ShareDialog {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Copy Share Link')).toBeInTheDocument()
    })
  })

  it('shows Cancel and Save buttons', async () => {
    render(<ShareDialog {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Cancel')).toBeInTheDocument()
      expect(screen.getByText('Save')).toBeInTheDocument()
    })
  })
})
