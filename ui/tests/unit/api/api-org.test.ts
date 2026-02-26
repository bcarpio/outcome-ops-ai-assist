import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  listOrgMembers,
  addOrgMember,
  updateOrgMember,
  removeOrgMember,
} from '../../../src/lib/api'
import { createOrgMember, createMockFetchResponse } from '../../helpers/mocks'

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('listOrgMembers', () => {
  it('returns org members', async () => {
    const member = createOrgMember()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ members: [member] }))

    const result = await listOrgMembers()
    expect(fetch).toHaveBeenCalledWith('/api/org/members')
    expect(result).toHaveLength(1)
    expect(result[0].user_email).toBe('user@example.com')
    expect(result[0].org_role).toBe('member')
  })

  it('returns empty array when no members', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ members: [] }))
    const result = await listOrgMembers()
    expect(result).toEqual([])
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Unauthorized' }, false, 401))
    await expect(listOrgMembers()).rejects.toThrow('Unauthorized')
  })
})

describe('addOrgMember', () => {
  it('adds a member with default role', async () => {
    const member = createOrgMember()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ member }))

    const result = await addOrgMember('user@example.com')
    expect(fetch).toHaveBeenCalledWith(
      '/api/org/members',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ email: 'user@example.com', org_role: 'member' }),
      })
    )
    expect(result.user_email).toBe('user@example.com')
  })

  it('adds a member as org_admin', async () => {
    const member = createOrgMember({ org_role: 'org_admin' })
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ member }))

    const result = await addOrgMember('admin@example.com', 'org_admin')
    const body = JSON.parse((fetch as ReturnType<typeof vi.fn>).mock.calls[0][1].body)
    expect(body.org_role).toBe('org_admin')
    expect(result.org_role).toBe('org_admin')
  })

  it('throws on duplicate member', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Member already exists' }, false, 409))
    await expect(addOrgMember('dup@example.com')).rejects.toThrow('Member already exists')
  })
})

describe('updateOrgMember', () => {
  it('updates member role to org_admin', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))

    await updateOrgMember('user@example.com', 'org_admin')
    expect(fetch).toHaveBeenCalledWith(
      '/api/org/members/user%40example.com',
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ org_role: 'org_admin' }),
      })
    )
  })

  it('updates member role to member', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))

    await updateOrgMember('admin@example.com', 'member')
    const body = JSON.parse((fetch as ReturnType<typeof vi.fn>).mock.calls[0][1].body)
    expect(body.org_role).toBe('member')
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Cannot demote last admin' }, false, 400))
    await expect(updateOrgMember('admin@example.com', 'member')).rejects.toThrow('Cannot demote last admin')
  })
})

describe('removeOrgMember', () => {
  it('removes a member', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))

    await removeOrgMember('user@example.com')
    expect(fetch).toHaveBeenCalledWith(
      '/api/org/members/user%40example.com',
      expect.objectContaining({ method: 'DELETE' })
    )
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Cannot remove last admin' }, false, 400))
    await expect(removeOrgMember('admin@example.com')).rejects.toThrow('Cannot remove last admin')
  })
})
