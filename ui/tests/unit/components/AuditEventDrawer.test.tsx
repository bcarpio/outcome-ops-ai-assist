import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AuditEventDrawer from '../../../src/components/AuditEventDrawer'
import type { AuditEvent } from '../../../src/lib/api'

function buildEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    request_id: 'r1',
    timestamp: '2026-05-01T12:00:00+00:00',
    category: 'admin',
    action: 'API_KEY_CREATED',
    user_email: 'brian@example.com',
    workspace_id: 'ws-1',
    source_ip: '203.0.113.42',
    user_agent: 'Mozilla/5.0',
    details: { key_id: 'kid_xyz' },
    ...overrides,
  }
}

const writeText = vi.fn().mockResolvedValue(undefined)

beforeEach(() => {
  writeText.mockClear()
  // userEvent.setup() owns navigator.clipboard if not pre-set, so we stub it
  // before each render. Use vi.stubGlobal so the override persists past the
  // user-event setup that runs inside each test body.
  vi.stubGlobal('navigator', {
    ...navigator,
    clipboard: { writeText },
  })
})

describe('AuditEventDrawer', () => {
  it('renders nothing when event is null', () => {
    const { container } = render(
      <AuditEventDrawer event={null} onClose={vi.fn()} />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('renders the IP / UA / request-id triad and details JSON', () => {
    render(<AuditEventDrawer event={buildEvent()} onClose={vi.fn()} />)
    expect(screen.getByText('Request ID')).toBeInTheDocument()
    expect(screen.getByText('Source IP')).toBeInTheDocument()
    expect(screen.getByText('User Agent')).toBeInTheDocument()
    expect(screen.getByText('Mozilla/5.0')).toBeInTheDocument()
    expect(screen.getByText(/kid_xyz/)).toBeInTheDocument()
  })

  it('copies request_id to clipboard when the per-field button is clicked', () => {
    render(<AuditEventDrawer event={buildEvent()} onClose={vi.fn()} />)

    // fireEvent (not user-event) so we bypass user-event's own clipboard
    // emulation and exercise the component's navigator.clipboard call path.
    fireEvent.click(screen.getByRole('button', { name: /Copy Request ID/i }))
    expect(writeText).toHaveBeenCalledWith('r1')
  })

  it('Copy as JSON puts a serialized event on the clipboard', () => {
    const event = buildEvent()
    render(<AuditEventDrawer event={event} onClose={vi.fn()} />)

    fireEvent.click(screen.getByText('Copy as JSON'))
    const written = writeText.mock.calls[0]?.[0] ?? ''
    expect(written).toContain('"request_id": "r1"')
    expect(written).toContain('"key_id": "kid_xyz"')
  })

  it('calls onClose when the X button is clicked', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<AuditEventDrawer event={buildEvent()} onClose={onClose} />)

    await user.click(screen.getByRole('button', { name: 'Close detail' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose on Escape', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<AuditEventDrawer event={buildEvent()} onClose={onClose} />)

    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalled()
  })

  it('renders the PII redaction summary panel for ingest rows', () => {
    const event = buildEvent({
      action: 'DOCUMENT_INGESTED',
      details: {
        source: 'github',
        document_id: 'docs/decision.md',
        char_count: 1234,
        redaction_summary: { EMAIL: 3, PHONE: 1 },
        skipped_reason: '',
        mode: 'STRICT',
      },
    })
    render(<AuditEventDrawer event={event} onClose={vi.fn()} />)
    expect(screen.getByText('PII redacted on ingest')).toBeInTheDocument()
    expect(screen.getByText('EMAIL')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('PHONE')).toBeInTheDocument()
  })

  it('hides the redaction panel when summary is empty (no PII found)', () => {
    const event = buildEvent({
      action: 'DOCUMENT_INGESTED',
      details: {
        source: 'github',
        document_id: 'README.md',
        char_count: 500,
        redaction_summary: {},
        skipped_reason: '',
        mode: 'STRICT',
      },
    })
    render(<AuditEventDrawer event={event} onClose={vi.fn()} />)
    expect(
      screen.queryByText('PII redacted on ingest'),
    ).not.toBeInTheDocument()
  })

  it('renders the skipped reason for ingest rows where redaction was bypassed', () => {
    const event = buildEvent({
      action: 'DOCUMENT_INGESTED',
      details: {
        source: 'github',
        document_id: 'docs/x.md',
        char_count: 800,
        redaction_summary: {},
        skipped_reason: 'workspace_none',
        mode: 'NONE',
      },
    })
    render(<AuditEventDrawer event={event} onClose={vi.fn()} />)
    expect(screen.getByText(/Redaction skipped/)).toBeInTheDocument()
    expect(screen.getByText('workspace_none')).toBeInTheDocument()
  })

  it('does not render the redaction panel for non-ingest rows even with a redaction_summary in details', () => {
    const event = buildEvent({
      action: 'API_KEY_CREATED',
      details: { redaction_summary: { EMAIL: 99 } },
    })
    render(<AuditEventDrawer event={event} onClose={vi.fn()} />)
    expect(
      screen.queryByText('PII redacted on ingest'),
    ).not.toBeInTheDocument()
  })
})
