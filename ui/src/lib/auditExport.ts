/**
 * Client-side audit-event exporter for the InfoSec drill flow.
 *
 * When an admin is in user-history mode ("Chat history for X, last 3h")
 * they need a way to escalate the data — attach to a ticket, hand off to
 * legal, ingest into a SIEM. Server-side streaming export would be the
 * "right" answer for unbounded data sets, but the GSI1 query is already
 * scoped to a user + category + small time window, so we stay in the
 * browser: paginate to completion, format, blob-download.
 *
 * Bounded by MAX_PAGES so an over-eager click can't run forever; the
 * page cap times the per-page limit also bounds total memory.
 */

import { getAuditEvents, type AuditEvent, type AuditEventFilters } from './api'

const MAX_PAGES = 50  // safety: 50 pages × 100/page = 5000 events
const PAGE_SIZE = 100

/**
 * Fetch every audit event matching the given filter, following
 * next_cursor until exhausted (or until MAX_PAGES is hit).
 */
export async function fetchAllAuditEvents(
  filters: Omit<AuditEventFilters, 'cursor' | 'limit'>,
): Promise<{ events: AuditEvent[]; truncated: boolean }> {
  const all: AuditEvent[] = []
  let cursor: string | undefined
  let truncated = false
  for (let page = 0; page < MAX_PAGES; page++) {
    const result = await getAuditEvents({
      ...filters,
      cursor,
      limit: PAGE_SIZE,
    })
    all.push(...result.events)
    cursor = result.next_cursor || undefined
    if (!cursor) return { events: all, truncated: false }
  }
  // Hit the cap with more pages still pending; surface so the UI can
  // warn the operator they're escalating an incomplete set.
  truncated = true
  return { events: all, truncated }
}

// Columns chosen for InfoSec / legal / HR escalation: the who/what/where
// metadata first, then the actual prompt + response so a reviewer can
// read the chat in chronological order. details_json is last because
// it's free-form per action and most reviewers won't parse it.
const CSV_COLUMNS: Array<keyof AuditEvent | 'details_json'> = [
  'timestamp',
  'user_email',
  'source_ip',
  'user_agent',
  'lambda_name',
  'model_id',
  'status',
  'flagged_reason',
  'workspace_id',
  'request_id',
  'category',
  'action',
  'input_text',
  'output_text',
  'details_json',
]

/** RFC 4180 field escaping: quote if value contains comma / quote / CR / LF. */
function escapeCsvField(val: unknown): string {
  if (val === null || val === undefined) return ''
  const s = typeof val === 'string' ? val : JSON.stringify(val)
  if (/[",\r\n]/.test(s)) {
    return '"' + s.replace(/"/g, '""') + '"'
  }
  return s
}

export function eventsToCsv(events: AuditEvent[]): string {
  const lines: string[] = [CSV_COLUMNS.join(',')]
  for (const ev of events) {
    const row = CSV_COLUMNS.map(col => {
      if (col === 'details_json') {
        return escapeCsvField(JSON.stringify(ev.details ?? {}))
      }
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return escapeCsvField((ev as any)[col])
    })
    lines.push(row.join(','))
  }
  return lines.join('\r\n') + '\r\n'
}

export function eventsToJson(events: AuditEvent[]): string {
  return JSON.stringify(events, null, 2)
}

/** Replace path-unsafe characters in user-supplied filename pieces. */
function sanitize(s: string): string {
  return s.replace(/[^a-zA-Z0-9\-_.]/g, '_')
}

export function buildExportFilename(
  userEmail: string,
  startIso: string,
  endIso: string,
  format: 'csv' | 'json',
): string {
  // Compact "YYYY-MM-DD_HH-mm" for both ends so file managers sort
  // chronologically and the timestamp survives shells that mangle ':'.
  const fmt = (iso: string) =>
    iso.slice(0, 16).replace('T', '_').replace(':', '-')
  return `audit-${sanitize(userEmail)}-${fmt(startIso)}-to-${fmt(endIso)}.${format}`
}

/** Trigger a browser download for `content`. Safe to call from a click handler. */
export function downloadFile(content: string, filename: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  // appendChild + remove dance is needed for Firefox.
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  // Hand the URL back to the GC; some browsers leak otherwise.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
