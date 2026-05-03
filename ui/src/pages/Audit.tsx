import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ShieldAlert,
  Loader2,
  AlertCircle,
  Activity,
  Users,
  Filter,
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
// Keep in sync with shared/audit_writer.py if either side changes.
const CRITICAL_ACTIONS = new Set([
  'API_KEY_CREATED',
  'API_KEY_REVOKED',
  'WORKSPACE_DELETED',
  'MCP_SERVER_DELETED',
  'ORG_MEMBER_ADDED',
])

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

export default function Audit() {
  const navigate = useNavigate()
  const [range, setRange] = useState<DateRangeOption>(30)
  const [summary, setSummary] = useState<AuditSummary | null>(null)
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [actionFilter, setActionFilter] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [eventsLoading, setEventsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [drawerEvent, setDrawerEvent] = useState<AuditEvent | null>(null)

  const startDate = useMemo(() => isoDaysAgo(range), [range])
  const endDate = useMemo(() => new Date().toISOString(), [range])

  // Initial summary + first page of events
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
        limit: 50,
      }),
    ])
      .then(([sum, page]: [AuditSummary, AuditEventsPage]) => {
        if (cancelled) return
        setSummary(sum)
        setEvents(page.events)
        setCursor(page.next_cursor)
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
    if (!cursor) return
    setEventsLoading(true)
    try {
      const page = await getAuditEvents({
        startDate,
        endDate,
        action: actionFilter || undefined,
        cursor,
        limit: 50,
      })
      setEvents(prev => [...prev, ...page.events])
      setCursor(page.next_cursor)
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to load more events'
      setError(msg)
    } finally {
      setEventsLoading(false)
    }
  }

  const criticalCount = useMemo(() => {
    if (!summary) return 0
    return summary.actions_by_type
      .filter(a => CRITICAL_ACTIONS.has(a.action))
      .reduce((acc, a) => acc + a.count, 0)
  }, [summary])

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
            <ShieldAlert size={22} className="text-gray-700" />
            <h1 className="text-xl font-semibold">Audit</h1>
          </div>
          <AdminNav />
        </div>
      </header>

      <main className="max-w-7xl mx-auto p-4 space-y-6">
        <div className="flex flex-wrap items-center gap-2">
          {[7, 30, 90].map(d => (
            <button
              key={d}
              onClick={() => setRange(d as DateRangeOption)}
              className={`px-3 py-1 text-sm rounded border ${
                range === d
                  ? 'bg-gray-900 text-white border-gray-900'
                  : 'bg-white text-gray-700 border-gray-300 hover:bg-gray-50'
              }`}
            >
              Last {d} days
            </button>
          ))}
          {actionFilter && (
            <button
              onClick={() => setActionFilter('')}
              className="ml-auto inline-flex items-center gap-1 px-2 py-1 text-xs rounded bg-blue-50 border border-blue-200 text-blue-700"
              aria-label={`Clear action filter ${actionFilter}`}
            >
              <Filter size={12} /> {actionFilter} ✕
            </button>
          )}
        </div>

        {loading && (
          <div className="flex items-center gap-2 text-gray-500" role="status">
            <Loader2 className="animate-spin" size={18} /> Loading audit data…
          </div>
        )}

        {error && (
          <div
            className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 text-red-800 rounded"
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
                value={formatNumber(summary.actions_total)}
                icon={<Activity size={18} />}
              />
              <StatCard label="Most Common Action" value={mostCommon} />
              <StatCard
                label="Active Actors"
                value={formatNumber(activeActors)}
                icon={<Users size={18} />}
              />
              <StatCard
                label="Critical Actions"
                value={formatNumber(criticalCount)}
                accent={criticalCount > 0 ? 'danger' : 'neutral'}
              />
            </section>

            <section className="bg-white border rounded p-4">
              <div className="text-sm font-medium mb-2">Daily Activity</div>
              {summary.trend.length === 0 ? (
                <EmptyState
                  title="No actions yet"
                  hint="Audit captures workspace, member, repo, MCP, and API key changes."
                />
              ) : (
                <ResponsiveContainer width="100%" height={200}>
                  <LineChart data={summary.trend}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis dataKey="date" />
                    <YAxis />
                    <Tooltip />
                    <Line type="monotone" dataKey="count" stroke="#1e3a8a" />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </section>

            <section className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <div className="bg-white border rounded p-4">
                <div className="text-sm font-medium mb-2">Actions by Type</div>
                {summary.actions_by_type.length === 0 ? (
                  <div className="text-sm text-gray-500">No actions in range.</div>
                ) : (
                  <table className="w-full text-sm">
                    <tbody>
                      {summary.actions_by_type.slice(0, 10).map(a => (
                        <tr
                          key={a.action}
                          className="border-b last:border-0 hover:bg-gray-50 cursor-pointer"
                          onClick={() => setActionFilter(a.action)}
                        >
                          <td className="py-1 font-mono text-xs">{a.action}</td>
                          <td className="py-1 text-right">{formatNumber(a.count)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>

              <div className="bg-white border rounded p-4">
                <div className="text-sm font-medium mb-2">Actions by Workspace</div>
                {summary.actions_by_workspace.length === 0 ? (
                  <div className="text-sm text-gray-500">No actions in range.</div>
                ) : (
                  <table className="w-full text-sm">
                    <tbody>
                      {summary.actions_by_workspace.slice(0, 10).map(w => (
                        <tr
                          key={w.workspace_id}
                          className="border-b last:border-0 hover:bg-gray-50 cursor-pointer"
                          onClick={() =>
                            navigate(
                              `/audit/workspace/${encodeURIComponent(w.workspace_id)}`,
                            )
                          }
                        >
                          <td className="py-1">{w.workspace_name || w.workspace_id}</td>
                          <td className="py-1 text-right">{formatNumber(w.count)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </section>

            <section className="bg-white border rounded p-4">
              <div className="text-sm font-medium mb-2">Top Actors</div>
              {summary.top_actors.length === 0 ? (
                <div className="text-sm text-gray-500">No actions in range.</div>
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase text-gray-500">
                      <th className="py-1">User</th>
                      <th className="py-1 text-right">Actions</th>
                      <th className="py-1 text-right">Last Active</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.top_actors.slice(0, 25).map(a => (
                      <tr key={a.user_email} className="border-b last:border-0">
                        <td className="py-1">{a.user_email}</td>
                        <td className="py-1 text-right">{formatNumber(a.count)}</td>
                        <td
                          className="py-1 text-right text-gray-500 text-xs"
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

            <section className="bg-white border rounded p-4">
              <div className="text-sm font-medium mb-2">Recent Activity</div>
              {events.length === 0 ? (
                <EmptyState
                  title="No actions yet"
                  hint="Audit captures workspace, member, repo, MCP, and API key changes."
                />
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase text-gray-500">
                      <th className="py-1">Time</th>
                      <th className="py-1">Actor</th>
                      <th className="py-1">Action</th>
                      <th className="py-1">Workspace</th>
                      <th className="py-1">IP</th>
                    </tr>
                  </thead>
                  <tbody>
                    {events.map(ev => (
                      <tr
                        key={ev.request_id}
                        className="border-b last:border-0 hover:bg-gray-50 cursor-pointer"
                        onClick={() => setDrawerEvent(ev)}
                      >
                        <td
                          className="py-1 text-xs text-gray-500"
                          title={ev.timestamp}
                        >
                          {formatTimestamp(ev.timestamp)}
                        </td>
                        <td className="py-1">{ev.user_email || '—'}</td>
                        <td className="py-1 font-mono text-xs">{ev.action || '—'}</td>
                        <td className="py-1 text-gray-700 text-xs">
                          {ev.workspace_id || '—'}
                        </td>
                        <td className="py-1 font-mono text-xs">
                          {ev.source_ip || '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              {cursor && (
                <div className="mt-3 text-center">
                  <button
                    onClick={loadMore}
                    disabled={eventsLoading}
                    className="px-3 py-1 text-sm border rounded bg-white hover:bg-gray-50 disabled:opacity-50"
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
  accent?: 'neutral' | 'danger'
}

function StatCard({ label, value, icon, accent = 'neutral' }: StatCardProps) {
  const accentClass =
    accent === 'danger' ? 'border-red-300 bg-red-50' : 'border-gray-200 bg-white'
  return (
    <div className={`border rounded p-3 ${accentClass}`}>
      <div className="flex items-center justify-between text-xs uppercase text-gray-500">
        <span>{label}</span>
        {icon}
      </div>
      <div className="text-xl font-semibold mt-1 break-all">{value}</div>
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
