import { useEffect } from 'react'
import { X } from 'lucide-react'
import type { AuditEvent } from '../lib/api'

interface AuditEventDrawerProps {
  event: AuditEvent | null
  onClose: () => void
}

/**
 * Right-side drawer surfacing a full audit row. The 480px width and the
 * IP / user-agent / request-id triad are deliberate: this is the screen
 * the InfoSec demo focuses on, so they need to be one click and one copy
 * away from any row.
 */
export default function AuditEventDrawer({ event, onClose }: AuditEventDrawerProps) {
  // Close on Escape so power users don't have to mouse over to the X.
  useEffect(() => {
    if (!event) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [event, onClose])

  if (!event) return null

  const copyToClipboard = (text: string) => {
    if (navigator?.clipboard?.writeText) {
      navigator.clipboard.writeText(text).catch(() => {
        /* clipboard can fail in iframes / unsecured contexts; non-fatal */
      })
    }
  }

  const copyAsJson = () => {
    copyToClipboard(JSON.stringify(event, null, 2))
  }

  return (
    <div
      role="dialog"
      aria-label="Audit event detail"
      className="fixed inset-0 z-50 flex justify-end bg-black/30"
      onClick={onClose}
    >
      <div
        className="w-full max-w-[480px] h-full bg-white shadow-xl overflow-y-auto"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-start justify-between p-4 border-b">
          <div>
            <div className="text-xs uppercase text-gray-500">{event.category || 'admin'}</div>
            <div className="text-lg font-semibold">{event.action || 'UNKNOWN'}</div>
            <div className="text-xs text-gray-500" title={event.timestamp}>
              {event.timestamp}
            </div>
            <div className="text-sm text-gray-700 mt-1">{event.user_email || '—'}</div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close detail"
            className="p-1 text-gray-500 hover:text-gray-800"
          >
            <X size={20} />
          </button>
        </div>

        <div className="p-4 space-y-4">
          <DetailField label="Workspace" value={event.workspace_id || '—'} />
          <DetailField
            label="Request ID"
            value={event.request_id || '—'}
            onCopy={() => copyToClipboard(event.request_id || '')}
          />
          <DetailField
            label="Source IP"
            value={event.source_ip || '—'}
            onCopy={() => copyToClipboard(event.source_ip || '')}
          />
          <DetailField
            label="User Agent"
            value={event.user_agent || '—'}
            multiline
          />

          <div>
            <div className="flex items-center justify-between mb-1">
              <div className="text-xs uppercase text-gray-500">Details</div>
              <button
                onClick={copyAsJson}
                className="text-xs text-blue-600 hover:underline"
              >
                Copy as JSON
              </button>
            </div>
            <pre className="text-xs font-mono bg-gray-50 border rounded p-2 whitespace-pre-wrap break-all">
              {JSON.stringify(event.details ?? {}, null, 2)}
            </pre>
          </div>
        </div>
      </div>
    </div>
  )
}

interface DetailFieldProps {
  label: string
  value: string
  onCopy?: () => void
  multiline?: boolean
}

function DetailField({ label, value, onCopy, multiline = false }: DetailFieldProps) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <div className="text-xs uppercase text-gray-500">{label}</div>
        {onCopy && (
          <button
            onClick={onCopy}
            className="text-xs text-blue-600 hover:underline"
            aria-label={`Copy ${label}`}
          >
            Copy
          </button>
        )}
      </div>
      <div
        className={`text-sm font-mono break-all ${multiline ? 'whitespace-pre-wrap' : ''}`}
      >
        {value}
      </div>
    </div>
  )
}
