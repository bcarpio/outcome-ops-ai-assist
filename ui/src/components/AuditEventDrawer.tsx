import { useEffect } from 'react'
import { X, History } from 'lucide-react'
import type { AuditEvent } from '../lib/api'

interface AuditEventDrawerProps {
  event: AuditEvent | null
  onClose: () => void
  /** Optional: when present, the drawer renders a "View user's chat history"
   * button that delegates to the parent so the page can scope its Recent
   * Activity to that user. Used by the InfoSec-drill flow ("system blocked
   * Joe — let me see what Joe's been doing in the lead-up"). */
  onViewUserHistory?: (userEmail: string) => void
}

/**
 * Right-side drawer surfacing a full audit row. The 480px width and the
 * IP / user-agent / request-id triad are deliberate: this is the screen
 * the InfoSec demo focuses on, so they need to be one click and one copy
 * away from any row.
 */
export default function AuditEventDrawer({
  event,
  onClose,
  onViewUserHistory,
}: AuditEventDrawerProps) {
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

  const isRefusal = event.status === 'refusal'
  const isIngest = event.action === 'DOCUMENT_INGESTED'
  const details = (event.details ?? {}) as Record<string, unknown>
  const redactionSummary = details.redaction_summary as
    | Record<string, number>
    | undefined
  const skippedReason = details.skipped_reason as string | undefined
  const headerLabel = isRefusal
    ? 'REFUSAL'
    : (event.action || (event.category === 'bedrock' ? 'BEDROCK_CALL' : 'UNKNOWN'))
  const headerAccent = isRefusal
    ? 'text-rose-700'
    : event.category === 'bedrock'
      ? 'text-purple-700'
      : 'text-gray-900'

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
            <div className={`text-lg font-semibold ${headerAccent}`}>{headerLabel}</div>
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
          {isRefusal && event.flagged_reason && (
            <div className="rounded-lg border border-rose-200 bg-rose-50 p-3">
              <div className="text-xs uppercase text-rose-700 font-medium mb-1">
                Refusal reason
              </div>
              <div className="text-sm text-rose-900 break-words">
                {event.flagged_reason}
              </div>
              {event.model_id && (
                <div className="text-xs text-rose-700 mt-2">
                  Model: <span className="font-mono">{event.model_id}</span>
                </div>
              )}
              {event.lambda_name && (
                <div className="text-xs text-rose-700">
                  Source: <span className="font-mono">{event.lambda_name}</span>
                </div>
              )}
            </div>
          )}

          {isIngest && redactionSummary && Object.keys(redactionSummary).length > 0 && (
            <div className="rounded-lg border border-teal-200 bg-teal-50 p-3">
              <div className="text-xs uppercase text-teal-700 font-medium mb-1">
                PII redacted on ingest
              </div>
              <div className="space-y-1 mt-2">
                {Object.entries(redactionSummary).map(([type, count]) => (
                  <div
                    key={type}
                    className="flex items-center justify-between text-xs text-teal-900"
                  >
                    <span className="font-mono">{type}</span>
                    <span>{count}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {isIngest && skippedReason && (
            <div className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-xs text-gray-700">
              Redaction skipped:{' '}
              <span className="font-mono">{skippedReason}</span>
            </div>
          )}

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

          {/* Bedrock rows (chat / RAG / codegen, refusal or not) carry
              the full prompt + response that shared/audit.py captures.
              Show them whenever they're present so an admin drilling
              into a refusal can also pivot to neighboring chat rows
              from the same user and see what triggered the block. */}
          {event.input_text && (
            <div>
              <div className="text-xs uppercase text-gray-500 mb-1">User prompt</div>
              <pre className="text-xs font-mono bg-gray-50 border rounded p-2 whitespace-pre-wrap break-words max-h-48 overflow-y-auto">
                {event.input_text}
              </pre>
            </div>
          )}
          {event.output_text && (
            <div>
              <div className="text-xs uppercase text-gray-500 mb-1">Model response</div>
              <pre className="text-xs font-mono bg-gray-50 border rounded p-2 whitespace-pre-wrap break-words max-h-48 overflow-y-auto">
                {event.output_text}
              </pre>
            </div>
          )}

          {onViewUserHistory && event.user_email && (
            <button
              onClick={() => onViewUserHistory(event.user_email)}
              className="w-full inline-flex items-center justify-center gap-2 px-3 py-2 text-sm rounded-lg border border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100 transition-colors"
            >
              <History size={14} />
              View {event.user_email}'s chat history
            </button>
          )}

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
