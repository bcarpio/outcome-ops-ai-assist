import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  sendChatMessage,
  listConversations,
  getConversation,
  deleteConversation,
  getSearchableWorkspaces,
  type StreamEvent,
} from '../../../src/lib/api'
import { createConversation, createMessage, createSearchableWorkspace, createMockStreamResponse } from '../../helpers/mocks'

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('sendChatMessage', () => {
  it('streams NDJSON events to callback', async () => {
    const events: StreamEvent[] = [
      { type: 'metadata', conversation_id: 'conv-1', sources: [], title: 'Test' },
      { type: 'token', text: 'Hello' },
      { type: 'token', text: ' world' },
      { type: 'done', message_id: 'msg-1' },
    ]
    global.fetch = vi.fn().mockResolvedValue(createMockStreamResponse(events))

    const received: StreamEvent[] = []
    await sendChatMessage(
      { message: 'test', conversation_id: 'conv-1' },
      (event) => received.push(event)
    )

    expect(fetch).toHaveBeenCalledWith(
      '/api/chat',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ message: 'test', conversation_id: 'conv-1' }),
      })
    )
    expect(received).toHaveLength(4)
    expect(received[0]).toEqual(events[0])
    expect(received[1]).toEqual({ type: 'token', text: 'Hello' })
    expect(received[3]).toEqual({ type: 'done', message_id: 'msg-1' })
  })

  it('throws on HTTP error', async () => {
    global.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 500 }))

    await expect(
      sendChatMessage({ message: 'test' }, vi.fn())
    ).rejects.toThrow('HTTP error: 500')
  })

  it('throws when response body is null', async () => {
    const response = new Response(null, { status: 200 })
    Object.defineProperty(response, 'body', { value: null })
    global.fetch = vi.fn().mockResolvedValue(response)

    await expect(
      sendChatMessage({ message: 'test' }, vi.fn())
    ).rejects.toThrow('No response body')
  })

  it('sends workspace_ids and advanced options', async () => {
    const events = [{ type: 'done', message_id: 'msg-1' }]
    global.fetch = vi.fn().mockResolvedValue(createMockStreamResponse(events))

    await sendChatMessage(
      { message: 'test', workspace_ids: ['ws-1'], advanced: true, top_k: 10, show_cost: true },
      vi.fn()
    )

    const body = JSON.parse((fetch as ReturnType<typeof vi.fn>).mock.calls[0][1].body)
    expect(body.workspace_ids).toEqual(['ws-1'])
    expect(body.advanced).toBe(true)
    expect(body.top_k).toBe(10)
    expect(body.show_cost).toBe(true)
  })
})

describe('listConversations', () => {
  it('returns conversations from API', async () => {
    const mockConv = createConversation()
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ conversations: [mockConv], has_more: false }),
    })

    const result = await listConversations()
    expect(fetch).toHaveBeenCalledWith('/api/conversations?limit=20')
    expect(result.conversations).toHaveLength(1)
    expect(result.conversations[0].conversation_id).toBe('conv-1')
  })

  it('returns empty response on error', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 500 })
    const result = await listConversations()
    expect(result).toEqual({ conversations: [], has_more: false })
  })

  it('returns empty response on network error', async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error('Network error'))
    const result = await listConversations()
    expect(result).toEqual({ conversations: [], has_more: false })
  })

  it('defaults title to Untitled when missing', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        conversations: [{ conversation_id: 'c1', message_count: 0, created_at: '2024-01-01', updated_at: '2024-01-01' }], has_more: false,
      }),
    })

    const result = await listConversations()
    expect(result.conversations[0].title).toBe('Untitled')
  })
})

describe('getConversation', () => {
  it('returns conversation with messages', async () => {
    const mockData = {
      conversation: createConversation(),
      messages: [createMessage()],
    }
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(mockData),
    })

    const result = await getConversation('conv-1')
    expect(fetch).toHaveBeenCalledWith('/api/conversations/conv-1')
    expect(result?.conversation.conversation_id).toBe('conv-1')
    expect(result?.messages).toHaveLength(1)
  })

  it('returns null on error', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 404 })
    const result = await getConversation('bad-id')
    expect(result).toBeNull()
  })

  it('returns null on network error', async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error('Network error'))
    const result = await getConversation('conv-1')
    expect(result).toBeNull()
  })
})

describe('deleteConversation', () => {
  it('returns true on success', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: true })
    const result = await deleteConversation('conv-1')
    expect(fetch).toHaveBeenCalledWith('/api/conversations/conv-1', { method: 'DELETE' })
    expect(result).toBe(true)
  })

  it('returns false on failure', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false })
    const result = await deleteConversation('conv-1')
    expect(result).toBe(false)
  })

  it('returns false on network error', async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error('Network error'))
    const result = await deleteConversation('conv-1')
    expect(result).toBe(false)
  })
})

describe('getSearchableWorkspaces', () => {
  it('returns searchable workspaces', async () => {
    const ws = createSearchableWorkspace()
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve(JSON.stringify({ workspaces: [ws] })),
    })

    const result = await getSearchableWorkspaces()
    expect(fetch).toHaveBeenCalledWith('/api/chat/searchable-workspaces')
    expect(result).toHaveLength(1)
    expect(result[0].workspace_id).toBe('ws-1')
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      text: () => Promise.resolve(JSON.stringify({ error: 'Forbidden' })),
    })

    await expect(getSearchableWorkspaces()).rejects.toThrow('Forbidden')
  })
})
