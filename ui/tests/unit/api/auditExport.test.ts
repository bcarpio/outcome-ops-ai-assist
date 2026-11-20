// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  eventsToCsv,
  eventsToJson,
  buildExportFilename,
  fetchAllAuditEvents,
} from '../../../src/lib/auditExport'
import type { AuditEvent } from '../../../src/lib/api'

vi.mock('../../../src/lib/api', () => ({
  getAuditEvents: vi.fn(),
}))
import { getAuditEvents } from '../../../src/lib/api'
const mockEvents = vi.mocked(getAuditEvents)

function buildEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    request_id: 'r1',
    timestamp: '2026-05-04T12:00:00+00:00',
    category: 'bedrock',
    action: '',
    user_email: 'brian@example.com',
    workspace_id: 'ws-1',
    source_ip: '203.0.113.10',
    user_agent: 'Mozilla/5.0',
    details: {},
    status: 'success',
    lambda_name: 'chat',
    model_id: 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
    input_text: 'hello',
    output_text: 'hi',
    ...overrides,
  }
}

describe('eventsToCsv', () => {
  it('emits a header row + one row per event', () => {
    const csv = eventsToCsv([buildEvent()])
    const lines = csv.split('\r\n')
    expect(lines[0]).toContain('timestamp,user_email,source_ip')
    expect(lines[0]).toContain('input_text,output_text,details_json')
    // 1 header + 1 row + trailing newline (== 1 empty line)
    expect(lines.length).toBe(3)
    expect(lines[2]).toBe('')
  })

  it('quotes fields containing commas', () => {
    const csv = eventsToCsv([
      buildEvent({ output_text: 'first, second, third' }),
    ])
    expect(csv).toContain('"first, second, third"')
  })

  it('escapes embedded double-quotes per RFC 4180', () => {
    const csv = eventsToCsv([
      buildEvent({ input_text: 'he said "hi"' }),
    ])
    // " inside a quoted field becomes ""
    expect(csv).toContain('"he said ""hi"""')
  })

  it('quotes fields containing newlines', () => {
    const csv = eventsToCsv([
      buildEvent({ output_text: 'line one\nline two' }),
    ])
    // Whole field is quoted; the embedded LF is preserved as-is.
    expect(csv).toMatch(/"line one\nline two"/)
  })

  it('serializes details as a JSON string in details_json column', () => {
    const csv = eventsToCsv([
      buildEvent({ details: { key_id: 'kid_xyz', name: 'ci-bot' } }),
    ])
    // The JSON contains a comma + quotes, so the whole field is quoted
    // and the inner quotes are doubled.
    expect(csv).toContain('"{""key_id"":""kid_xyz"",""name"":""ci-bot""}"')
  })

  it('emits empty cells for missing optional fields', () => {
    const csv = eventsToCsv([
      buildEvent({ flagged_reason: undefined, model_id: undefined }),
    ])
    // ,,, sequences indicate empty fields between commas.
    expect(csv).toMatch(/,,/)
  })
})

describe('eventsToJson', () => {
  it('round-trips through JSON.parse', () => {
    const evs = [buildEvent({ request_id: 'a' }), buildEvent({ request_id: 'b' })]
    const json = eventsToJson(evs)
    const parsed = JSON.parse(json)
    expect(parsed).toHaveLength(2)
    expect(parsed[0].request_id).toBe('a')
    expect(parsed[1].request_id).toBe('b')
  })
})

describe('buildExportFilename', () => {
  it('sanitizes the user email and embeds the date range', () => {
    const name = buildExportFilename(
      'brian@example.com',
      '2026-05-04T01:00:00+00:00',
      '2026-05-04T04:00:00+00:00',
      'csv',
    )
    expect(name).toBe('audit-brian_example.com-2026-05-04_01-00-to-2026-05-04_04-00.csv')
  })

  it('replaces unsafe characters with underscores', () => {
    const name = buildExportFilename(
      'a/b\\c?d#e@x.com',
      '2026-05-04T00:00:00+00:00',
      '2026-05-04T01:00:00+00:00',
      'json',
    )
    // Each unsafe char becomes _
    expect(name.startsWith('audit-a_b_c_d_e_x.com-')).toBe(true)
    expect(name.endsWith('.json')).toBe(true)
    // No path separators
    expect(name).not.toContain('/')
    expect(name).not.toContain('\\')
  })
})

describe('fetchAllAuditEvents', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('paginates until next_cursor is null', async () => {
    mockEvents
      .mockResolvedValueOnce({
        events: [buildEvent({ request_id: 'p1-1' })],
        next_cursor: 'cursor-1',
        limit: 100,
        start_date: '',
        end_date: '',
      })
      .mockResolvedValueOnce({
        events: [buildEvent({ request_id: 'p2-1' })],
        next_cursor: 'cursor-2',
        limit: 100,
        start_date: '',
        end_date: '',
      })
      .mockResolvedValueOnce({
        events: [buildEvent({ request_id: 'p3-1' })],
        next_cursor: null,
        limit: 100,
        start_date: '',
        end_date: '',
      })

    const { events, truncated } = await fetchAllAuditEvents({
      userEmail: 'brian@example.com',
      category: 'bedrock',
    })
    expect(events.map(e => e.request_id)).toEqual(['p1-1', 'p2-1', 'p3-1'])
    expect(truncated).toBe(false)
    expect(mockEvents).toHaveBeenCalledTimes(3)
    // Second call carries cursor from first response
    expect(mockEvents.mock.calls[1][0]?.cursor).toBe('cursor-1')
  })

  it('reports truncated=true when MAX_PAGES is hit', async () => {
    // Always return a cursor; the loop should bail at the safety cap.
    mockEvents.mockResolvedValue({
      events: [buildEvent()],
      next_cursor: 'never-null',
      limit: 100,
      start_date: '',
      end_date: '',
    })

    const { events, truncated } = await fetchAllAuditEvents({
      userEmail: 'noisy@example.com',
      category: 'bedrock',
    })
    expect(truncated).toBe(true)
    // 50 pages × 1 event/page from this stub.
    expect(events).toHaveLength(50)
  })

  it('forwards filters to every page request', async () => {
    mockEvents.mockResolvedValueOnce({
      events: [],
      next_cursor: null,
      limit: 100,
      start_date: '',
      end_date: '',
    })

    await fetchAllAuditEvents({
      userEmail: 'brian@example.com',
      category: 'bedrock',
      startDate: '2026-05-04T00:00:00+00:00',
      endDate: '2026-05-04T03:00:00+00:00',
    })

    expect(mockEvents).toHaveBeenCalledWith({
      userEmail: 'brian@example.com',
      category: 'bedrock',
      startDate: '2026-05-04T00:00:00+00:00',
      endDate: '2026-05-04T03:00:00+00:00',
      cursor: undefined,
      limit: 100,
    })
  })
})
