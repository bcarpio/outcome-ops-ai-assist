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
})
