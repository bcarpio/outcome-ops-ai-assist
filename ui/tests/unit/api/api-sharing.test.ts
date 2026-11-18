// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  getSearchAccess,
  updateSearchAccess,
  listSharingOptions,
} from '../../../src/lib/api'
import { createWorkspaceSearchAccess, createMockFetchResponse } from '../../helpers/mocks'

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('getSearchAccess', () => {
  it('returns workspaces with search access', async () => {
    const ws = createWorkspaceSearchAccess()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ search_allowed_workspaces: [ws] }))

    const result = await getSearchAccess('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/sharing')
    expect(result).toHaveLength(1)
    expect(result[0].workspace_id).toBe('ws-1')
    expect(result[0].name).toBe('Test Workspace')
  })

  it('returns empty array when none', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ search_allowed_workspaces: [] }))
    const result = await getSearchAccess('ws-1')
    expect(result).toEqual([])
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Forbidden' }, false, 403))
    await expect(getSearchAccess('ws-1')).rejects.toThrow('Forbidden')
  })
})

describe('updateSearchAccess', () => {
  it('updates allowed workspaces', async () => {
    const ws = createWorkspaceSearchAccess({ workspace_id: 'ws-2', name: 'Other WS' })
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ search_allowed_workspaces: [ws] }))

    const result = await updateSearchAccess('ws-1', ['ws-2'])
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/sharing',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ search_allowed_workspaces: ['ws-2'] }),
      })
    )
    expect(result).toHaveLength(1)
    expect(result[0].workspace_id).toBe('ws-2')
  })

  it('clears all access with empty array', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ search_allowed_workspaces: [] }))

    const result = await updateSearchAccess('ws-1', [])
    const body = JSON.parse((fetch as ReturnType<typeof vi.fn>).mock.calls[0][1].body)
    expect(body.search_allowed_workspaces).toEqual([])
    expect(result).toEqual([])
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Not found' }, false, 404))
    await expect(updateSearchAccess('ws-1', ['ws-2'])).rejects.toThrow('Not found')
  })
})

describe('listSharingOptions', () => {
  it('returns available workspaces for sharing', async () => {
    const ws1 = createWorkspaceSearchAccess({ workspace_id: 'ws-2', name: 'WS 2' })
    const ws2 = createWorkspaceSearchAccess({ workspace_id: 'ws-3', name: 'WS 3' })
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ workspaces: [ws1, ws2] }))

    const result = await listSharingOptions('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/sharing/available')
    expect(result).toHaveLength(2)
    expect(result[0].workspace_id).toBe('ws-2')
    expect(result[1].workspace_id).toBe('ws-3')
  })

  it('returns empty when no options', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ workspaces: [] }))
    const result = await listSharingOptions('ws-1')
    expect(result).toEqual([])
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Forbidden' }, false, 403))
    await expect(listSharingOptions('ws-1')).rejects.toThrow('Forbidden')
  })
})
