import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import SharedConversationsList from '../../../src/components/SharedConversationsList'
import { renderWithRouter } from '../../helpers/render'

vi.mock('../../../src/lib/api', () => ({
  listSharedConversations: vi.fn(),
  shareConversation: vi.fn(),
}))

import { listSharedConversations } from '../../../src/lib/api'

const mockListSharedConversations = vi.mocked(listSharedConversations)

beforeEach(() => {
  vi.clearAllMocks()
})

function renderList() {
  return renderWithRouter(<SharedConversationsList />, {
    initialEntries: ['/chat'],
  })
}

describe('SharedConversationsList', () => {
  it('shows loading state initially', () => {
    mockListSharedConversations.mockReturnValue(new Promise(() => {}))
    renderList()
    expect(document.querySelector('.animate-spin')).toBeInTheDocument()
  })

  it('renders nothing when no shared conversations', async () => {
    mockListSharedConversations.mockResolvedValue({
      conversations: [],
      has_more: false,
    })

    const { container } = renderList()

    await waitFor(() => {
      expect(mockListSharedConversations).toHaveBeenCalled()
    })
    // Should render nothing (null)
    expect(container.querySelector('[class*="px-4"]')).not.toBeInTheDocument()
  })

  it('renders shared conversations', async () => {
    mockListSharedConversations.mockResolvedValue({
      conversations: [
        {
          conversation_id: 'sc-1',
          title: 'Shared Chat One',
          message_count: 5,
          shared_by: 'alice@example.com',
          updated_at: '2024-01-01T00:00:00Z',
          workspace_id: 'ws-1',
        },
        {
          conversation_id: 'sc-2',
          title: 'Shared Chat Two',
          message_count: 3,
          shared_by: 'bob@example.com',
          updated_at: '2024-01-02T00:00:00Z',
          workspace_id: 'ws-1',
        },
      ],
      has_more: false,
    })

    renderList()

    await waitFor(() => {
      expect(screen.getByText('Shared Chat One')).toBeInTheDocument()
    })
    expect(screen.getByText('Shared Chat Two')).toBeInTheDocument()
    expect(screen.getByText('alice@example.com')).toBeInTheDocument()
    expect(screen.getByText('bob@example.com')).toBeInTheDocument()
  })

  it('shows "Shared with me" heading', async () => {
    mockListSharedConversations.mockResolvedValue({
      conversations: [
        {
          conversation_id: 'sc-1',
          title: 'A Chat',
          message_count: 1,
          shared_by: 'user@example.com',
          updated_at: '2024-01-01T00:00:00Z',
          workspace_id: 'ws-1',
        },
      ],
      has_more: false,
    })

    renderList()

    await waitFor(() => {
      expect(screen.getByText('Shared with me')).toBeInTheDocument()
    })
  })

  it('shows Load More when has_more is true', async () => {
    mockListSharedConversations.mockResolvedValue({
      conversations: [
        {
          conversation_id: 'sc-1',
          title: 'A Chat',
          message_count: 1,
          shared_by: 'user@example.com',
          updated_at: '2024-01-01T00:00:00Z',
          workspace_id: 'ws-1',
        },
      ],
      has_more: true,
      next_cursor: 'cursor1',
    })

    renderList()

    await waitFor(() => {
      expect(screen.getByText('Load More')).toBeInTheDocument()
    })
  })

  it('hides Load More when has_more is false', async () => {
    mockListSharedConversations.mockResolvedValue({
      conversations: [
        {
          conversation_id: 'sc-1',
          title: 'A Chat',
          message_count: 1,
          shared_by: 'user@example.com',
          updated_at: '2024-01-01T00:00:00Z',
          workspace_id: 'ws-1',
        },
      ],
      has_more: false,
    })

    renderList()

    await waitFor(() => {
      expect(screen.getByText('A Chat')).toBeInTheDocument()
    })
    expect(screen.queryByText('Load More')).not.toBeInTheDocument()
  })

  it('loads more conversations when Load More is clicked', async () => {
    mockListSharedConversations
      .mockResolvedValueOnce({
        conversations: [
          {
            conversation_id: 'sc-1',
            title: 'First',
            message_count: 1,
            shared_by: 'user@example.com',
            updated_at: '2024-01-01T00:00:00Z',
            workspace_id: 'ws-1',
          },
        ],
        has_more: true,
        next_cursor: 'cursor1',
      })
      .mockResolvedValueOnce({
        conversations: [
          {
            conversation_id: 'sc-2',
            title: 'Second',
            message_count: 2,
            shared_by: 'other@example.com',
            updated_at: '2024-01-02T00:00:00Z',
            workspace_id: 'ws-1',
          },
        ],
        has_more: false,
      })

    renderList()

    await waitFor(() => {
      expect(screen.getByText('Load More')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Load More'))

    await waitFor(() => {
      expect(screen.getByText('Second')).toBeInTheDocument()
    })
    expect(mockListSharedConversations).toHaveBeenCalledTimes(2)
  })
})
