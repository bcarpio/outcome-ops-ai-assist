import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ShieldAlert,
  Loader2,
  AlertCircle,
  Activity,
  Users,
  Filter,
  AlertTriangle,
} from 'lucide-react'
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from 'recharts'
import AdminNav from '../components/AdminNav'
import AuditEventDrawer from '../components/AuditEventDrawer'
import {
  getAuditSummary,
  getAuditEvents,
  type AuditSummary,
  type AuditEvent,
  type AuditEventsPage,
} from '../lib/api'

type DateRangeOption = 7 | 30 | 90

// Per chunk 07: this set is the InfoSec sound-bite metric on the page.
// Refusals are layered on top via status="refusal" in the merged feed.
const CRITICAL_ACTIONS = new Set([
  'API_KEY_CREATED',
  'API_KEY_REVOKED',
  'WORKSPACE_DELETED',
  'MCP_SERVER_DELETED',
  'ORG_MEMBER_ADDED',
])

// Action-to-palette map. Inspired by the Analytics page's recharts colors,
// kept narrow so the table stays readable. Each entry yields a Tailwind
// class pair: a light bg + a darker text/border tone for an inline pill.
type Tone = {
  bg: string
  text: string
  ring: string
  /** `stroke` is consumed by recharts elsewhere in the page; included here
   * so a future per-action sparkline can pull from the same map. */
  stroke: string
}

const TONES: Record<string, Tone> = {
  workspace: { bg: 'bg-sky-50',     text: 'text-sky-700',     ring: 'ring-sky-200',     stroke: '#0ea5e9' },
  member:    { bg: 'bg-emerald-50', text: 'text-emerald-700', ring: 'ring-emerald-200', stroke: '#10b981' },
  repo:      { bg: 'bg-indigo-50',  text: 'text-indigo-700',  ring: 'ring-indigo-200',  stroke: '#6366f1' },
  mcp:       { bg: 'bg-purple-50',  text: 'text-purple-700',  ring: 'ring-purple-200',  stroke: '#a855f7' },
  apikey:    { bg: 'bg-amber-50',   text: 'text-amber-800',   ring: 'ring-amber-200',   stroke: '#f59e0b' },
  budget:    { bg: 'bg-teal-50',    text: 'text-teal-700',    ring: 'ring-teal-200',    stroke: '#14b8a6' },
  prompt:    { bg: 'bg-fuchsia-50', text: 'text-fuchsia-700', ring: 'ring-fuchsia-200', stroke: '#d946ef' },
  refusal:   { bg: 'bg-rose-50',    text: 'text-rose-700',    ring: 'ring-rose-300',    stroke: '#e11d48' },
  unknown:   { bg: 'bg-gray-50',    text: 'text-gray-700',    ring: 'ring-gray-200',    stroke: '#6b7280' },
}

function toneFor(ev: { action?: string; category?: string; status?: string }): Tone {
  if (ev.status === 'refusal') return TONES.refusal
  const a = ev.action || ''
  if (a.startsWith('API_KEY')) return TONES.apikey
  if (a.startsWith('WORKSPACE_BUDGET')) return TONES.budget
  if (a.startsWith('WORKSPACE_MEMBER') || a.startsWith('ORG_MEMBER')) return TONES.member
  if (a.startsWith('WORKSPACE_REPO')) return TONES.repo
  if (a.startsWith('WORKSPACE_')) return TONES.workspace
  if (a.startsWith('MCP_SERVER')) return TONES.mcp
  if (a.startsWith('SYSTEM_PROMPT')) return TONES.prompt
  return TONES.unknown
}

function isoDaysAgo(days: number): string {
  const d = new Date()
  d.setUTCDate(d.getUTCDate() - days)
  return d.toISOString()
}

function formatNumber(n: number): string {
  return n.toLocaleString()
}

function formatTimestamp(iso: string): string {
  if (!iso) return '—'
  return iso.replace('T', ' ').replace(/\..*$/, '')
}

function isRefusal(ev: AuditEvent): boolean {
  // Refusals are bedrock-category rows where shared.audit.detect_refusal
  // set status="refusal". The handler now projects status/flagged_reason
  // onto the public envelope as top-level optional fields.
  return ev.status === 'refusal'
}

function actionLabel(ev: AuditEvent): string {
  if (isRefusal(ev)) return 'REFUSAL'
  return ev.action || (ev.category === 'bedrock' ? 'BEDROCK_CALL' : '—')
}

export default function Audit() {
  const navigate = useNavigate()
  const [range, setRange] = useState<DateRangeOption>(30)
  const [summary, setSummary] = useState<AuditSummary | null>(null)
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [refusalCount, setRefusalCount] = useState<number>(0)
  const [adminCursor, setAdminCursor] = useState<string | null>(null)
  const [actionFilter, setActionFilter] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [eventsLoading, setEventsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [drawerEvent, setDrawerEvent] = useState<AuditEvent | null>(null)

  const startDate = useMemo(() => isoDaysAgo(range), [range])
  const endDate = useMemo(() => new Date().toISOString(), [range])

  // Initial summary + first page of admin events + refusals (parallel).
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    Promise.all([
      getAuditSummary(startDate, endDate),
      getAuditEvents({
        startDate,
        endDate,
        action: actionFilter || undefined,
        category: 'admin',
        limit: 50,
      }),
      // Refusals live as bedrock rows with status="refusal". One extra
      // call surfaces them inline with admin actions in the same feed.
      getAuditEvents({
        startDate,
        endDate,
        category: 'bedrock',
        status: 'refusal',
        limit: 25,
      }),
    ])
      .then(([sum, adminPage, refusalPage]: [AuditSummary, AuditEventsPage, AuditEventsPage]) => {
        if (cancelled) return
        setSummary(sum)
        setRefusalCount(refusalPage.events.length)
        const merged = [...adminPage.events, ...refusalPage.events].sort(
          (a, b) => (a.timestamp < b.timestamp ? 1 : -1),
        )
        setEvents(merged)
        setAdminCursor(adminPage.next_cursor)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        const msg = e instanceof Error ? e.message : 'Failed to load audit data'
        setError(msg)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [startDate, endDate, actionFilter])

  const loadMore = async () => {
    if (!adminCursor) return
    setEventsLoading(true)
    try {
      const page = await getAuditEvents({
        startDate,
        endDate,
        action: actionFilter || undefined,
        category: 'admin',
        cursor: adminCursor,
        limit: 50,
      })
      setEvents(prev =>
        [...prev, ...page.events].sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1)),
      )
      setAdminCursor(page.next_cursor)
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to load more events'
      setError(msg)
    } finally {
      setEventsLoading(false)
    }
  }

  const criticalCount = useMemo(() => {
    if (!summary) return refusalCount
    const adminCritical = summary.actions_by_type
      .filter(a => CRITICAL_ACTIONS.has(a.action))
      .reduce((acc, a) => acc + a.count, 0)
    // Refusals roll into the same "things InfoSec wants to see at a glance"
    // count. ToS refusal is a security signal even though the user's intent
    // got blocked at the model layer.
    return adminCritical + refusalCount
  }, [summary, refusalCount])

  const mostCommon = useMemo(() => {
    if (!summary || !summary.actions_by_type.length) return '—'
    return summary.actions_by_type[0].action
  }, [summary])

  const activeActors = summary?.top_actors.length ?? 0

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b">
        <div className="max-w-7xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <ShieldAlert size={24} className="text-blue-600" />
            <h1 className="text-xl font-semibold">Audit</h1>
          </div>
          <AdminNav />
        </div>
      </header>

      <main className="max-w-7xl mx-auto p-4 space-y-6">
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-lg border bg-white overflow-hidden">
            {[7, 30, 90].map(d => (
              <button
                key={d}
                onClick={() => setRange(d as DateRangeOption)}
                className={`px-3 py-1.5 text-sm transition-colors ${
                  range === d
                    ? 'bg-blue-600 text-white'
                    : 'text-gray-700 hover:bg-gray-100'
                }`}
              >
                Last {d} days
              </button>
            ))}
          </div>
          {actionFilter && (
            <button
              onClick={() => setActionFilter('')}
              className="ml-auto inline-flex items-center gap-1 px-2 py-1 text-xs rounded-full bg-blue-50 border border-blue-200 text-blue-700"
              aria-label={`Clear action filter ${actionFilter}`}
            >
              <Filter size={12} /> {actionFilter} ✕
            </button>
          )}
        </div>

        {loading && (
          <div className="flex items-center gap-2 text-blue-600" role="status">
            <Loader2 className="animate-spin" size={32} /> Loading audit data…
          </div>
        )}

        {error && (
          <div
            className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-2 text-red-700"
            role="alert"
          >
            <AlertCircle size={18} />
            <div className="text-sm">
              {error}{' '}
              <button onClick={() => setRange(range)} className="underline ml-2">
                Retry
              </button>
            </div>
          </div>
        )}

        {!loading && !error && summary && (
          <>
            <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
              <StatCard
                label="Total Actions"
                value={formatNumber(summary.actions_total + refusalCount)}
                icon={<Activity size={18} className="text-blue-500" />}
                accent="blue"
              />
              <StatCard
                label="Most Common Action"
                value={mostCommon}
                accent="indigo"
                kind="action"
              />
              <StatCard
                label="Active Actors"
                value={formatNumber(activeActors)}
                icon={<Users size={18} className="text-emerald-500" />}
                accent="emerald"
              />
              <StatCard
                label="Critical Actions"
                value={formatNumber(criticalCount)}
                icon={<AlertTriangle size={18} className="text-rose-500" />}
                accent={criticalCount > 0 ? 'danger' : 'neutral'}
              />
            </section>

            <section className="bg-white border rounded-lg shadow-sm p-4">
              <div className="text-sm font-medium mb-2 text-gray-700">Daily Activity</div>
              {summary.trend.length === 0 ? (
                <EmptyState
                  title="No actions yet"
                  hint="Audit captures workspace, member, repo, MCP, API key changes — and Bedrock refusals."
                />
              ) : (
                <ResponsiveContainer width="100%" height={200}>
                  <LineChart data={summary.trend}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
                    <XAxis dataKey="date" stroke="#9ca3af" tick={{ fontSize: 12 }} />
                    <YAxis stroke="#9ca3af" tick={{ fontSize: 12 }} allowDecimals={false} />
                    <Tooltip
                      contentStyle={{
                        background: 'white',
                        border: '1px solid #e5e7eb',
                        borderRadius: 8,
                        fontSize: 12,
                      }}
                    />
                    <Line
                      type="monotone"
                      dataKey="count"
                      stroke="#3b82f6"
                      strokeWidth={2}
                      dot={{ fill: '#3b82f6', r: 3 }}
                      activeDot={{ r: 5 }}
                    />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </section>

            <section className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <div className="bg-white border rounded-lg shadow-sm p-4">
                <div className="text-sm font-medium mb-3 text-gray-700">Actions by Type</div>
                {summary.actions_by_type.length === 0 ? (
                  <div className="text-sm text-gray-500">No actions in range.</div>
                ) : (
                  <div className="space-y-2">
                    {summary.actions_by_type.slice(0, 10).map(a => {
                      const t = toneFor({ action: a.action })
                      const max = summary.actions_by_type[0].count || 1
                      const pct = Math.max(4, (a.count / max) * 100)
                      return (
                        <button
                          key={a.action}
                          onClick={() => setActionFilter(a.action)}
                          className="w-full group text-left"
                        >
                          <div className="flex items-center justify-between text-xs mb-0.5">
                            <span className={`font-mono ${t.text} truncate pr-2`}>
                              {a.action}
                            </span>
                            <span className="text-gray-600">{formatNumber(a.count)}</span>
                          </div>
                          <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
                            <div
                              className="h-full transition-all"
                              style={{ width: `${pct}%`, background: t.stroke }}
                            />
                          </div>
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>

              <div className="bg-white border rounded-lg shadow-sm p-4">
                <div className="text-sm font-medium mb-3 text-gray-700">Actions by Workspace</div>
                {summary.actions_by_workspace.length === 0 ? (
                  <div className="text-sm text-gray-500">No actions in range.</div>
                ) : (
                  <div className="space-y-2">
                    {summary.actions_by_workspace.slice(0, 10).map(w => {
                      const max = summary.actions_by_workspace[0].count || 1
                      const pct = Math.max(4, (w.count / max) * 100)
                      return (
                        <button
                          key={w.workspace_id}
                          onClick={() =>
                            navigate(
                              `/audit/workspace/${encodeURIComponent(w.workspace_id)}`,
                            )
                          }
                          className="w-full group text-left"
                        >
                          <div className="flex items-center justify-between text-xs mb-0.5">
                            <span className="text-gray-700 group-hover:text-blue-600 truncate pr-2 transition-colors">
                              {w.workspace_name || w.workspace_id}
                            </span>
                            <span className="text-gray-600">{formatNumber(w.count)}</span>
                          </div>
                          <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
                            <div
                              className="h-full bg-sky-500 group-hover:bg-sky-600 transition-colors"
                              style={{ width: `${pct}%` }}
                            />
                          </div>
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            </section>

            <section className="bg-white border rounded-lg shadow-sm p-4">
              <div className="text-sm font-medium mb-2 text-gray-700">Top Actors</div>
              {summary.top_actors.length === 0 ? (
                <div className="text-sm text-gray-500">No actions in range.</div>
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase text-gray-500 border-b">
                      <th className="py-2">User</th>
                      <th className="py-2 text-right">Actions</th>
                      <th className="py-2 text-right">Last Active</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.top_actors.slice(0, 25).map(a => (
                      <tr key={a.user_email} className="border-b last:border-0 hover:bg-gray-50">
                        <td className="py-2 text-gray-800">{a.user_email}</td>
                        <td className="py-2 text-right font-mono text-emerald-700">
                          {formatNumber(a.count)}
                        </td>
                        <td
                          className="py-2 text-right text-gray-500 text-xs"
                          title={a.last_active}
                        >
                          {formatTimestamp(a.last_active)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            <section className="bg-white border rounded-lg shadow-sm p-4">
              <div className="text-sm font-medium mb-2 text-gray-700">Recent Activity</div>
              {events.length === 0 ? (
                <EmptyState
                  title="No actions yet"
                  hint="Audit captures workspace, member, repo, MCP, API key changes — and Bedrock refusals."
                />
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase text-gray-500 border-b">
                      <th className="py-2">Time</th>
                      <th className="py-2">Actor</th>
                      <th className="py-2">Action</th>
                      <th className="py-2">Workspace</th>
                      <th className="py-2">IP</th>
                    </tr>
                  </thead>
                  <tbody>
                    {events.map(ev => {
                      const t = toneFor({
                        action: ev.action,
                        category: ev.category,
                        status: ev.status,
                      })
                      const label = actionLabel(ev)
                      return (
                        <tr
                          key={ev.request_id}
                          className="border-b last:border-0 hover:bg-gray-50 cursor-pointer"
                          onClick={() => setDrawerEvent(ev)}
                        >
                          <td
                            className="py-2 text-xs text-gray-500"
                            title={ev.timestamp}
                          >
                            {formatTimestamp(ev.timestamp)}
                          </td>
                          <td className="py-2 text-gray-800">{ev.user_email || '—'}</td>
                          <td className="py-2">
                            <span
                              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-mono ring-1 ${t.bg} ${t.text} ${t.ring}`}
                            >
                              {label === 'REFUSAL' && <AlertTriangle size={11} />}
                              {label}
                            </span>
                          </td>
                          <td className="py-2 text-gray-700 text-xs">
                            {ev.workspace_id || '—'}
                          </td>
                          <td className="py-2 font-mono text-xs text-gray-700">
                            {ev.source_ip || '—'}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              )}

              {adminCursor && (
                <div className="mt-3 text-center">
                  <button
                    onClick={loadMore}
                    disabled={eventsLoading}
                    className="px-3 py-1.5 text-sm border rounded-lg bg-white hover:bg-gray-50 disabled:opacity-50 transition-colors"
                  >
                    {eventsLoading ? 'Loading…' : 'Load more'}
                  </button>
                </div>
              )}
            </section>
          </>
        )}
      </main>

      <AuditEventDrawer
        event={drawerEvent}
        onClose={() => setDrawerEvent(null)}
      />
    </div>
  )
}

interface StatCardProps {
  label: string
  value: string
  icon?: React.ReactNode
  accent?: 'neutral' | 'danger' | 'blue' | 'indigo' | 'emerald'
  /** "number" cards use the big 2xl numeral; "action" cards render the
   *  value as a smaller monospace identifier so long action names like
   *  WORKSPACE_BUDGET_UPDATED don't blow out the card height. */
  kind?: 'number' | 'action'
}

function StatCard({ label, value, icon, accent = 'neutral', kind = 'number' }: StatCardProps) {
  const accentClass = {
    neutral:  'border-gray-200 bg-white',
    danger:   'border-rose-300 bg-rose-50',
    blue:     'border-blue-200 bg-blue-50',
    indigo:   'border-indigo-200 bg-indigo-50',
    emerald:  'border-emerald-200 bg-emerald-50',
  }[accent]
  const valueClass =
    kind === 'action'
      ? 'text-sm font-mono font-medium mt-2 break-all text-indigo-700'
      : 'text-2xl font-semibold mt-1 break-all text-gray-900'
  return (
    <div className={`border rounded-lg shadow-sm p-3 ${accentClass}`}>
      <div className="flex items-center justify-between text-xs uppercase text-gray-600 font-medium">
        <span>{label}</span>
        {icon}
      </div>
      <div className={valueClass}>{value}</div>
    </div>
  )
}

interface EmptyStateProps {
  title: string
  hint: string
}

function EmptyState({ title, hint }: EmptyStateProps) {
  return (
    <div className="text-center py-6">
      <div className="text-base font-medium text-gray-700">{title}</div>
      <div className="text-sm text-gray-500 mt-1">{hint}</div>
    </div>
  )
}
