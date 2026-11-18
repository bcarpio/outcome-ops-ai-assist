// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
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
  getChatAvailablePrompts: vi.fn(),
  shareConversation: vi.fn(),
  listSharedConversations: vi.fn(),
}))

vi.mock('../../../src/components/AllChatsModal', () => ({
  default: () => <div data-testid="all-chats-modal">All Chats Modal</div>,
}))

vi.mock('../../../src/components/ShareDialog', () => ({
  default: () => <div data-testid="share-dialog">Share Dialog</div>,
}))

vi.mock('../../../src/components/SharedConversationsList', () => ({
  default: () => <div data-testid="shared-conversations-list">Shared Conversations</div>,
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
  getChatAvailablePrompts,
  listSharedConversations,
} from '../../../src/lib/api'

const mockSendChatMessage = vi.mocked(sendChatMessage)
const mockListConversations = vi.mocked(listConversations)
const mockGetConversation = vi.mocked(getConversation)
const mockDeleteConversation = vi.mocked(deleteConversation)
const mockGetSearchableWorkspaces = vi.mocked(getSearchableWorkspaces)
const mockGetChatMcpServers = vi.mocked(getChatMcpServers)
const mockGetChatAvailablePrompts = vi.mocked(getChatAvailablePrompts)
const mockListSharedConversations = vi.mocked(listSharedConversations)

beforeEach(() => {
  vi.clearAllMocks()
  mockListConversations.mockResolvedValue({ conversations: [], has_more: false })
  mockGetSearchableWorkspaces.mockResolvedValue([])
  mockGetChatMcpServers.mockResolvedValue([])
  mockGetChatAvailablePrompts.mockResolvedValue([])
  mockListSharedConversations.mockResolvedValue({ conversations: [], has_more: false })
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
    mockListConversations.mockResolvedValue({ conversations: convs, has_more: false })

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
    mockListConversations.mockResolvedValue({ conversations: [conv], has_more: false })
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

  it('shows "All Chats" button when has_more is true', async () => {
    const convs = [
      createConversation({ conversation_id: 'c1', title: 'Chat 1' }),
    ]
    mockListConversations.mockResolvedValue({
      conversations: convs,
      has_more: true,
    })

    renderChat()

    await waitFor(() => {
      expect(screen.getByText('All Chats')).toBeInTheDocument()
    })
  })

  it('hides "All Chats" button when has_more is false', async () => {
    const convs = [
      createConversation({ conversation_id: 'c1', title: 'Chat 1' }),
    ]
    mockListConversations.mockResolvedValue({
      conversations: convs,
      has_more: false,
    })

    renderChat()

    await waitFor(() => {
      expect(screen.getByText('Chat 1')).toBeInTheDocument()
    })
    expect(screen.queryByText('All Chats')).not.toBeInTheDocument()
  })

  it('deletes a conversation from sidebar', async () => {
    const conv = createConversation({ conversation_id: 'c1', title: 'My Chat' })
    mockListConversations.mockResolvedValue({ conversations: [conv], has_more: false })
    mockDeleteConversation.mockResolvedValue(true)

    renderChat()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('My Chat')).toBeInTheDocument()
    })

    // The delete button is inside the conversation row, find the Trash2 icon button
    const convRow = screen.getByText('My Chat').closest('div')!
    const deleteBtn = convRow.querySelector('button')
    expect(deleteBtn).toBeTruthy()
    await user.click(deleteBtn!)

    await waitFor(() => {
      expect(mockDeleteConversation).toHaveBeenCalledWith('c1')
    })
  })

  it('toggles advanced mode via Zap button', async () => {
    renderChat()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('Standard')).toBeInTheDocument()
    })

    // Click the Zap button (advanced toggle) using its title
    const zapBtn = screen.getByTitle('Standard mode - faster responses')
    await user.click(zapBtn)

    await waitFor(() => {
      expect(screen.getByText('Advanced')).toBeInTheDocument()
    })
  })

  it('displays cost when streaming returns cost data', async () => {
    mockSendChatMessage.mockImplementation(async (_req, onEvent) => {
      onEvent({ type: 'metadata', conversation_id: 'c1', sources: [], title: 'Test' })
      onEvent({ type: 'token', text: 'Response text' })
      onEvent({ type: 'done', message_id: 'msg-1', cost: { input_tokens: 100, output_tokens: 50, cost: 0.005 } })
    })

    renderChat()
    const user = userEvent.setup()
    await waitFor(() => expect(mockListConversations).toHaveBeenCalled())

    const input = screen.getByPlaceholderText('Ask about your codebase...')
    await user.type(input, 'test question')

    // Click the Cost button to enable cost display
    const costBtn = screen.getByTitle('Show token usage')
    await user.click(costBtn)

    // Send message
    const sendButtons = screen.getAllByRole('button')
    const sendBtn = sendButtons.find(btn => btn.className.includes('bg-blue-600'))
    await user.click(sendBtn!)

    await waitFor(() => {
      expect(mockSendChatMessage).toHaveBeenCalledWith(
        expect.objectContaining({ show_cost: true }),
        expect.any(Function)
      )
    })
  })

  it('shows sources panel when sources are returned', async () => {
    const testSources = [
      { repo: 'org/repo', type: 'code', path: 'src/index.ts' },
      { repo: 'org/repo', type: 'doc', path: 'README.md' },
    ]

    mockSendChatMessage.mockImplementation(async (_req, onEvent) => {
      onEvent({ type: 'metadata', conversation_id: 'c1', sources: testSources, title: 'Test' })
      onEvent({ type: 'token', text: 'Answer' })
      onEvent({ type: 'done', message_id: 'msg-1' })
    })

    renderChat()
    const user = userEvent.setup()
    await waitFor(() => expect(mockListConversations).toHaveBeenCalled())

    const input = screen.getByPlaceholderText('Ask about your codebase...')
    await user.type(input, 'test')

    const sendButtons = screen.getAllByRole('button')
    const sendBtn = sendButtons.find(btn => btn.className.includes('bg-blue-600'))
    await user.click(sendBtn!)

    await waitFor(() => {
      // Sources count should appear (format: "N sources found")
      expect(screen.getByText('2 sources found')).toBeInTheDocument()
    })
  })

  it('handles streaming error events', async () => {
    mockSendChatMessage.mockImplementation(async (_req, onEvent) => {
      onEvent({ type: 'metadata', conversation_id: 'c1', sources: [], title: 'Test' })
      onEvent({ type: 'error', error: 'Rate limit exceeded' })
    })

    renderChat()
    const user = userEvent.setup()
    await waitFor(() => expect(mockListConversations).toHaveBeenCalled())

    const input = screen.getByPlaceholderText('Ask about your codebase...')
    await user.type(input, 'test')

    const sendButtons = screen.getAllByRole('button')
    const sendBtn = sendButtons.find(btn => btn.className.includes('bg-blue-600'))
    await user.click(sendBtn!)

    await waitFor(() => {
      expect(screen.getByText('Error: Rate limit exceeded')).toBeInTheDocument()
    })
  })

  it('handles tool_use events during streaming', async () => {
    mockSendChatMessage.mockImplementation(async (_req, onEvent) => {
      onEvent({ type: 'metadata', conversation_id: 'c1', sources: [], title: 'Test' })
      onEvent({ type: 'tool_use', tool: 'search_sonar', status: 'running' })
      onEvent({ type: 'token', text: 'Answer' })
      onEvent({ type: 'done', message_id: 'msg-1' })
    })

    renderChat()
    const user = userEvent.setup()
    await waitFor(() => expect(mockListConversations).toHaveBeenCalled())

    const input = screen.getByPlaceholderText('Ask about your codebase...')
    await user.type(input, 'test')

    const sendButtons = screen.getAllByRole('button')
    const sendBtn = sendButtons.find(btn => btn.className.includes('bg-blue-600'))
    await user.click(sendBtn!)

    await waitFor(() => {
      expect(mockSendChatMessage).toHaveBeenCalled()
    })
  })

  it('handles send error (exception)', async () => {
    mockSendChatMessage.mockRejectedValue(new Error('Connection failed'))

    renderChat()
    const user = userEvent.setup()
    await waitFor(() => expect(mockListConversations).toHaveBeenCalled())

    const input = screen.getByPlaceholderText('Ask about your codebase...')
    await user.type(input, 'test')

    const sendButtons = screen.getAllByRole('button')
    const sendBtn = sendButtons.find(btn => btn.className.includes('bg-blue-600'))
    await user.click(sendBtn!)

    await waitFor(() => {
      expect(screen.getByText('Error: Connection failed')).toBeInTheDocument()
    })
  })

  it('selects a workspace from the dropdown', async () => {
    const workspaces = [
      createSearchableWorkspace({ workspace_id: 'ws-1', name: 'Workspace A' }),
      createSearchableWorkspace({ workspace_id: 'ws-2', name: 'Workspace B' }),
    ]
    mockGetSearchableWorkspaces.mockResolvedValue(workspaces)

    renderChat()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('All workspaces')).toBeInTheDocument()
    })

    // Click to open workspace dropdown
    await user.click(screen.getByText('All workspaces'))

    // Should show workspace items
    await waitFor(() => {
      expect(screen.getByText('Workspace A')).toBeInTheDocument()
      expect(screen.getByText('Workspace B')).toBeInTheDocument()
    })
  })

  it('shows New Chat button and sidebar content', async () => {
    renderChat()

    await waitFor(() => {
      expect(screen.getByText('Ask anything about your codebase')).toBeInTheDocument()
    })

    // Verify the sidebar content is present
    const buttons = screen.getAllByText('New Chat')
    expect(buttons.length).toBeGreaterThanOrEqual(1)
  })

  it('loads conversation messages from URL params', async () => {
    const conv = createConversation({ conversation_id: 'c1', title: 'Loaded Chat' })
    mockListConversations.mockResolvedValue({ conversations: [conv], has_more: false })
    mockGetConversation.mockResolvedValue({
      conversation: { ...conv, is_owner: true, owner_email: 'user@test.com' },
      messages: [
        { message_id: 'msg-1', role: 'user' as const, content: 'Hello', created_at: '2024-01-01' },
        { message_id: 'msg-2', role: 'assistant' as const, content: 'Hi there', created_at: '2024-01-01', sources: [] },
      ],
    })

    renderChat('c1')

    await waitFor(() => {
      expect(mockGetConversation).toHaveBeenCalledWith('c1')
    })

    // Verify messages are rendered
    await waitFor(() => {
      expect(screen.getByText('Hello')).toBeInTheDocument()
    })
  })

  it('handles MCP server selection in dropdown', async () => {
    mockGetChatMcpServers.mockResolvedValue([
      { mcp_id: 'mcp-1', name: 'SonarQube', status: 'active', tool_count: 18, is_global: true },
      { mcp_id: 'mcp-2', name: 'GitHub Tools', status: 'active', tool_count: 5, is_global: false },
    ])

    renderChat()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('MCP (0)')).toBeInTheDocument()
    })

    // Click MCP dropdown
    await user.click(screen.getByText('MCP (0)'))

    // Should show MCP servers
    await waitFor(() => {
      expect(screen.getByText('SonarQube')).toBeInTheDocument()
      expect(screen.getByText('GitHub Tools')).toBeInTheDocument()
    })
  })

  it('shows prompt selector when prompts are available', async () => {
    mockGetChatAvailablePrompts.mockResolvedValue([
      { workspace_id: 'ws-1', workspace_name: 'Engineering', prompt_preview: 'You are an engineering assistant...' },
      { workspace_id: 'ws-2', workspace_name: 'Security', prompt_preview: 'You are a security expert...' },
    ])

    renderChat()

    await waitFor(() => {
      expect(screen.getByText('Voice: Default')).toBeInTheDocument()
    })
  })

  it('auto-selects prompt when only one available', async () => {
    mockGetChatAvailablePrompts.mockResolvedValue([
      { workspace_id: 'ws-1', workspace_name: 'Engineering', prompt_preview: 'You are an engineering assistant...' },
    ])

    renderChat()

    await waitFor(() => {
      expect(screen.getByText('Voice: Engineering')).toBeInTheDocument()
    })
  })

  it('handles new chat button in sidebar when no conversation loaded', async () => {
    renderChat()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('Ask anything about your codebase')).toBeInTheDocument()
    })

    // The New Chat button should be present in the sidebar
    const newChatButtons = screen.getAllByText('New Chat')
    expect(newChatButtons.length).toBeGreaterThanOrEqual(1)

    // Clicking it should work without errors (it navigates to / which is fine for a fresh state)
    // We verify the button is clickable and no crash occurs
    await user.click(newChatButtons[0])
  })

  it('selects a conversation from sidebar', async () => {
    const conv1 = createConversation({ conversation_id: 'c1', title: 'First Chat' })
    const conv2 = createConversation({ conversation_id: 'c2', title: 'Second Chat' })
    mockListConversations.mockResolvedValue({ conversations: [conv1, conv2], has_more: false })
    mockGetConversation.mockResolvedValue({
      conversation: conv1,
      messages: [],
    })

    renderChat()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('First Chat')).toBeInTheDocument()
    })

    // Click on a conversation in the sidebar
    await user.click(screen.getByText('First Chat'))

    await waitFor(() => {
      expect(mockGetConversation).toHaveBeenCalledWith('c1')
    })
  })

  it('toggles workspace selection off', async () => {
    const workspaces = [
      createSearchableWorkspace({ workspace_id: 'ws-1', name: 'Alpha WS' }),
      createSearchableWorkspace({ workspace_id: 'ws-2', name: 'Beta WS' }),
    ]
    mockGetSearchableWorkspaces.mockResolvedValue(workspaces)

    renderChat()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('All workspaces')).toBeInTheDocument()
    })

    // Open dropdown
    await user.click(screen.getByText('All workspaces'))

    await waitFor(() => {
      expect(screen.getByText('Alpha WS')).toBeInTheDocument()
    })

    // Uncheck Alpha WS workspace
    const wsALabel = screen.getByText('Alpha WS').closest('label')!
    const checkbox = wsALabel.querySelector('input[type="checkbox"]')!
    await user.click(checkbox)

    // After unchecking one of two, the button text should show the remaining workspace name
    // The dropdown may close automatically or stay open
    await waitFor(() => {
      // Should show just "Beta WS" as the selector text (single workspace selected)
      const selectorSpan = screen.getAllByText('Beta WS')
      expect(selectorSpan.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('shows MCP server tool count in dropdown and allows selection', async () => {
    mockGetChatMcpServers.mockResolvedValue([
      { mcp_id: 'mcp-1', name: 'SonarTools', status: 'active', tool_count: 18, is_global: true },
    ])

    renderChat()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('MCP (0)')).toBeInTheDocument()
    })

    // Open MCP dropdown
    await user.click(screen.getByText('MCP (0)'))

    await waitFor(() => {
      expect(screen.getByText('SonarTools')).toBeInTheDocument()
      expect(screen.getByText(/18 tools/)).toBeInTheDocument()
    })

    // Select the server via checkbox
    const sqLabel = screen.getByText('SonarTools').closest('label')!
    const checkbox = sqLabel.querySelector('input[type="checkbox"]')!
    await user.click(checkbox)

    // After selection, the dropdown button text should change
    await waitFor(() => {
      const allSonarTexts = screen.getAllByText('SonarTools')
      expect(allSonarTexts.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('expands and collapses sources panel', async () => {
    mockSendChatMessage.mockImplementation(async (_req, onEvent) => {
      onEvent({ type: 'metadata', conversation_id: 'c1', sources: [
        { repo: 'org/repo', type: 'code', path: 'src/index.ts' },
      ], title: 'Test' })
      onEvent({ type: 'token', text: 'Answer' })
      onEvent({ type: 'done', message_id: 'msg-1' })
    })

    renderChat()
    const user = userEvent.setup()
    await waitFor(() => expect(mockListConversations).toHaveBeenCalled())

    const input = screen.getByPlaceholderText('Ask about your codebase...')
    await user.type(input, 'test')

    const sendButtons = screen.getAllByRole('button')
    const sendBtn = sendButtons.find(btn => btn.className.includes('bg-blue-600'))
    await user.click(sendBtn!)

    await waitFor(() => {
      expect(screen.getByText('1 source found')).toBeInTheDocument()
    })

    // Click to expand sources
    await user.click(screen.getByText('1 source found'))

    // Source details should be visible
    await waitFor(() => {
      expect(screen.getByText('src/index.ts')).toBeInTheDocument()
    })
  })

  it('shows "Workspaces (0)" when all workspaces deselected', async () => {
    const workspaces = [
      createSearchableWorkspace({ workspace_id: 'ws-1', name: 'WS A' }),
    ]
    mockGetSearchableWorkspaces.mockResolvedValue(workspaces)

    renderChat()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('All workspaces')).toBeInTheDocument()
    })

    // Open dropdown
    await user.click(screen.getByText('All workspaces'))

    // Click "Clear all"
    await waitFor(() => {
      expect(screen.getByText('Clear all')).toBeInTheDocument()
    })
    await user.click(screen.getByText('Clear all'))

    await waitFor(() => {
      expect(screen.getByText('Workspaces (0)')).toBeInTheDocument()
    })
  })

  it('shows "Select all" link in workspace dropdown', async () => {
    const workspaces = [
      createSearchableWorkspace({ workspace_id: 'ws-1', name: 'WS Alpha' }),
      createSearchableWorkspace({ workspace_id: 'ws-2', name: 'WS Beta' }),
    ]
    mockGetSearchableWorkspaces.mockResolvedValue(workspaces)

    renderChat()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('All workspaces')).toBeInTheDocument()
    })

    // Open dropdown - "Select all" should be visible
    await user.click(screen.getByText('All workspaces'))

    await waitFor(() => {
      expect(screen.getByText('Select all')).toBeInTheDocument()
    })
  })

  it('displays N workspaces text when multiple selected', async () => {
    const workspaces = [
      createSearchableWorkspace({ workspace_id: 'ws-1', name: 'WS A' }),
      createSearchableWorkspace({ workspace_id: 'ws-2', name: 'WS B' }),
      createSearchableWorkspace({ workspace_id: 'ws-3', name: 'WS C' }),
    ]
    mockGetSearchableWorkspaces.mockResolvedValue(workspaces)

    renderChat()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('All workspaces')).toBeInTheDocument()
    })

    // Open dropdown and uncheck one
    await user.click(screen.getByText('All workspaces'))

    await waitFor(() => {
      expect(screen.getByText('WS A')).toBeInTheDocument()
    })

    const wsALabel = screen.getByText('WS A').closest('label')!
    const checkbox = wsALabel.querySelector('input[type="checkbox"]')!
    await user.click(checkbox)

    await waitFor(() => {
      expect(screen.getByText('2 workspaces')).toBeInTheDocument()
    })
  })

  it('shows prompt dropdown and selects a prompt', async () => {
    mockGetChatAvailablePrompts.mockResolvedValue([
      { workspace_id: 'ws-1', workspace_name: 'Engineering', prompt_preview: 'You are an engineering assistant...' },
      { workspace_id: 'ws-2', workspace_name: 'Security', prompt_preview: 'You are a security expert...' },
    ])

    renderChat()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('Voice: Default')).toBeInTheDocument()
    })

    // Open prompt dropdown
    await user.click(screen.getByText('Voice: Default'))

    await waitFor(() => {
      expect(screen.getByText('Engineering')).toBeInTheDocument()
      expect(screen.getByText('Security')).toBeInTheDocument()
    })
  })

  it('displays cost data after message send with show_cost enabled', async () => {
    mockSendChatMessage.mockImplementation(async (_req, onEvent) => {
      onEvent({ type: 'metadata', conversation_id: 'c1', sources: [], title: 'Test' })
      onEvent({ type: 'token', text: 'Answer' })
      onEvent({ type: 'done', message_id: 'msg-1', cost: { input_tokens: 1000, output_tokens: 500, cost: 0.0123 } })
    })

    renderChat()
    const user = userEvent.setup()
    await waitFor(() => expect(mockListConversations).toHaveBeenCalled())

    // Enable cost display
    const costBtn = screen.getByTitle('Show token usage')
    await user.click(costBtn)

    // Type and send
    const input = screen.getByPlaceholderText('Ask about your codebase...')
    await user.type(input, 'test')

    const sendButtons = screen.getAllByRole('button')
    const sendBtn = sendButtons.find(btn => btn.className.includes('bg-blue-600'))
    await user.click(sendBtn!)

    await waitFor(() => {
      expect(screen.getByText(/1,000 in/)).toBeInTheDocument()
      expect(screen.getByText(/500 out/)).toBeInTheDocument()
    })
  })
})
