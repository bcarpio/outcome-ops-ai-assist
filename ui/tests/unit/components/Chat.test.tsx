import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Chat from '../../../src/pages/Chat'
import { renderWithRouter } from '../../helpers/render'
import { createConversation, createSearchableWorkspace } from '../../helpers/mocks'

vi.mock('../../../src/lib/api', () => ({
  sendChatMessage: vi.fn(),
  listConversations: vi.fn(),
  getConversation: vi.fn(),
  deleteConversation: vi.fn(),
  getSearchableWorkspaces: vi.fn(),
  getChatMcpServers: vi.fn(),
}))

vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="markdown">{children}</div>,
}))

import {
  sendChatMessage,
  listConversations,
  getConversation,
  deleteConversation,
  getSearchableWorkspaces,
  getChatMcpServers,
} from '../../../src/lib/api'

const mockSendChatMessage = vi.mocked(sendChatMessage)
const mockListConversations = vi.mocked(listConversations)
const mockGetConversation = vi.mocked(getConversation)
const mockDeleteConversation = vi.mocked(deleteConversation)
const mockGetSearchableWorkspaces = vi.mocked(getSearchableWorkspaces)
const mockGetChatMcpServers = vi.mocked(getChatMcpServers)

beforeEach(() => {
  vi.clearAllMocks()
  mockListConversations.mockResolvedValue([])
  mockGetSearchableWorkspaces.mockResolvedValue([])
  mockGetChatMcpServers.mockResolvedValue([])
})

function renderChat(conversationId?: string) {
  const entry = conversationId ? `/chat/${conversationId}` : '/chat'
  return renderWithRouter(<Chat />, {
    initialEntries: [entry],
    path: '/chat/:conversationId?',
  })
}

describe('Chat', () => {
  it('renders empty chat state', async () => {
    renderChat()

    await waitFor(() => {
      expect(screen.getByText('Ask anything about your codebase')).toBeInTheDocument()
    })
    expect(screen.getByPlaceholderText('Ask about your codebase...')).toBeInTheDocument()
  })

  it('renders sidebar conversations', async () => {
    const convs = [
      createConversation({ conversation_id: 'c1', title: 'First Chat' }),
      createConversation({ conversation_id: 'c2', title: 'Second Chat' }),
    ]
    mockListConversations.mockResolvedValue(convs)

    renderChat()

    await waitFor(() => {
      expect(screen.getByText('First Chat')).toBeInTheDocument()
    })
    expect(screen.getByText('Second Chat')).toBeInTheDocument()
  })

  it('shows New Chat button in sidebar', async () => {
    renderChat()

    await waitFor(() => {
      const buttons = screen.getAllByText('New Chat')
      expect(buttons.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('sends a message on button click', async () => {
    mockSendChatMessage.mockImplementation(async (_req, onEvent) => {
      onEvent({ type: 'metadata', conversation_id: 'c1', sources: [], title: 'Test' })
      onEvent({ type: 'token', text: 'Hello' })
      onEvent({ type: 'done', message_id: 'msg-1' })
    })

    renderChat()
    await waitFor(() => expect(mockListConversations).toHaveBeenCalled())

    const user = userEvent.setup()
    const input = screen.getByPlaceholderText('Ask about your codebase...')
    await user.type(input, 'test question')

    // Send button is the blue button with the Send icon - find by its class
    const sendButtons = screen.getAllByRole('button')
    const sendBtn = sendButtons.find(btn => btn.className.includes('bg-blue-600'))
    expect(sendBtn).toBeDefined()
    await user.click(sendBtn!)

    await waitFor(() => {
      expect(mockSendChatMessage).toHaveBeenCalledWith(
        expect.objectContaining({ message: 'test question' }),
        expect.any(Function)
      )
    })
  })

  it('shows workspace selector when workspaces exist', async () => {
    const workspaces = [
      createSearchableWorkspace({ workspace_id: 'ws-1', name: 'Workspace A' }),
      createSearchableWorkspace({ workspace_id: 'ws-2', name: 'Workspace B' }),
    ]
    mockGetSearchableWorkspaces.mockResolvedValue(workspaces)

    renderChat()

    await waitFor(() => {
      expect(screen.getByText('All workspaces')).toBeInTheDocument()
    })
  })

  it('loads conversation from URL params', async () => {
    const conv = createConversation({ conversation_id: 'c1', title: 'Loaded Chat' })
    mockListConversations.mockResolvedValue([conv])
    mockGetConversation.mockResolvedValue({
      conversation: conv,
      messages: [
        { message_id: 'msg-1', role: 'user', content: 'Hello', created_at: '2024-01-01' },
        { message_id: 'msg-2', role: 'assistant', content: 'Hi there', created_at: '2024-01-01' },
      ],
    })

    renderChat('c1')

    await waitFor(() => {
      expect(mockGetConversation).toHaveBeenCalledWith('c1')
    })
  })

  it('displays status indicator text', async () => {
    renderChat()

    await waitFor(() => {
      expect(screen.getByText('Enter to send, Shift+Enter for new line')).toBeInTheDocument()
    })
  })

  it('shows advanced and standard mode toggle', async () => {
    renderChat()

    await waitFor(() => {
      expect(screen.getByText('Standard')).toBeInTheDocument()
    })
  })

  it('shows branding in sidebar', async () => {
    renderChat()

    await waitFor(() => {
      expect(screen.getByText('Outcome')).toBeInTheDocument()
      expect(screen.getByText('Ops')).toBeInTheDocument()
      expect(screen.getByText('AI Assist')).toBeInTheDocument()
    })
  })

  it('shows MCP selector when MCP servers exist', async () => {
    mockGetChatMcpServers.mockResolvedValue([
      { mcp_id: 'mcp-1', name: 'SonarQube', status: 'active', tool_count: 18, is_global: true },
    ])

    renderChat()

    await waitFor(() => {
      expect(screen.getByText('MCP (0)')).toBeInTheDocument()
    })
  })

  it('does not show MCP selector when no servers', async () => {
    mockGetChatMcpServers.mockResolvedValue([])

    renderChat()

    await waitFor(() => {
      expect(mockGetChatMcpServers).toHaveBeenCalled()
    })
    expect(screen.queryByText('MCP (0)')).not.toBeInTheDocument()
  })
})
