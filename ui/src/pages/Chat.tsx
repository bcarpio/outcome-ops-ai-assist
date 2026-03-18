import { useState, useEffect, useRef, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Send,
  Plus,
  Trash2,
  MessageSquare,
  Loader2,
  ChevronDown,
  ChevronUp,
  FileText,
  Zap,
  Search,
  LogOut,
  Folder,
  Copy,
  Check,
  Server,
  List,
} from 'lucide-react'
import {
  sendChatMessage,
  listConversations,
  getConversation,
  deleteConversation,
  getSearchableWorkspaces,
  getChatMcpServers,
  type Conversation,
  type Message,
  type Source,
  type StreamEvent,
  type SearchableWorkspace,
  type ChatMcpServer,
} from '../lib/api'
import AllChatsModal from '../components/AllChatsModal'

interface DisplayMessage extends Message {
  isStreaming?: boolean
}

type Status = 'idle' | 'thinking' | 'searching' | 'typing' | 'tool_use'

export default function Chat() {
  const { conversationId } = useParams()
  const navigate = useNavigate()

  // State
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [messages, setMessages] = useState<DisplayMessage[]>([])
  const [input, setInput] = useState('')
  const [status, setStatus] = useState<Status>('idle')
  const [currentConversationId, setCurrentConversationId] = useState<string | null>(null)
  const [currentTitle, setCurrentTitle] = useState<string>('New Chat')
  const [sources, setSources] = useState<Source[]>([])
  const [showSources, setShowSources] = useState(false)
  const [advanced, setAdvanced] = useState(false)
  const [topK, setTopK] = useState(5)
  const [showCost, setShowCost] = useState(false)
  const [lastCost, setLastCost] = useState<{ input_tokens: number; output_tokens: number; cost: number } | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [copiedMessageId, setCopiedMessageId] = useState<string | null>(null)
  const [hasMoreConversations, setHasMoreConversations] = useState(false)
  const [showAllChats, setShowAllChats] = useState(false)

  const handleCopyMessage = useCallback(async (content: string, messageId: string) => {
    try {
      // Render markdown to HTML for rich text clipboard
      const tempDiv = document.createElement('div')
      tempDiv.className = 'prose prose-sm'
      // Find the rendered message content from the DOM
      const msgEl = document.querySelector(`[data-message-id="${messageId}"]`)
      const html = msgEl ? msgEl.innerHTML : content

      const blob = new Blob([html], { type: 'text/html' })
      const textBlob = new Blob([content], { type: 'text/plain' })
      await navigator.clipboard.write([
        new ClipboardItem({
          'text/html': blob,
          'text/plain': textBlob,
        }),
      ])
      setCopiedMessageId(messageId)
      setTimeout(() => setCopiedMessageId(null), 2000)
    } catch {
      // Fallback: plain text copy
      try {
        await navigator.clipboard.writeText(content)
      } catch {
        const textarea = document.createElement('textarea')
        textarea.value = content
        document.body.appendChild(textarea)
        textarea.select()
        document.execCommand('copy')
        document.body.removeChild(textarea)
      }
      setCopiedMessageId(messageId)
      setTimeout(() => setCopiedMessageId(null), 2000)
    }
  }, [])

  // Workspace selector state
  const [searchableWorkspaces, setSearchableWorkspaces] = useState<SearchableWorkspace[]>([])
  const [selectedWorkspaceIds, setSelectedWorkspaceIds] = useState<Set<string>>(new Set())
  const [showWorkspaceDropdown, setShowWorkspaceDropdown] = useState(false)
  const [loadingWorkspaces, setLoadingWorkspaces] = useState(true)

  // MCP server selector state
  const [mcpServers, setMcpServers] = useState<ChatMcpServer[]>([])
  const [selectedMcpServerIds, setSelectedMcpServerIds] = useState<Set<string>>(new Set())
  const [showMcpDropdown, setShowMcpDropdown] = useState(false)
  const [loadingMcpServers, setLoadingMcpServers] = useState(true)
  const [activeToolCall, setActiveToolCall] = useState<string | null>(null)

  // Legacy workspace context (stored in localStorage when workspaces feature is enabled)
  const [workspaceName, setWorkspaceName] = useState<string | null>(null)

  // Load searchable workspaces on mount
  useEffect(() => {
    async function loadWorkspaces() {
      try {
        const workspaces = await getSearchableWorkspaces()
        setSearchableWorkspaces(workspaces)
        // Default: select all workspaces
        if (workspaces.length > 0) {
          setSelectedWorkspaceIds(new Set(workspaces.map(ws => ws.workspace_id)))
        }
      } catch (err) {
        console.error('Failed to load workspaces:', err)
      } finally {
        setLoadingWorkspaces(false)
      }
    }
    loadWorkspaces()
  }, [])

  // Load MCP servers on mount
  useEffect(() => {
    async function loadMcpServers() {
      try {
        const servers = await getChatMcpServers()
        setMcpServers(servers.filter(s => s.status === 'active'))
      } catch (err) {
        console.error('Failed to load MCP servers:', err)
      } finally {
        setLoadingMcpServers(false)
      }
    }
    loadMcpServers()
  }, [])

  // Check for legacy workspace context on mount
  useEffect(() => {
    const name = localStorage.getItem('selectedWorkspaceName')
    if (name) {
      setWorkspaceName(name)
    }
  }, [])

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const activeConversationRef = useRef<string | null>(null)

  // Load conversations on mount
  useEffect(() => {
    loadConversations()
  }, [])

  // Handle URL params - load conversation when URL changes
  useEffect(() => {
    if (conversationId) {
      // Skip if this is already the active conversation (e.g., URL update during streaming)
      if (activeConversationRef.current === conversationId) {
        return
      }
      // Update both state and ref
      setCurrentConversationId(conversationId)
      activeConversationRef.current = conversationId
      // Clear sources when switching conversations
      setSources([])
      // Set title from sidebar list
      const conv = conversations.find(c => c.conversation_id === conversationId)
      if (conv) {
        setCurrentTitle(conv.title)
      } else {
        setCurrentTitle('Chat')
      }
      // Load messages from server
      getConversation(conversationId).then(data => {
        if (data?.messages) {
          setMessages(data.messages.map(m => ({ ...m, isStreaming: false })))
        } else {
          setMessages([])
        }
      })
    }
  }, [conversationId, conversations])

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function loadConversations() {
    const result = await listConversations(15)
    setConversations(result.conversations)
    setHasMoreConversations(result.has_more)
  }

  // Start new chat
  function handleNewChat() {
    setCurrentConversationId(null)
    activeConversationRef.current = null
    setCurrentTitle('New Chat')
    setMessages([])
    setSources([])
    setLastCost(null)
    navigate('/')
    inputRef.current?.focus()
  }

  // Delete conversation
  async function handleDeleteConversation(convId: string, e: React.MouseEvent) {
    e.stopPropagation()
    await deleteConversation(convId)
    setConversations(prev => prev.filter(c => c.conversation_id !== convId))
    if (currentConversationId === convId) {
      handleNewChat()
    }
  }

  // Select conversation
  function handleSelectConversation(conv: Conversation) {
    // Reset ref so useEffect will load the conversation
    activeConversationRef.current = null
    navigate(`/chat/${conv.conversation_id}`)
  }

  // Send message
  async function handleSend() {
    if (!input.trim() || status !== 'idle') return

    const userMessage: DisplayMessage = {
      message_id: `temp-${Date.now()}`,
      role: 'user',
      content: input.trim(),
      created_at: new Date().toISOString(),
    }

    const assistantMessage: DisplayMessage = {
      message_id: `temp-assistant-${Date.now()}`,
      role: 'assistant',
      content: '',
      isStreaming: true,
      created_at: new Date().toISOString(),
    }

    setMessages(prev => [...prev, userMessage, assistantMessage])
    setInput('')
    setStatus('thinking')
    setSources([])

    try {
      await sendChatMessage(
        {
          conversation_id: currentConversationId || undefined,
          message: userMessage.content,
          top_k: topK,
          advanced,
          show_cost: showCost,
          workspace_ids: selectedWorkspaceIds.size > 0 ? Array.from(selectedWorkspaceIds) : undefined,
          mcp_server_ids: selectedMcpServerIds.size > 0 ? Array.from(selectedMcpServerIds) : undefined,
        },
        (event: StreamEvent) => {
          switch (event.type) {
            case 'metadata':
              // Track conversation ID in ref for use in done handler (avoids closure issues)
              activeConversationRef.current = event.conversation_id
              setCurrentConversationId(event.conversation_id)
              setCurrentTitle(event.title)
              setSources(event.sources)
              setStatus('typing')

              // Update URL if new conversation
              if (!currentConversationId) {
                navigate(`/chat/${event.conversation_id}`, { replace: true })
              }

              // Add conversation to sidebar if new
              setConversations(prev => {
                const existing = prev.findIndex(c => c.conversation_id === event.conversation_id)
                if (existing >= 0) {
                  return prev
                }
                const conv: Conversation = {
                  conversation_id: event.conversation_id,
                  title: event.title,
                  message_count: 0,
                  created_at: new Date().toISOString(),
                  updated_at: new Date().toISOString(),
                }
                return [conv, ...prev]
              })
              break

            case 'token':
              setMessages(prev => {
                const lastIndex = prev.length - 1
                const last = prev[lastIndex]
                if (last && last.role === 'assistant') {
                  // Create new array with new object to trigger React re-render
                  return [
                    ...prev.slice(0, lastIndex),
                    { ...last, content: last.content + event.text }
                  ]
                }
                return prev
              })
              break

            case 'done':
              setMessages(prev => {
                const updated = [...prev]
                const last = updated[updated.length - 1]
                if (last && last.role === 'assistant') {
                  last.message_id = event.message_id
                  last.isStreaming = false
                  last.sources = sources
                }
                return updated
              })
              // Update cost if returned
              if (event.cost) {
                setLastCost(event.cost)
              }
              setStatus('idle')
              setActiveToolCall(null)
              // Refresh conversation list from server (updates message counts, timestamps)
              loadConversations()
              break

            case 'tool_use':
              setActiveToolCall(event.tool)
              setStatus('tool_use')
              break

            case 'error':
              setMessages(prev => {
                const updated = [...prev]
                const last = updated[updated.length - 1]
                if (last && last.role === 'assistant') {
                  last.content = `Error: ${event.error}`
                  last.isStreaming = false
                }
                return updated
              })
              setStatus('idle')
              setActiveToolCall(null)
              break
          }
        }
      )
    } catch (error) {
      console.error('Chat error:', error)
      setMessages(prev => {
        const updated = [...prev]
        const last = updated[updated.length - 1]
        if (last && last.role === 'assistant') {
          last.content = `Error: ${error instanceof Error ? error.message : 'Unknown error'}`
          last.isStreaming = false
        }
        return updated
      })
      setStatus('idle')
    }

  }

  // Handle Enter key
  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  // Toggle workspace selection
  function toggleWorkspace(workspaceId: string) {
    setSelectedWorkspaceIds(prev => {
      const next = new Set(prev)
      if (next.has(workspaceId)) {
        next.delete(workspaceId)
      } else {
        next.add(workspaceId)
      }
      return next
    })
  }

  // Toggle MCP server selection
  function toggleMcpServer(mcpId: string) {
    setSelectedMcpServerIds(prev => {
      const next = new Set(prev)
      if (next.has(mcpId)) {
        next.delete(mcpId)
      } else {
        next.add(mcpId)
      }
      return next
    })
  }

  // Select all workspaces
  function selectAllWorkspaces() {
    setSelectedWorkspaceIds(new Set(searchableWorkspaces.map(ws => ws.workspace_id)))
  }

  // Get workspace selector display text
  function getWorkspaceSelectorText(): string {
    if (loadingWorkspaces) return 'Loading...'
    if (selectedWorkspaceIds.size === 0) return 'Workspaces (0)'
    if (selectedWorkspaceIds.size === searchableWorkspaces.length) return 'All workspaces'
    if (selectedWorkspaceIds.size === 1) {
      const ws = searchableWorkspaces.find(w => selectedWorkspaceIds.has(w.workspace_id))
      return ws?.name || 'Workspaces (1)'
    }
    return `${selectedWorkspaceIds.size} workspaces`
  }

  // Get MCP selector display text
  function getMcpSelectorText(): string {
    if (loadingMcpServers) return 'Loading...'
    if (selectedMcpServerIds.size === 0) return `MCP (0)`
    if (selectedMcpServerIds.size === 1) {
      const s = mcpServers.find(s => selectedMcpServerIds.has(s.mcp_id))
      return s?.name || `MCP (1)`
    }
    return `${selectedMcpServerIds.size} MCP servers`
  }

  return (
    <div className="flex h-screen bg-gray-50">
      {/* Sidebar */}
      <div className={`${sidebarOpen ? 'w-64' : 'w-0'} bg-gray-900 text-white flex flex-col transition-all duration-300 overflow-hidden`}>
        <div className="p-4">
          <button
            onClick={handleNewChat}
            className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg transition-colors"
          >
            <Plus size={18} />
            New Chat
          </button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {conversations.map(conv => (
            <div
              key={conv.conversation_id}
              onClick={() => handleSelectConversation(conv)}
              className={`flex items-center gap-2 px-4 py-3 cursor-pointer hover:bg-gray-800 group ${
                currentConversationId === conv.conversation_id ? 'bg-gray-800' : ''
              }`}
            >
              <MessageSquare size={16} className="text-gray-400 flex-shrink-0" />
              <span className="flex-1 truncate text-sm">{conv.title}</span>
              <button
                onClick={(e) => handleDeleteConversation(conv.conversation_id, e)}
                className="opacity-0 group-hover:opacity-100 p-1 hover:bg-gray-700 rounded transition-opacity"
              >
                <Trash2 size={14} className="text-gray-400" />
              </button>
            </div>
          ))}

          {hasMoreConversations && (
            <button
              onClick={() => setShowAllChats(true)}
              className="w-full flex items-center justify-center gap-2 py-2 mt-1 text-sm text-gray-400 hover:text-white hover:bg-gray-700 rounded transition-colors"
            >
              <List size={16} />
              All Chats
            </button>
          )}
        </div>

        {/* Workspace indicator (when workspaces feature is enabled) */}
        {workspaceName && (
          <div className="px-4 py-2 border-t border-gray-700">
            <button
              onClick={() => navigate('/workspaces')}
              className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-300 hover:bg-gray-800 rounded-lg transition-colors"
            >
              <Folder size={14} className="text-sky-400" />
              <span className="truncate">{workspaceName}</span>
            </button>
          </div>
        )}

        <div className="p-4 border-t border-gray-700 flex items-center justify-between">
          <span className="text-sm font-semibold tracking-tight">
            <span className="text-white">Outcome</span>
            <span className="text-sky-400">Ops</span>
            <span className="text-gray-500 font-normal ml-1">AI Assist</span>
          </span>
          <div className="flex items-center gap-1">
            <button
              onClick={() => navigate('/workspaces')}
              title="Workspaces"
              className="p-1.5 text-gray-400 hover:text-white hover:bg-gray-700 rounded transition-colors"
            >
              <Folder size={16} />
            </button>
            <button
              onClick={() => { window.location.href = '/api/logout' }}
              title="Sign out"
              className="p-1.5 text-gray-400 hover:text-white hover:bg-gray-700 rounded transition-colors"
            >
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b bg-white">
          <div className="flex items-center gap-3">
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className="p-2 hover:bg-gray-100 rounded-lg"
            >
              <MessageSquare size={20} />
            </button>
            <h1 className="font-medium">{currentTitle}</h1>
          </div>

          <div className="flex items-center gap-2">
            {/* Workspace Selector */}
            {searchableWorkspaces.length > 0 && (
              <div className="relative">
                <button
                  onClick={() => { setShowWorkspaceDropdown(!showWorkspaceDropdown); setShowMcpDropdown(false) }}
                  className="flex items-center gap-2 px-3 py-1.5 text-sm text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
                >
                  <Folder size={14} className="text-sky-500" />
                  <span>{getWorkspaceSelectorText()}</span>
                  <ChevronDown size={14} className={`transition-transform ${showWorkspaceDropdown ? 'rotate-180' : ''}`} />
                </button>

                {showWorkspaceDropdown && (
                  <>
                    <div
                      className="fixed inset-0 z-10"
                      onClick={() => setShowWorkspaceDropdown(false)}
                    />
                    <div className="absolute right-0 mt-2 w-64 bg-white rounded-lg shadow-lg border z-20 py-2 max-h-80 overflow-y-auto">
                      <div className="px-3 py-2 border-b">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-medium text-gray-500 uppercase">Search in</span>
                          <div className="flex gap-2">
                            {selectedWorkspaceIds.size > 0 && (
                              <button
                                onClick={() => setSelectedWorkspaceIds(new Set())}
                                className="text-xs text-gray-500 hover:text-gray-700"
                              >
                                Clear all
                              </button>
                            )}
                            <button
                              onClick={selectAllWorkspaces}
                              className="text-xs text-blue-600 hover:text-blue-700"
                            >
                              Select all
                            </button>
                          </div>
                        </div>
                      </div>
                      {searchableWorkspaces.map(ws => (
                        <label
                          key={ws.workspace_id}
                          className="flex items-center gap-3 px-3 py-2 hover:bg-gray-50 cursor-pointer"
                        >
                          <input
                            type="checkbox"
                            checked={selectedWorkspaceIds.has(ws.workspace_id)}
                            onChange={() => toggleWorkspace(ws.workspace_id)}
                            className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                          />
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-medium text-gray-900 truncate">{ws.name}</p>
                            <p className="text-xs text-gray-500 capitalize">{ws.access_type}</p>
                          </div>
                        </label>
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}

            {/* MCP Server Selector */}
            {mcpServers.length > 0 && (
              <div className="relative">
                <button
                  onClick={() => { setShowMcpDropdown(!showMcpDropdown); setShowWorkspaceDropdown(false) }}
                  className={`flex items-center gap-2 px-3 py-1.5 text-sm rounded-lg transition-colors ${
                    selectedMcpServerIds.size > 0
                      ? 'text-purple-700 bg-purple-50 hover:bg-purple-100 border border-purple-200'
                      : 'text-gray-700 bg-gray-100 hover:bg-gray-200'
                  }`}
                >
                  <Server size={14} className={selectedMcpServerIds.size > 0 ? 'text-purple-500' : 'text-gray-400'} />
                  <span>{getMcpSelectorText()}</span>
                  <ChevronDown size={14} className={`transition-transform ${showMcpDropdown ? 'rotate-180' : ''}`} />
                </button>

                {showMcpDropdown && (
                  <>
                    <div
                      className="fixed inset-0 z-10"
                      onClick={() => setShowMcpDropdown(false)}
                    />
                    <div className="absolute right-0 mt-2 w-72 bg-white rounded-lg shadow-lg border z-20 py-2 max-h-80 overflow-y-auto">
                      <div className="px-3 py-2 border-b">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-medium text-gray-500 uppercase">MCP Tools</span>
                          {selectedMcpServerIds.size > 0 && (
                            <button
                              onClick={() => setSelectedMcpServerIds(new Set())}
                              className="text-xs text-gray-500 hover:text-gray-700"
                            >
                              Clear all
                            </button>
                          )}
                        </div>
                      </div>
                      {mcpServers.map(server => (
                        <label
                          key={server.mcp_id}
                          className="flex items-center gap-3 px-3 py-2 hover:bg-gray-50 cursor-pointer"
                        >
                          <input
                            type="checkbox"
                            checked={selectedMcpServerIds.has(server.mcp_id)}
                            onChange={() => toggleMcpServer(server.mcp_id)}
                            className="w-4 h-4 text-purple-600 rounded focus:ring-purple-500"
                          />
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-medium text-gray-900 truncate">{server.name}</p>
                            <p className="text-xs text-gray-500">
                              {server.tool_count} tool{server.tool_count !== 1 ? 's' : ''}
                              {server.is_global && <span className="ml-1 text-purple-500">(global)</span>}
                            </p>
                          </div>
                        </label>
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4">
          {messages.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-gray-400">
              <MessageSquare size={48} className="mb-4" />
              <p className="text-lg">Ask anything about your codebase</p>
              <p className="text-sm mt-2">ADRs, documentation, code patterns, and more</p>
            </div>
          ) : (
            <div className="max-w-5xl mx-auto space-y-6">
              {messages.map((msg, i) => (
                <div
                  key={msg.message_id || i}
                  className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'} group/msg`}
                >
                  <div className={`max-w-[85%] ${msg.role === 'assistant' ? 'relative' : ''}`}>
                    <div
                      className={`rounded-lg px-4 py-3 ${
                        msg.role === 'user'
                          ? 'bg-blue-600 text-white'
                          : 'bg-white border shadow-sm'
                      }`}
                    >
                      {msg.role === 'assistant' ? (
                        <div className="prose prose-sm max-w-none" data-message-id={msg.message_id || String(i)}>
                          {msg.content ? (
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                          ) : msg.isStreaming ? (
                            <div className="flex items-center gap-2 text-gray-500">
                              <Loader2 size={16} className="animate-spin" />
                              <span>Generating response...</span>
                            </div>
                          ) : null}
                          {msg.isStreaming && msg.content && (
                            <span className="inline-block w-2 h-4 bg-blue-500 animate-pulse ml-1" />
                          )}
                        </div>
                      ) : (
                        <p className="whitespace-pre-wrap">{msg.content}</p>
                      )}
                    </div>
                    {msg.role === 'assistant' && msg.content && !msg.isStreaming && (
                      <div className="mt-1 flex justify-start opacity-0 group-hover/msg:opacity-100 transition-opacity">
                        <button
                          onClick={() => handleCopyMessage(msg.content, msg.message_id || String(i))}
                          className="flex items-center gap-1 px-2 py-1 text-xs text-gray-400 hover:text-gray-600 rounded hover:bg-gray-100 transition-colors"
                          title="Copy to clipboard"
                        >
                          {copiedMessageId === (msg.message_id || String(i)) ? (
                            <>
                              <Check size={14} className="text-green-500" />
                              <span className="text-green-500">Copied</span>
                            </>
                          ) : (
                            <>
                              <Copy size={14} />
                              <span>Copy</span>
                            </>
                          )}
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              ))}

              {/* Searching indicator */}
              {status === 'thinking' && (
                <div className="flex justify-start">
                  <div className="bg-white border shadow-sm rounded-lg px-4 py-3 flex items-center gap-2">
                    <Search size={16} className="text-blue-600 animate-pulse" />
                    <span className="text-gray-600">Searching knowledge base...</span>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        {/* Sources */}
        {sources.length > 0 && (
          <div className="border-t bg-white">
            <button
              onClick={() => setShowSources(!showSources)}
              className="w-full flex items-center justify-between px-4 py-2 text-sm text-gray-600 hover:bg-gray-50"
            >
              <span className="flex items-center gap-2">
                <FileText size={14} />
                {sources.length} source{sources.length !== 1 ? 's' : ''} found
              </span>
              {showSources ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
            </button>
            {showSources && (
              <div className="px-4 pb-3 space-y-1 max-h-48 overflow-y-auto">
                {sources.map((source, i) => (
                  <div key={i} className="text-xs text-gray-500 flex items-center gap-2 min-w-0">
                    <span className="font-medium text-gray-700 shrink-0">{source.repo}</span>
                    <span className="text-gray-400 shrink-0">|</span>
                    <span className="truncate">{source.path}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Input */}
        <div className="border-t bg-white p-4">
          <div className="max-w-5xl mx-auto flex items-end gap-2">
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => {
                setInput(e.target.value)
                // Auto-expand as user types
                e.target.style.height = 'auto'
                e.target.style.height = Math.min(e.target.scrollHeight, 300) + 'px'
              }}
              onKeyDown={handleKeyDown}
              placeholder="Ask about your codebase..."
              rows={1}
              className="flex-1 resize-y rounded-xl border border-gray-300 px-4 py-3 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              style={{ minHeight: '48px', maxHeight: '300px' }}
              disabled={status !== 'idle'}
            />
            {/* Search depth selector */}
            <div className="relative">
              <select
                value={topK}
                onChange={e => setTopK(Number(e.target.value))}
                disabled={status !== 'idle'}
                title="Search depth - number of documents to search"
                className="appearance-none px-3 py-3 pr-8 bg-gray-100 text-gray-700 border border-gray-200 rounded-xl text-sm font-medium cursor-pointer hover:bg-gray-200 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <option value={3}>3</option>
                <option value={5}>5</option>
                <option value={10}>10</option>
                <option value={20}>20</option>
                <option value={30}>30</option>
              </select>
              <Search className="absolute right-2 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
            </div>
            {/* Advanced mode toggle */}
            <button
              onClick={() => setAdvanced(!advanced)}
              disabled={status !== 'idle'}
              title={advanced ? 'Advanced mode - better quality' : 'Standard mode - faster responses'}
              className={`p-3 rounded-xl transition-colors ${
                advanced
                  ? 'bg-amber-100 text-amber-700 border border-amber-300 hover:bg-amber-200'
                  : 'bg-gray-100 text-gray-500 border border-gray-200 hover:bg-gray-200'
              } disabled:opacity-50 disabled:cursor-not-allowed`}
            >
              <Zap size={20} />
            </button>
            {/* Cost toggle */}
            <button
              onClick={() => setShowCost(!showCost)}
              disabled={status !== 'idle'}
              title={showCost ? 'Hide token usage' : 'Show token usage'}
              className={`px-3 py-3 rounded-xl text-sm font-medium transition-colors ${
                showCost
                  ? 'bg-emerald-100 text-emerald-700 border border-emerald-300 hover:bg-emerald-200'
                  : 'bg-gray-100 text-gray-500 border border-gray-200 hover:bg-gray-200'
              } disabled:opacity-50 disabled:cursor-not-allowed`}
            >
              Cost
            </button>
            {/* Send button */}
            <button
              onClick={handleSend}
              disabled={!input.trim() || status !== 'idle'}
              className="p-3 bg-blue-600 text-white rounded-xl hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {status !== 'idle' ? (
                <Loader2 size={20} className="animate-spin" />
              ) : (
                <Send size={20} />
              )}
            </button>
          </div>
          {/* Status and mode indicator */}
          <div className="max-w-5xl mx-auto mt-2 flex items-center justify-between text-xs">
            <div className="text-gray-400">
              {status === 'thinking' && 'Searching knowledge base...'}
              {status === 'typing' && 'Generating response...'}
              {status === 'tool_use' && activeToolCall && `Calling ${activeToolCall}...`}
              {status === 'idle' && 'Enter to send, Shift+Enter for new line'}
            </div>
            <div className="flex items-center gap-3">
              {showCost && lastCost && (
                <span className="text-green-600">
                  {lastCost.input_tokens.toLocaleString()} in / {lastCost.output_tokens.toLocaleString()} out = ${lastCost.cost.toFixed(4)}
                </span>
              )}
              <span className={advanced ? 'text-amber-600' : 'text-gray-500'}>
                {advanced ? 'Advanced' : 'Standard'}
              </span>
            </div>
          </div>
        </div>
      </div>

      {showAllChats && (
        <AllChatsModal
          onClose={() => setShowAllChats(false)}
          onSelect={(conv) => {
            handleSelectConversation(conv)
            setShowAllChats(false)
          }}
          onDelete={(convId) => {
            setConversations(prev => prev.filter(c => c.conversation_id !== convId))
            if (currentConversationId === convId) {
              handleNewChat()
            }
          }}
        />
      )}
    </div>
  )
}
