import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AllChatsModal from '../../../src/components/AllChatsModal'
import { createConversation } from '../../helpers/mocks'

vi.mock('../../../src/lib/api', () => ({
  listConversations: vi.fn(),
  deleteConversation: vi.fn(),
}))

import { listConversations, deleteConversation } from '../../../src/lib/api'

const mockListConversations = vi.mocked(listConversations)
const mockDeleteConversation = vi.mocked(deleteConversation)

const defaultProps = {
  onClose: vi.fn(),
  onSelect: vi.fn(),
  onDelete: vi.fn(),
}

beforeEach(() => {
  vi.clearAllMocks()
  mockListConversations.mockResolvedValue({
    conversations: [],
    has_more: false,
  })
  mockDeleteConversation.mockResolvedValue(undefined)
})

describe('AllChatsModal', () => {
  it('renders loading state initially', () => {
    // Don't resolve the promise yet
    mockListConversations.mockReturnValue(new Promise(() => {}))
    render(<AllChatsModal {...defaultProps} />)
    // Loader2 renders as an SVG with animate-spin class
    expect(document.querySelector('.animate-spin')).toBeInTheDocument()
  })

  it('renders conversations after loading', async () => {
    const convs = [
      createConversation({ conversation_id: 'c1', title: 'Test Chat', updated_at: new Date().toISOString() }),
    ]
    mockListConversations.mockResolvedValue({
      conversations: convs,
      has_more: false,
    })

    render(<AllChatsModal {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Test Chat')).toBeInTheDocument()
    })
  })

  it('filters conversations by search query', async () => {
    const now = new Date().toISOString()
    const convs = [
      createConversation({ conversation_id: 'c1', title: 'Alpha Chat', updated_at: now }),
      createConversation({ conversation_id: 'c2', title: 'Beta Discussion', updated_at: now }),
    ]
    mockListConversations.mockResolvedValue({
      conversations: convs,
      has_more: false,
    })

    render(<AllChatsModal {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Alpha Chat')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const searchInput = screen.getByPlaceholderText('Search conversations...')
    await user.type(searchInput, 'Beta')

    expect(screen.queryByText('Alpha Chat')).not.toBeInTheDocument()
    expect(screen.getByText('Beta Discussion')).toBeInTheDocument()
  })

  it('calls onSelect when conversation clicked', async () => {
    const conv = createConversation({ conversation_id: 'c1', title: 'Click Me', updated_at: new Date().toISOString() })
    mockListConversations.mockResolvedValue({
      conversations: [conv],
      has_more: false,
    })

    render(<AllChatsModal {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Click Me')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Click Me'))

    expect(defaultProps.onSelect).toHaveBeenCalledWith(conv)
  })

  it('shows "Show More" when has_more is true', async () => {
    mockListConversations.mockResolvedValue({
      conversations: [createConversation({ updated_at: new Date().toISOString() })],
      has_more: true,
      next_cursor: 'abc123',
    })

    render(<AllChatsModal {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Show More')).toBeInTheDocument()
    })
  })

  it('hides "Show More" when no more results', async () => {
    mockListConversations.mockResolvedValue({
      conversations: [createConversation({ updated_at: new Date().toISOString() })],
      has_more: false,
    })

    render(<AllChatsModal {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Test Conversation')).toBeInTheDocument()
    })
    expect(screen.queryByText('Show More')).not.toBeInTheDocument()
  })

  it('loads more conversations when Show More clicked', async () => {
    const now = new Date().toISOString()
    mockListConversations
      .mockResolvedValueOnce({
        conversations: [createConversation({ conversation_id: 'c1', title: 'First', updated_at: now })],
        has_more: true,
        next_cursor: 'cursor1',
      })
      .mockResolvedValueOnce({
        conversations: [createConversation({ conversation_id: 'c2', title: 'Second', updated_at: now })],
        has_more: false,
      })

    render(<AllChatsModal {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Show More')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Show More'))

    await waitFor(() => {
      expect(screen.getByText('Second')).toBeInTheDocument()
    })
    expect(mockListConversations).toHaveBeenCalledWith(50, 'cursor1')
  })

  it('shows empty state when no conversations', async () => {
    render(<AllChatsModal {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('No conversations yet')).toBeInTheDocument()
    })
  })

  it('shows no matching conversations message when search has no results', async () => {
    mockListConversations.mockResolvedValue({
      conversations: [createConversation({ conversation_id: 'c1', title: 'Hello', updated_at: new Date().toISOString() })],
      has_more: false,
    })

    render(<AllChatsModal {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Hello')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText('Search conversations...'), 'zzzzz')

    expect(screen.getByText('No matching conversations')).toBeInTheDocument()
  })

  it('closes when X button is clicked', async () => {
    mockListConversations.mockResolvedValue({
      conversations: [createConversation({ updated_at: new Date().toISOString() })],
      has_more: false,
    })

    render(<AllChatsModal {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('All Chats')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    // X button is in the header
    const closeButton = screen.getByText('All Chats').parentElement!.querySelector('button')!
    await user.click(closeButton)

    expect(defaultProps.onClose).toHaveBeenCalled()
  })

  it('auto-focuses the search input', async () => {
    mockListConversations.mockResolvedValue({
      conversations: [],
      has_more: false,
    })

    render(<AllChatsModal {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByPlaceholderText('Search conversations...')).toHaveFocus()
    })
  })
})
