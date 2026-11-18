// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createMockFetchResponse, createMockStreamResponse } from '../../helpers/mocks'

// We need to mock import.meta.env before importing
const mockFetch = vi.fn()
vi.stubGlobal('fetch', mockFetch)

import {
  sendChatMessage,
  listConversations,
  getConversation,
  deleteConversation,
  listWorkspaces,
  createWorkspace,
  getWorkspace,
  updateWorkspace,
  deleteWorkspace,
  listMembers,
  addMember,
  updateMember,
  removeMember,
  listWorkspaceRepos,
  listAvailableRepos,
  addWorkspaceRepo,
  removeWorkspaceRepo,
  getSearchableWorkspaces,
  getChatAvailablePrompts,
  listMcpServers,
  createMcpServer,
  getMcpServer,
  updateMcpServer,
  deleteMcpServer,
  refreshMcpTools,
  getMcpServerWorkspaces,
  assignMcpToWorkspace,
  removeMcpFromWorkspace,
  listWorkspaceMcpServers,
  getChatMcpServers,
  getCurrentUser,
  createApiKey,
  listApiKeys,
  revokeApiKey,
  getWorkspaceSystemPrompt,
  updateWorkspaceSystemPrompt,
  resetWorkspaceSystemPrompt,
  shareConversation,
  listSharedConversations,
  searchUsers,
  listOrgMembers,
  addOrgMember,
  updateOrgMember,
  removeOrgMember,
  getSearchAccess,
  updateSearchAccess,
  listSharingOptions,
  getDocumentUploadUrl,
  listDocuments,
  deleteDocument,
  getConfluenceAuthUrl,
  listConfluenceConnections,
  listConfluenceSpaces,
  updateConfluenceSpaces,
  deleteConfluenceConnection,
  triggerConfluenceSync,
  getJiraAuthUrl,
  listJiraConnections,
  listJiraProjects,
  updateJiraProjects,
  deleteJiraConnection,
  triggerJiraSync,
  getOutlookAuthUrl,
  listOutlookConnections,
  listOutlookFolders,
  updateOutlookFolders,
  deleteOutlookConnection,
  triggerOutlookSync,
  getTeamsAuthUrl,
  listTeamsConnections,
  listTeamsTeams,
  listTeamsChannels,
  saveTeamsConfig,
  triggerTeamsSync,
  deleteTeamsConnection,
  getSharePointAuthUrl,
  listSharePointConnections,
  listSharePointSites,
  updateSharePointSites,
  triggerSharePointSync,
  deleteSharePointConnection,
  getGitHubInstallUrl,
  listGitHubInstallations,
  listGitHubInstallationRepos,
  listGitHubRepos,
  addGitHubRepos,
  updateGitHubRepo,
  removeGitHubRepo,
  triggerGitHubSync,
  type StreamEvent,
} from '../../../src/lib/api'

beforeEach(() => {
  vi.clearAllMocks()
})

// ============================================================================
// sendChatMessage (streaming)
// ============================================================================
describe('sendChatMessage', () => {
  it('streams events from NDJSON response', async () => {
    const events: StreamEvent[] = []
    const streamEvents = [
      { type: 'metadata', conversation_id: 'c1', sources: [], title: 'Test' },
      { type: 'token', text: 'Hello ' },
      { type: 'token', text: 'World' },
      { type: 'done', message_id: 'msg-1' },
    ]
    const response = createMockStreamResponse(streamEvents)
    mockFetch.mockResolvedValue(response)

    await sendChatMessage({ message: 'test' }, (e) => events.push(e))

    expect(mockFetch).toHaveBeenCalledWith('/api/chat', expect.objectContaining({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }))
    expect(events).toHaveLength(4)
    expect(events[0]).toEqual(expect.objectContaining({ type: 'metadata' }))
    expect(events[3]).toEqual(expect.objectContaining({ type: 'done' }))
  })

  it('throws on non-ok response', async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 500 })

    await expect(
      sendChatMessage({ message: 'test' }, vi.fn())
    ).rejects.toThrow('HTTP error: 500')
  })

  it('throws when response body is missing', async () => {
    mockFetch.mockResolvedValue({ ok: true, body: null })

    await expect(
      sendChatMessage({ message: 'test' }, vi.fn())
    ).rejects.toThrow('No response body')
  })
})

// ============================================================================
// listConversations
// ============================================================================
describe('listConversations', () => {
  it('returns conversations with default limit', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({
      conversations: [
        { conversation_id: 'c1', title: 'Chat 1', message_count: 5, created_at: '2024-01-01', updated_at: '2024-01-01' },
      ],
      has_more: false,
    }))

    const result = await listConversations()

    expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining('/api/conversations?limit=20'))
    expect(result.conversations).toHaveLength(1)
    expect(result.conversations[0].title).toBe('Chat 1')
    expect(result.has_more).toBe(false)
  })

  it('passes cursor parameter when provided', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({
      conversations: [],
      has_more: false,
    }))

    await listConversations(10, 'cursor-abc')

    expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining('cursor=cursor-abc'))
  })

  it('propagates fetch errors', async () => {
    mockFetch.mockRejectedValue(new Error('Network error'))

    await expect(listConversations()).rejects.toThrow('Network error')
  })

  it('throws on non-ok response', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}, false, 500))

    await expect(listConversations()).rejects.toThrow(/500/)
  })

  it('handles missing title gracefully', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({
      conversations: [
        { conversation_id: 'c1', message_count: 0, created_at: '2024-01-01', updated_at: '2024-01-01' },
      ],
      has_more: false,
    }))

    const result = await listConversations()
    expect(result.conversations[0].title).toBe('Untitled')
  })
})

// ============================================================================
// getConversation
// ============================================================================
describe('getConversation', () => {
  it('returns conversation with messages', async () => {
    const data = {
      conversation: { conversation_id: 'c1', title: 'Test' },
      messages: [{ message_id: 'msg-1', role: 'user', content: 'Hello' }],
    }
    mockFetch.mockResolvedValue(createMockFetchResponse(data))

    const result = await getConversation('c1')

    expect(mockFetch).toHaveBeenCalledWith('/api/conversations/c1')
    expect(result).toEqual(data)
  })

  it('throws on non-ok response', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}, false, 404))

    await expect(getConversation('c1')).rejects.toThrow(/404/)
  })

  it('propagates fetch errors', async () => {
    mockFetch.mockRejectedValue(new Error('Network error'))

    await expect(getConversation('c1')).rejects.toThrow('Network error')
  })
})

// ============================================================================
// deleteConversation
// ============================================================================
describe('deleteConversation', () => {
  it('resolves on success', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}, true, 200))

    await expect(deleteConversation('c1')).resolves.toBeUndefined()

    expect(mockFetch).toHaveBeenCalledWith('/api/conversations/c1', { method: 'DELETE' })
  })

  it('throws on non-ok response', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}, false, 404))

    await expect(deleteConversation('c1')).rejects.toThrow(/404/)
  })

  it('propagates fetch errors', async () => {
    mockFetch.mockRejectedValue(new Error('Network error'))

    await expect(deleteConversation('c1')).rejects.toThrow('Network error')
  })
})

// ============================================================================
// Workspace CRUD
// ============================================================================
describe('listWorkspaces', () => {
  it('returns workspaces array', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ workspaces: [{ workspace_id: 'ws-1', name: 'Test' }] }))

    const result = await listWorkspaces()
    expect(result).toHaveLength(1)
    expect(result[0].name).toBe('Test')
  })

  it('throws on non-ok response', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ error: 'Unauthorized' }, false, 401))

    await expect(listWorkspaces()).rejects.toThrow('Unauthorized')
  })
})

describe('createWorkspace', () => {
  it('creates workspace with name and description', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ workspace: { workspace_id: 'ws-new', name: 'New WS' } }))

    const result = await createWorkspace('New WS', 'Description')

    expect(mockFetch).toHaveBeenCalledWith('/api/workspaces', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ name: 'New WS', description: 'Description' }),
    }))
    expect(result.name).toBe('New WS')
  })

  it('throws on non-ok response', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ error: 'Name taken' }, false, 409))

    await expect(createWorkspace('Taken')).rejects.toThrow('Name taken')
  })

  it('throws on empty workspace in response', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(createWorkspace('Test')).rejects.toThrow('Invalid response from server')
  })
})

describe('getWorkspace', () => {
  it('returns workspace by id', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ workspace: { workspace_id: 'ws-1', name: 'WS' } }))

    const result = await getWorkspace('ws-1')
    expect(result.workspace_id).toBe('ws-1')
  })

  it('throws on error', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}, false, 404))

    await expect(getWorkspace('missing')).rejects.toThrow()
  })
})

describe('updateWorkspace', () => {
  it('updates workspace name', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ workspace: { workspace_id: 'ws-1', name: 'Updated' } }))

    const result = await updateWorkspace('ws-1', { name: 'Updated' })
    expect(result.name).toBe('Updated')
  })
})

describe('deleteWorkspace', () => {
  it('deletes workspace', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(deleteWorkspace('ws-1')).resolves.toBeUndefined()
    expect(mockFetch).toHaveBeenCalledWith('/api/workspaces/ws-1', { method: 'DELETE' })
  })

  it('throws on error', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ error: 'Forbidden' }, false, 403))

    await expect(deleteWorkspace('ws-1')).rejects.toThrow('Forbidden')
  })
})

// ============================================================================
// Members
// ============================================================================
describe('listMembers', () => {
  it('returns members array', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ members: [{ user_email: 'a@b.com', role: 'member' }] }))

    const result = await listMembers('ws-1')
    expect(result).toHaveLength(1)
  })
})

describe('addMember', () => {
  it('adds member with default role', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ member: { user_email: 'a@b.com', role: 'member' } }))

    const result = await addMember('ws-1', 'a@b.com')
    expect(result.user_email).toBe('a@b.com')
    expect(mockFetch).toHaveBeenCalledWith('/api/workspaces/ws-1/members', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ email: 'a@b.com', role: 'member' }),
    }))
  })
})

describe('updateMember', () => {
  it('updates member role', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(updateMember('ws-1', 'a@b.com', 'workspace_admin')).resolves.toBeUndefined()
  })
})

describe('removeMember', () => {
  it('removes member', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(removeMember('ws-1', 'a@b.com')).resolves.toBeUndefined()
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/members/a%40b.com',
      { method: 'DELETE' }
    )
  })
})

// ============================================================================
// Workspace repos
// ============================================================================
describe('listWorkspaceRepos', () => {
  it('returns repos', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ repos: [{ repo_name: 'org/repo' }] }))

    const result = await listWorkspaceRepos('ws-1')
    expect(result).toHaveLength(1)
  })
})

describe('listAvailableRepos', () => {
  it('returns available repos', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ repos: [{ full_name: 'org/repo' }] }))

    const result = await listAvailableRepos('ws-1')
    expect(result).toHaveLength(1)
  })
})

describe('addWorkspaceRepo', () => {
  it('adds repo', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ repo: { repo_name: 'org/repo' } }))

    const result = await addWorkspaceRepo('ws-1', 'org/repo')
    expect(result.repo_name).toBe('org/repo')
  })
})

describe('removeWorkspaceRepo', () => {
  it('removes repo', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(removeWorkspaceRepo('ws-1', 'org/repo')).resolves.toBeUndefined()
  })
})

// ============================================================================
// Chat-related
// ============================================================================
describe('getSearchableWorkspaces', () => {
  it('returns workspaces', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ workspaces: [{ workspace_id: 'ws-1', name: 'WS', access_type: 'member' }] }))

    const result = await getSearchableWorkspaces()
    expect(result).toHaveLength(1)
    expect(mockFetch).toHaveBeenCalledWith('/api/chat/searchable-workspaces')
  })
})

describe('getChatAvailablePrompts', () => {
  it('returns prompts', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ prompts: [{ workspace_id: 'ws-1', workspace_name: 'WS', prompt_preview: 'You are...' }] }))

    const result = await getChatAvailablePrompts()
    expect(result).toHaveLength(1)
  })
})

// ============================================================================
// MCP Server Management
// ============================================================================
describe('listMcpServers', () => {
  it('returns servers', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ mcp_servers: [{ mcp_id: 'mcp-1' }] }))

    const result = await listMcpServers()
    expect(result).toHaveLength(1)
  })

  it('throws on error', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ error: 'Unauthorized' }, false, 401))

    await expect(listMcpServers()).rejects.toThrow('Unauthorized')
  })
})

describe('createMcpServer', () => {
  it('creates server', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ mcp_server: { mcp_id: 'mcp-new', name: 'New' } }))

    const result = await createMcpServer({ name: 'New', url: 'https://mcp.test', auth_type: 'none', is_global: true, refresh_interval_days: 7 })
    expect(result.mcp_id).toBe('mcp-new')
  })
})

describe('getMcpServer', () => {
  it('returns server', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ mcp_server: { mcp_id: 'mcp-1' } }))

    const result = await getMcpServer('mcp-1')
    expect(result.mcp_id).toBe('mcp-1')
  })
})

describe('updateMcpServer', () => {
  it('updates server', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ mcp_server: { mcp_id: 'mcp-1', name: 'Updated' } }))

    const result = await updateMcpServer('mcp-1', { name: 'Updated' })
    expect(result.name).toBe('Updated')
  })
})

describe('deleteMcpServer', () => {
  it('deletes server', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(deleteMcpServer('mcp-1')).resolves.toBeUndefined()
  })

  it('throws on error', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ error: 'Not found' }, false, 404))

    await expect(deleteMcpServer('mcp-1')).rejects.toThrow('Not found')
  })
})

describe('refreshMcpTools', () => {
  it('refreshes and returns updated server', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ mcp_server: { mcp_id: 'mcp-1', tool_count: 20 } }))

    const result = await refreshMcpTools('mcp-1')
    expect(result.tool_count).toBe(20)
    expect(mockFetch).toHaveBeenCalledWith('/api/mcp/servers/mcp-1/refresh', { method: 'POST' })
  })
})

describe('getMcpServerWorkspaces', () => {
  it('returns workspace ids', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ workspace_ids: ['ws-1', 'ws-2'] }))

    const result = await getMcpServerWorkspaces('mcp-1')
    expect(result).toEqual(['ws-1', 'ws-2'])
  })
})

describe('assignMcpToWorkspace', () => {
  it('assigns mcp to workspace', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(assignMcpToWorkspace('ws-1', 'mcp-1')).resolves.toBeUndefined()
    expect(mockFetch).toHaveBeenCalledWith('/api/workspaces/ws-1/mcp-servers', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ mcp_id: 'mcp-1' }),
    }))
  })
})

describe('removeMcpFromWorkspace', () => {
  it('removes mcp from workspace', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(removeMcpFromWorkspace('ws-1', 'mcp-1')).resolves.toBeUndefined()
    expect(mockFetch).toHaveBeenCalledWith('/api/workspaces/ws-1/mcp-servers/mcp-1', { method: 'DELETE' })
  })
})

describe('listWorkspaceMcpServers', () => {
  it('returns workspace mcp servers', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ mcp_servers: [{ mcp_id: 'mcp-1' }] }))

    const result = await listWorkspaceMcpServers('ws-1')
    expect(result).toHaveLength(1)
  })
})

describe('getChatMcpServers', () => {
  it('returns chat mcp servers', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ mcp_servers: [{ mcp_id: 'mcp-1', name: 'SQ' }] }))

    const result = await getChatMcpServers()
    expect(result).toHaveLength(1)
    expect(mockFetch).toHaveBeenCalledWith('/api/chat/mcp-servers')
  })
})

// ============================================================================
// User Profile
// ============================================================================
describe('getCurrentUser', () => {
  it('returns user profile', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ user: { email: 'user@test.com' }, claims: { name: 'Test User' } }))

    const result = await getCurrentUser()
    expect(result.email).toBe('user@test.com')
    expect(result.name).toBe('Test User')
  })

  it('throws on error', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}, false, 401))

    await expect(getCurrentUser()).rejects.toThrow()
  })
})

// ============================================================================
// API Keys
// ============================================================================
describe('createApiKey', () => {
  it('creates api key', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ key_id: 'k1', key: 'oo-key-123' }))

    const result = await createApiKey({ name: 'Test Key' })
    expect(result.key_id).toBe('k1')
  })
})

describe('listApiKeys', () => {
  it('returns api keys', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ api_keys: [{ key_id: 'k1' }] }))

    const result = await listApiKeys()
    expect(result).toHaveLength(1)
  })
})

describe('revokeApiKey', () => {
  it('revokes api key', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(revokeApiKey('k1')).resolves.toBeUndefined()
    expect(mockFetch).toHaveBeenCalledWith('/api/api-keys/k1', { method: 'DELETE' })
  })
})

// ============================================================================
// System Prompt
// ============================================================================
describe('getWorkspaceSystemPrompt', () => {
  it('returns system prompt', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ custom_system_prompt: 'You are helpful', updated_by: 'a@b.com', updated_at: '2024-01-01' }))

    const result = await getWorkspaceSystemPrompt('ws-1')
    expect(result.custom_system_prompt).toBe('You are helpful')
  })
})

describe('updateWorkspaceSystemPrompt', () => {
  it('updates system prompt', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ custom_system_prompt: 'New prompt', updated_by: 'a@b.com', updated_at: '2024-01-01' }))

    const result = await updateWorkspaceSystemPrompt('ws-1', 'New prompt')
    expect(result.custom_system_prompt).toBe('New prompt')
  })
})

describe('resetWorkspaceSystemPrompt', () => {
  it('resets system prompt', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ custom_system_prompt: null, updated_by: null, updated_at: null }))

    const result = await resetWorkspaceSystemPrompt('ws-1')
    expect(result.custom_system_prompt).toBeNull()
  })
})

// ============================================================================
// Sharing
// ============================================================================
describe('shareConversation', () => {
  it('shares conversation with workspaces', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ shared_workspace_ids: ['ws-1'], share_url: 'https://example.com/share/c1' }))

    const result = await shareConversation('c1', ['ws-1'])
    expect(result.shared_workspace_ids).toEqual(['ws-1'])
    expect(mockFetch).toHaveBeenCalledWith('/api/conversations/c1/share', expect.objectContaining({
      method: 'PUT',
    }))
  })
})

describe('listSharedConversations', () => {
  it('returns shared conversations', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ conversations: [], has_more: false }))

    const result = await listSharedConversations()
    expect(result.conversations).toEqual([])
  })

  it('passes workspace_id and cursor params', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ conversations: [], has_more: false }))

    await listSharedConversations('ws-1', 10, 'cursor-123')

    expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining('workspace_id=ws-1'))
    expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining('cursor=cursor-123'))
  })
})

// ============================================================================
// Search users
// ============================================================================
describe('searchUsers', () => {
  it('returns matching users', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ users: [{ user_email: 'a@b.com', name: 'Alice', is_service_account: false }] }))

    const result = await searchUsers('alice')
    expect(result).toHaveLength(1)
    expect(mockFetch).toHaveBeenCalledWith('/api/users/search?q=alice')
  })
})

// ============================================================================
// Org Management
// ============================================================================
describe('listOrgMembers', () => {
  it('returns org members', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ members: [{ user_email: 'a@b.com', org_role: 'member' }] }))

    const result = await listOrgMembers()
    expect(result).toHaveLength(1)
  })
})

describe('addOrgMember', () => {
  it('adds org member', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ member: { user_email: 'a@b.com', org_role: 'member' } }))

    const result = await addOrgMember('a@b.com')
    expect(result.user_email).toBe('a@b.com')
  })
})

describe('updateOrgMember', () => {
  it('updates org member role', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(updateOrgMember('a@b.com', 'org_admin')).resolves.toBeUndefined()
  })
})

describe('removeOrgMember', () => {
  it('removes org member', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(removeOrgMember('a@b.com')).resolves.toBeUndefined()
    expect(mockFetch).toHaveBeenCalledWith('/api/org/members/a%40b.com', { method: 'DELETE' })
  })
})

// ============================================================================
// Workspace Sharing
// ============================================================================
describe('getSearchAccess', () => {
  it('returns search access list', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ search_allowed_workspaces: [{ workspace_id: 'ws-2', name: 'Other' }] }))

    const result = await getSearchAccess('ws-1')
    expect(result).toHaveLength(1)
  })
})

describe('updateSearchAccess', () => {
  it('updates search access', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ search_allowed_workspaces: [{ workspace_id: 'ws-2' }] }))

    const result = await updateSearchAccess('ws-1', ['ws-2'])
    expect(result).toHaveLength(1)
  })
})

describe('listSharingOptions', () => {
  it('lists sharing options', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ workspaces: [{ workspace_id: 'ws-2', name: 'Other' }] }))

    const result = await listSharingOptions('ws-1')
    expect(result).toHaveLength(1)
  })
})

// ============================================================================
// Documents
// ============================================================================
describe('getDocumentUploadUrl', () => {
  it('returns upload URL', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({
      upload_url: 'https://s3.amazonaws.com/upload',
      s3_key: 'key',
      bucket: 'bucket',
      expires_in: 3600,
    }))

    const result = await getDocumentUploadUrl('ws-1', 'file.pdf')
    expect(result.upload_url).toBe('https://s3.amazonaws.com/upload')
  })

  it('throws on empty response', async () => {
    // Return response with empty text
    mockFetch.mockResolvedValue({
      ok: true,
      status: 200,
      text: () => Promise.resolve(''),
      json: () => Promise.resolve(null),
    } as Response)

    await expect(getDocumentUploadUrl('ws-1', 'file.pdf')).rejects.toThrow('Empty response from server')
  })
})

describe('listDocuments', () => {
  it('returns documents without prefix', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ documents: [{ path: 'docs/readme.md' }] }))

    const result = await listDocuments('ws-1')
    expect(result).toHaveLength(1)
    expect(mockFetch).toHaveBeenCalledWith('/api/workspaces/ws-1/documents')
  })

  it('passes prefix parameter', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ documents: [] }))

    await listDocuments('ws-1', 'docs/')

    expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining('prefix=docs%2F'))
  })
})

describe('deleteDocument', () => {
  it('deletes document', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(deleteDocument('ws-1', 'docs/readme.md')).resolves.toBeUndefined()
  })
})

// ============================================================================
// Confluence
// ============================================================================
describe('getConfluenceAuthUrl', () => {
  it('returns auth url', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ auth_url: 'https://auth.atlassian.com/...' }))

    const result = await getConfluenceAuthUrl('ws-1')
    expect(result).toBe('https://auth.atlassian.com/...')
  })
})

describe('listConfluenceConnections', () => {
  it('returns connections', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ connections: [{ connection_id: 'conn-1' }] }))

    const result = await listConfluenceConnections('ws-1')
    expect(result).toHaveLength(1)
  })
})

describe('deleteConfluenceConnection', () => {
  it('deletes connection', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(deleteConfluenceConnection('ws-1', 'conn-1')).resolves.toBeUndefined()
  })
})

describe('triggerConfluenceSync', () => {
  it('triggers sync', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(triggerConfluenceSync('ws-1', 'conn-1')).resolves.toBeUndefined()
  })
})

// ============================================================================
// Jira
// ============================================================================
describe('getJiraAuthUrl', () => {
  it('returns auth url', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ auth_url: 'https://auth.atlassian.com/jira' }))

    const result = await getJiraAuthUrl('ws-1')
    expect(result).toBe('https://auth.atlassian.com/jira')
  })
})

describe('listJiraConnections', () => {
  it('returns connections', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ connections: [{ connection_id: 'jira-1' }] }))

    const result = await listJiraConnections('ws-1')
    expect(result).toHaveLength(1)
  })
})

describe('deleteJiraConnection', () => {
  it('deletes connection', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(deleteJiraConnection('ws-1', 'jira-1')).resolves.toBeUndefined()
  })
})

describe('triggerJiraSync', () => {
  it('triggers sync', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(triggerJiraSync('ws-1', 'jira-1')).resolves.toBeUndefined()
  })
})

// ============================================================================
// Outlook
// ============================================================================
describe('getOutlookAuthUrl', () => {
  it('returns auth url', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ auth_url: 'https://login.microsoftonline.com/...' }))

    const result = await getOutlookAuthUrl('ws-1')
    expect(result).toBe('https://login.microsoftonline.com/...')
  })
})

describe('listOutlookConnections', () => {
  it('returns connections', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ connections: [{ connection_id: 'outlook-1' }] }))

    const result = await listOutlookConnections('ws-1')
    expect(result).toHaveLength(1)
  })
})

describe('deleteOutlookConnection', () => {
  it('deletes connection', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(deleteOutlookConnection('ws-1', 'outlook-1')).resolves.toBeUndefined()
  })
})

describe('triggerOutlookSync', () => {
  it('triggers sync', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(triggerOutlookSync('ws-1', 'outlook-1')).resolves.toBeUndefined()
  })
})

describe('listOutlookFolders', () => {
  it('returns folders', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ folders: [{ id: 'f1', displayName: 'Inbox' }], selected_count: 1 }))

    const result = await listOutlookFolders('ws-1', 'outlook-1')
    expect(result.folders).toHaveLength(1)
    expect(result.selected_count).toBe(1)
  })
})

describe('updateOutlookFolders', () => {
  it('updates folders', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(updateOutlookFolders('ws-1', 'outlook-1', [{ id: 'f1', displayName: 'Inbox' }])).resolves.toBeUndefined()
  })
})

// ============================================================================
// Teams
// ============================================================================
describe('getTeamsAuthUrl', () => {
  it('returns auth url', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ auth_url: 'https://login.microsoftonline.com/teams' }))

    const result = await getTeamsAuthUrl('ws-1')
    expect(result).toBe('https://login.microsoftonline.com/teams')
  })

  it('throws on error', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ error: 'Unauthorized' }, false, 401))

    await expect(getTeamsAuthUrl('ws-1')).rejects.toThrow('Unauthorized')
  })
})

describe('listTeamsConnections', () => {
  it('returns connections', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ connections: [{ connection_id: 'teams-1' }] }))

    const result = await listTeamsConnections('ws-1')
    expect(result).toHaveLength(1)
  })

  it('throws on error', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ error: 'Fail' }, false, 500))

    await expect(listTeamsConnections('ws-1')).rejects.toThrow('Fail')
  })
})

describe('listTeamsTeams', () => {
  it('returns teams', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ teams: [{ id: 't1', displayName: 'Engineering' }] }))

    const result = await listTeamsTeams('ws-1', 'teams-1')
    expect(result).toHaveLength(1)
  })
})

describe('listTeamsChannels', () => {
  it('returns channels', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ channels: [{ id: 'ch1', displayName: 'General' }] }))

    const result = await listTeamsChannels('ws-1', 'teams-1', 't1')
    expect(result).toHaveLength(1)
  })
})

describe('saveTeamsConfig', () => {
  it('saves config', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(saveTeamsConfig('ws-1', 'teams-1', { days_back: 30, include_channels: [], sync_files: true })).resolves.toBeUndefined()
  })
})

describe('triggerTeamsSync', () => {
  it('triggers sync', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(triggerTeamsSync('ws-1', 'teams-1')).resolves.toBeUndefined()
  })
})

describe('deleteTeamsConnection', () => {
  it('deletes connection', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(deleteTeamsConnection('ws-1', 'teams-1')).resolves.toBeUndefined()
  })
})

// ============================================================================
// SharePoint
// ============================================================================
describe('getSharePointAuthUrl', () => {
  it('returns auth url', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ auth_url: 'https://login.microsoftonline.com/sp' }))

    const result = await getSharePointAuthUrl('ws-1')
    expect(result).toBe('https://login.microsoftonline.com/sp')
  })
})

describe('listSharePointConnections', () => {
  it('returns connections', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ connections: [{ connection_id: 'sp-1' }] }))

    const result = await listSharePointConnections('ws-1')
    expect(result).toHaveLength(1)
  })
})

describe('listSharePointSites', () => {
  it('returns sites', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ sites: [{ id: 's1', displayName: 'Intranet' }] }))

    const result = await listSharePointSites('ws-1', 'sp-1')
    expect(result).toHaveLength(1)
  })
})

describe('updateSharePointSites', () => {
  it('updates sites', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(updateSharePointSites('ws-1', 'sp-1', [], true)).resolves.toBeUndefined()
  })
})

describe('triggerSharePointSync', () => {
  it('triggers sync', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(triggerSharePointSync('ws-1', 'sp-1')).resolves.toBeUndefined()
  })
})

describe('deleteSharePointConnection', () => {
  it('deletes connection', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(deleteSharePointConnection('ws-1', 'sp-1')).resolves.toBeUndefined()
  })
})

// ============================================================================
// GitHub
// ============================================================================
describe('getGitHubInstallUrl', () => {
  it('returns install url', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ install_url: 'https://github.com/apps/test/installations/new' }))

    const result = await getGitHubInstallUrl('ws-1')
    expect(result).toBe('https://github.com/apps/test/installations/new')
  })
})

describe('listGitHubInstallations', () => {
  it('returns installations', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ installations: [{ installation_id: 'install-1' }] }))

    const result = await listGitHubInstallations('ws-1')
    expect(result).toHaveLength(1)
  })
})

describe('listGitHubInstallationRepos', () => {
  it('returns repos', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ repos: [{ full_name: 'org/repo' }] }))

    const result = await listGitHubInstallationRepos('ws-1', 'install-1')
    expect(result).toHaveLength(1)
  })
})

describe('listGitHubRepos', () => {
  it('returns repos', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ repos: [{ repo_name: 'org/repo' }] }))

    const result = await listGitHubRepos('ws-1')
    expect(result).toHaveLength(1)
  })
})

describe('addGitHubRepos', () => {
  it('adds repos', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ repos: [{ repo_name: 'org/repo' }] }))

    const result = await addGitHubRepos('ws-1', [{ repo_name: 'org/repo', installation_id: 'install-1' }])
    expect(result).toHaveLength(1)
  })
})

describe('updateGitHubRepo', () => {
  it('updates repo', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ repo: { repo_name: 'org/repo', enabled: false } }))

    const result = await updateGitHubRepo('ws-1', 'org/repo', { enabled: false })
    expect(result.enabled).toBe(false)
  })
})

describe('removeGitHubRepo', () => {
  it('removes repo', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(removeGitHubRepo('ws-1', 'org/repo')).resolves.toBeUndefined()
  })
})

describe('triggerGitHubSync', () => {
  it('triggers sync for all repos', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(triggerGitHubSync('ws-1')).resolves.toBeUndefined()
    expect(mockFetch).toHaveBeenCalledWith('/api/workspaces/ws-1/github/sync', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({}),
    }))
  })

  it('triggers sync for a specific repo', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(triggerGitHubSync('ws-1', 'org/repo')).resolves.toBeUndefined()
    expect(mockFetch).toHaveBeenCalledWith('/api/workspaces/ws-1/github/sync', expect.objectContaining({
      body: JSON.stringify({ repo_name: 'org/repo' }),
    }))
  })
})

// ============================================================================
// Confluence extended
// ============================================================================
describe('listConfluenceSpaces', () => {
  it('returns spaces', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ spaces: [{ id: 'sp1', key: 'ENG', name: 'Engineering' }], selected_count: 1 }))

    const result = await listConfluenceSpaces('ws-1', 'conn-1')
    expect(result.spaces).toHaveLength(1)
    expect(result.selected_count).toBe(1)
  })
})

describe('updateConfluenceSpaces', () => {
  it('updates spaces', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(updateConfluenceSpaces('ws-1', 'conn-1', [], false)).resolves.toBeUndefined()
  })
})

// ============================================================================
// Jira extended
// ============================================================================
describe('listJiraProjects', () => {
  it('returns projects', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({ projects: [{ id: 'p1', key: 'ENG', name: 'Engineering' }], selected_count: 0 }))

    const result = await listJiraProjects('ws-1', 'jira-1')
    expect(result.projects).toHaveLength(1)
  })
})

describe('updateJiraProjects', () => {
  it('updates projects', async () => {
    mockFetch.mockResolvedValue(createMockFetchResponse({}))

    await expect(updateJiraProjects('ws-1', 'jira-1', ['ENG'])).resolves.toBeUndefined()
  })
})
