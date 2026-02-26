import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
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
} from '../../../src/lib/api'
import { createWorkspace as mockWorkspace, createWorkspaceMember, createMockFetchResponse } from '../../helpers/mocks'

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('listWorkspaces', () => {
  it('returns workspaces', async () => {
    const ws = mockWorkspace()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ workspaces: [ws] }))

    const result = await listWorkspaces()
    expect(fetch).toHaveBeenCalledWith('/api/workspaces')
    expect(result).toHaveLength(1)
    expect(result[0].workspace_id).toBe('ws-1')
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Unauthorized' }, false, 401))
    await expect(listWorkspaces()).rejects.toThrow('Unauthorized')
  })
})

describe('createWorkspace', () => {
  it('creates a workspace', async () => {
    const ws = mockWorkspace({ name: 'New WS' })
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ workspace: ws }))

    const result = await createWorkspace('New WS', 'Description')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ name: 'New WS', description: 'Description' }),
      })
    )
    expect(result.name).toBe('New WS')
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Duplicate name' }, false, 409))
    await expect(createWorkspace('Dup')).rejects.toThrow('Duplicate name')
  })

  it('throws on invalid response', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({}))
    await expect(createWorkspace('Test')).rejects.toThrow('Invalid response from server')
  })
})

describe('getWorkspace', () => {
  it('returns workspace details', async () => {
    const ws = mockWorkspace()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ workspace: ws }))

    const result = await getWorkspace('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1')
    expect(result.workspace_id).toBe('ws-1')
  })

  it('throws on not found', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Not found' }, false, 404))
    await expect(getWorkspace('bad')).rejects.toThrow('Not found')
  })
})

describe('updateWorkspace', () => {
  it('updates workspace', async () => {
    const ws = mockWorkspace({ name: 'Updated' })
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ workspace: ws }))

    const result = await updateWorkspace('ws-1', { name: 'Updated' })
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1',
      expect.objectContaining({ method: 'PATCH' })
    )
    expect(result.name).toBe('Updated')
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Forbidden' }, false, 403))
    await expect(updateWorkspace('ws-1', { name: 'x' })).rejects.toThrow('Forbidden')
  })
})

describe('deleteWorkspace', () => {
  it('deletes workspace', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await deleteWorkspace('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1', expect.objectContaining({ method: 'DELETE' }))
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Not allowed' }, false, 403))
    await expect(deleteWorkspace('ws-1')).rejects.toThrow('Not allowed')
  })
})

describe('listMembers', () => {
  it('returns members', async () => {
    const member = createWorkspaceMember()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ members: [member] }))

    const result = await listMembers('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/members')
    expect(result).toHaveLength(1)
    expect(result[0].user_email).toBe('user@example.com')
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Forbidden' }, false, 403))
    await expect(listMembers('ws-1')).rejects.toThrow('Forbidden')
  })
})

describe('addMember', () => {
  it('adds a member', async () => {
    const member = createWorkspaceMember()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ member }))

    const result = await addMember('ws-1', 'user@example.com', 'member')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/members',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ email: 'user@example.com', role: 'member' }),
      })
    )
    expect(result.user_email).toBe('user@example.com')
  })

  it('throws on duplicate', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Already exists' }, false, 409))
    await expect(addMember('ws-1', 'dup@test.com')).rejects.toThrow('Already exists')
  })
})

describe('updateMember', () => {
  it('updates member role', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await updateMember('ws-1', 'user@example.com', 'workspace_admin')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/members/user%40example.com',
      expect.objectContaining({ method: 'PATCH' })
    )
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Forbidden' }, false, 403))
    await expect(updateMember('ws-1', 'u@t.com', 'member')).rejects.toThrow('Forbidden')
  })
})

describe('removeMember', () => {
  it('removes a member', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await removeMember('ws-1', 'user@example.com')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/members/user%40example.com',
      expect.objectContaining({ method: 'DELETE' })
    )
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Cannot remove' }, false, 400))
    await expect(removeMember('ws-1', 'u@t.com')).rejects.toThrow('Cannot remove')
  })
})

describe('listWorkspaceRepos', () => {
  it('returns repos', async () => {
    const repo = { repo_name: 'org/repo', workspace_id: 'ws-1', enabled: true, added_at: '2024-01-01', added_by: 'test' }
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ repos: [repo] }))

    const result = await listWorkspaceRepos('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/repos')
    expect(result).toHaveLength(1)
  })
})

describe('listAvailableRepos', () => {
  it('returns available repos', async () => {
    const repo = { full_name: 'org/repo', name: 'repo', description: 'desc', private: false, updated_at: '2024-01-01' }
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ repos: [repo] }))

    const result = await listAvailableRepos('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/repos/available')
    expect(result).toHaveLength(1)
  })
})

describe('addWorkspaceRepo', () => {
  it('adds a repo', async () => {
    const repo = { repo_name: 'org/repo', workspace_id: 'ws-1', enabled: true, added_at: '2024-01-01', added_by: 'test' }
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ repo }))

    const result = await addWorkspaceRepo('ws-1', 'org/repo')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/repos',
      expect.objectContaining({ method: 'POST' })
    )
    expect(result.repo_name).toBe('org/repo')
  })
})

describe('removeWorkspaceRepo', () => {
  it('removes a repo', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await removeWorkspaceRepo('ws-1', 'org/repo')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/repos/org%2Frepo',
      expect.objectContaining({ method: 'DELETE' })
    )
  })
})
