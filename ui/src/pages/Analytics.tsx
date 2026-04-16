import { useState, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  BarChart3,
  Loader2,
  MessageSquare,
  Folder,
  Shield,
  Settings,
  LogOut,
  AlertCircle,
  TrendingUp,
  ArrowUpRight,
  ArrowDownRight,
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
import {
  getAnalyticsSummary,
  getAnalyticsUsers,
  getSearchableWorkspaces,
  type AnalyticsSummary,
  type AnalyticsUser,
  type SearchableWorkspace,
  type AnalyticsDailyPoint,
} from '../lib/api'

type DateRangeOption = 30 | 60 | 90

function formatCost(value: number): string {
  if (value >= 1000) return `$${value.toFixed(0)}`
  if (value >= 1) return `$${value.toFixed(2)}`
  return `$${value.toFixed(4)}`
}

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`
  return String(value)
}

function formatNumber(value: number): string {
  return value.toLocaleString()
}

function shortenModelName(modelId: string): string {
  return modelId
    .replace(/^us\.anthropic\./, '')
    .replace(/^anthropic\./, '')
    .replace(/-v\d+:\d+$/, '')
}

function formatLastActive(iso: string): string {
  if (!iso) return '—'
  const then = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - then.getTime()
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24))
  if (diffDays === 0) return 'Today'
  if (diffDays === 1) return 'Yesterday'
  if (diffDays < 7) return `${diffDays}d ago`
  return then.toISOString().slice(0, 10)
}

function isoDaysAgo(days: number): string {
  const d = new Date()
  d.setUTCDate(d.getUTCDate() - days)
  return d.toISOString()
}

interface WoWDelta {
  current: number
  prior: number
  pct: number | null
}

interface WoWDeltas {
  cost: WoWDelta
  queries: WoWDelta
  tokens: WoWDelta
}

function computeWoWDeltas(trend: AnalyticsDailyPoint[] | undefined): WoWDeltas | null {
  if (!trend || trend.length < 2) return null
  // Use the last 7 days vs the 7 days before that. Older data is ignored so
  // the comparison stays interpretable regardless of the selected range.
  const sorted = [...trend].sort((a, b) => (a.date < b.date ? -1 : 1))
  const tail = sorted.slice(-14)
  if (tail.length < 2) return null
  const half = Math.floor(tail.length / 2)
  const prior = tail.slice(0, half)
  const current = tail.slice(half)
  if (prior.length === 0 || current.length === 0) return null

  const sum = (pts: AnalyticsDailyPoint[], key: keyof AnalyticsDailyPoint) =>
    pts.reduce((acc, p) => acc + Number(p[key] || 0), 0)

  // daily_trend today only carries cost_usd + queries; tokens are totals-only,
  // so WoW delta for tokens is not available from this response shape.
  const costCur = sum(current, 'cost_usd')
  const costPrior = sum(prior, 'cost_usd')
  const qCur = sum(current, 'queries')
  const qPrior = sum(prior, 'queries')

  const pct = (cur: number, prev: number): number | null => {
    if (prev === 0) return cur === 0 ? 0 : null
    return (cur - prev) / prev
  }

  return {
    cost: { current: costCur, prior: costPrior, pct: pct(costCur, costPrior) },
    queries: { current: qCur, prior: qPrior, pct: pct(qCur, qPrior) },
    tokens: { current: 0, prior: 0, pct: null },
  }
}

export default function Analytics() {
  const navigate = useNavigate()
  const [range, setRange] = useState<DateRangeOption>(30)
  const [workspaceFilter, setWorkspaceFilter] = useState<string>('')
  const [workspaces, setWorkspaces] = useState<SearchableWorkspace[]>([])
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null)
  const [users, setUsers] = useState<AnalyticsUser[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const isOrgAdmin = useMemo(
    () => workspaces.some(ws => ws.access_type === 'org_admin'),
    [workspaces],
  )

  useEffect(() => {
    getSearchableWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces([]))
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    const startDate = isoDaysAgo(range)
    const wsId = workspaceFilter || undefined

    Promise.all([
      getAnalyticsSummary(startDate, undefined, wsId),
      getAnalyticsUsers(startDate, undefined, wsId, 10).catch(() => ({ users: [] })),
    ])
      .then(([summaryData, userData]) => {
        if (cancelled) return
        setSummary(summaryData)
        setUsers(userData.users)
      })
      .catch(err => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Failed to load analytics')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [range, workspaceFilter])

  const maxWorkspaceCost = useMemo(() => {
    if (!summary?.by_workspace.length) return 0
    return Math.max(...summary.by_workspace.map(w => w.cost_usd))
  }, [summary])

  const sortedModels = useMemo(() => {
    if (!summary) return []
    return Object.entries(summary.by_model)
      .map(([model, data]) => ({ model, ...data }))
      .sort((a, b) => b.cost_usd - a.cost_usd)
  }, [summary])

  const maxModelCost = useMemo(() => {
    if (!sortedModels.length) return 0
    return Math.max(...sortedModels.map(m => m.cost_usd))
  }, [sortedModels])

  // Week-over-week deltas on cost / queries / tokens — computed from the
  // trailing 14 days of the daily_trend series. Returns null when there's
  // not enough data in the prior window to compare meaningfully.
  const deltas = useMemo(() => computeWoWDeltas(summary?.daily_trend), [summary])

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b">
        <div className="max-w-6xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <BarChart3 className="text-blue-600" size={24} />
            <h1 className="text-xl font-semibold">Analytics</h1>
          </div>
          <div className="flex items-center gap-3">
            <div className="inline-flex rounded-lg border bg-white overflow-hidden">
              {([30, 60, 90] as DateRangeOption[]).map(days => (
                <button
                  key={days}
                  onClick={() => setRange(days)}
                  className={`px-3 py-1.5 text-sm transition-colors ${
                    range === days
                      ? 'bg-blue-600 text-white'
                      : 'text-gray-700 hover:bg-gray-100'
                  }`}
                >
                  {days}d
                </button>
              ))}
            </div>
            {isOrgAdmin && workspaces.length > 0 && (
              <select
                value={workspaceFilter}
                onChange={e => setWorkspaceFilter(e.target.value)}
                className="px-3 py-1.5 text-sm border rounded-lg bg-white"
                aria-label="Filter by workspace"
              >
                <option value="">All workspaces</option>
                {workspaces.map(ws => (
                  <option key={ws.workspace_id} value={ws.workspace_id}>
                    {ws.name}
                  </option>
                ))}
              </select>
            )}
            <button
              onClick={() => navigate('/')}
              title="Chat"
              className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
            >
              <MessageSquare size={20} />
            </button>
            <button
              onClick={() => navigate('/workspaces')}
              title="Workspaces"
              className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
            >
              <Folder size={20} />
            </button>
            <button
              onClick={() => navigate('/settings')}
              title="Settings"
              className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
            >
              <Settings size={20} />
            </button>
            {isOrgAdmin && (
              <button
                onClick={() => navigate('/org/settings')}
                title="Organization Settings"
                className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
              >
                <Shield size={20} />
              </button>
            )}
            <button
              onClick={() => { window.location.href = '/api/logout' }}
              title="Sign out"
              className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
            >
              <LogOut size={20} />
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 py-8">
        {loading ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="animate-spin text-blue-600" size={32} />
          </div>
        ) : error ? (
          <div className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-2 text-red-700">
            <AlertCircle size={20} className="shrink-0 mt-0.5" />
            <div>
              <div className="font-medium">Failed to load analytics</div>
              <div className="text-sm text-red-600 mt-1">{error}</div>
            </div>
          </div>
        ) : !summary ? (
          <div className="text-center py-16 text-gray-500">No analytics data available.</div>
        ) : (
          <div className="space-y-6">
            {/* Stat cards */}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
              <StatCard
                label="Total Cost"
                value={formatCost(summary.total_cost_usd)}
                sublabel={`last ${range} days`}
                delta={deltas?.cost}
                deltaDirection="higher-is-bad"
              />
              <StatCard
                label="Queries"
                value={formatNumber(summary.total_queries)}
                sublabel={`${summary.total_queries === 0 ? 'no activity' : 'total'}`}
                delta={deltas?.queries}
                deltaDirection="higher-is-good"
              />
              <StatCard
                label="Input Tokens"
                value={formatTokens(summary.total_input_tokens)}
                sublabel={`${formatTokens(summary.total_output_tokens)} out`}
              />
              <StatCard
                label="Avg Cost/Query"
                value={
                  summary.total_queries > 0
                    ? formatCost(summary.total_cost_usd / summary.total_queries)
                    : '$0.00'
                }
                sublabel="efficiency"
              />
            </div>

            {/* Daily cost trend */}
            <section className="bg-white border rounded-lg shadow-sm p-4">
              <div className="flex items-center gap-2 mb-4">
                <TrendingUp size={16} className="text-gray-500" />
                <h2 className="font-medium text-gray-900">
                  Daily Cost Trend
                </h2>
                <span className="text-sm text-gray-500 ml-1">
                  (last {range} days)
                </span>
              </div>
              {summary.daily_trend.length === 0 ? (
                <p className="text-sm text-gray-500 py-4">No activity in this range.</p>
              ) : (
                <div className="h-56">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={summary.daily_trend} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
                      <XAxis
                        dataKey="date"
                        stroke="#9ca3af"
                        fontSize={11}
                        tickFormatter={d => d.slice(5)}
                        minTickGap={20}
                      />
                      <YAxis
                        stroke="#9ca3af"
                        fontSize={11}
                        tickFormatter={v => `$${v.toFixed(2)}`}
                        width={60}
                      />
                      <Tooltip content={<TrendTooltip />} />
                      <Line
                        type="monotone"
                        dataKey="cost_usd"
                        stroke="#3b82f6"
                        strokeWidth={2}
                        dot={false}
                        activeDot={{ r: 4 }}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}
            </section>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              {/* Cost by workspace */}
              <section className="bg-white border rounded-lg shadow-sm p-4">
                <h2 className="font-medium text-gray-900 mb-4">Cost by Workspace</h2>
                {summary.by_workspace.length === 0 ? (
                  <p className="text-sm text-gray-500">No workspace activity.</p>
                ) : (
                  <div className="space-y-3">
                    {summary.by_workspace.slice(0, 8).map(ws => {
                      const pct = maxWorkspaceCost > 0 ? (ws.cost_usd / maxWorkspaceCost) * 100 : 0
                      return (
                        <button
                          key={ws.workspace_id}
                          onClick={() => navigate(`/analytics/workspace/${ws.workspace_id}`)}
                          className="block w-full text-left group"
                        >
                          <div className="flex items-center justify-between text-sm mb-1">
                            <span className="text-gray-700 group-hover:text-blue-600 truncate pr-2 transition-colors">
                              {ws.name}
                            </span>
                            <span className="text-gray-900 font-medium">
                              {formatCost(ws.cost_usd)}
                            </span>
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
              </section>

              {/* Cost by model */}
              <section className="bg-white border rounded-lg shadow-sm p-4">
                <h2 className="font-medium text-gray-900 mb-4">Cost by Model</h2>
                {sortedModels.length === 0 ? (
                  <p className="text-sm text-gray-500">No model usage.</p>
                ) : (
                  <div className="space-y-3">
                    {sortedModels.map(m => {
                      const pct = maxModelCost > 0 ? (m.cost_usd / maxModelCost) * 100 : 0
                      return (
                        <div key={m.model}>
                          <div className="flex items-center justify-between text-sm mb-1">
                            <span className="text-gray-700 truncate pr-2">
                              {shortenModelName(m.model)}
                            </span>
                            <span className="text-gray-900 font-medium">
                              {formatCost(m.cost_usd)}
                            </span>
                          </div>
                          <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
                            <div
                              className="h-full bg-purple-500"
                              style={{ width: `${pct}%` }}
                            />
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </section>
            </div>

            {/* Top users */}
            <section className="bg-white border rounded-lg shadow-sm">
              <div className="px-4 py-3 border-b">
                <h2 className="font-medium text-gray-900">Top Users</h2>
              </div>
              {users.length === 0 ? (
                <p className="text-sm text-gray-500 px-4 py-6">
                  {isOrgAdmin
                    ? 'No user activity in this range.'
                    : 'User breakdown requires workspace admin access.'}
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left text-gray-500 text-xs uppercase tracking-wide">
                        <th className="px-4 py-2 font-medium">User</th>
                        <th className="px-4 py-2 font-medium">Queries</th>
                        <th className="px-4 py-2 font-medium">Cost</th>
                        <th className="px-4 py-2 font-medium">Last Active</th>
                      </tr>
                    </thead>
                    <tbody>
                      {users.map(u => (
                        <tr key={u.email} className="border-t hover:bg-gray-50">
                          <td className="px-4 py-2 text-gray-900">{u.email}</td>
                          <td className="px-4 py-2 text-gray-700">
                            {formatNumber(u.queries)}
                          </td>
                          <td className="px-4 py-2 text-gray-700">
                            {formatCost(u.cost_usd)}
                          </td>
                          <td className="px-4 py-2 text-gray-500">
                            {formatLastActive(u.last_active)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

          </div>
        )}
      </main>
    </div>
  )
}

interface StatCardProps {
  label: string
  value: string
  sublabel?: string
  delta?: WoWDelta | null
  deltaDirection?: 'higher-is-good' | 'higher-is-bad'
}

function StatCard({ label, value, sublabel, delta, deltaDirection = 'higher-is-good' }: StatCardProps) {
  return (
    <div className="bg-white border rounded-lg shadow-sm p-4">
      <div className="text-xs text-gray-500 uppercase tracking-wide">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-gray-900">{value}</div>
      {delta && delta.pct !== null ? (
        <WoWBadge pct={delta.pct} direction={deltaDirection} />
      ) : (
        sublabel && <div className="mt-1 text-xs text-gray-500">{sublabel}</div>
      )}
    </div>
  )
}

interface WoWBadgeProps {
  pct: number
  direction: 'higher-is-good' | 'higher-is-bad'
}

function WoWBadge({ pct, direction }: WoWBadgeProps) {
  const up = pct > 0.005
  const down = pct < -0.005
  const flat = !up && !down
  const good = direction === 'higher-is-good' ? up : down
  const color = flat ? 'text-gray-500' : good ? 'text-emerald-600' : 'text-rose-600'
  const Icon = up ? ArrowUpRight : down ? ArrowDownRight : null
  const formatted = `${pct > 0 ? '+' : ''}${(pct * 100).toFixed(1)}%`
  return (
    <div className={`mt-1 flex items-center gap-1 text-xs ${color}`}>
      {Icon && <Icon size={12} />}
      <span>{formatted}</span>
      <span className="text-gray-400 font-normal">vs prior 7d</span>
    </div>
  )
}

interface TrendTooltipPayload {
  date: string
  cost_usd: number
  queries: number
}

interface TrendTooltipProps {
  active?: boolean
  payload?: Array<{ payload: TrendTooltipPayload }>
}

function TrendTooltip({ active, payload }: TrendTooltipProps) {
  if (!active || !payload || payload.length === 0) return null
  const data = payload[0].payload
  return (
    <div className="bg-white border rounded-lg shadow-md px-3 py-2 text-xs">
      <div className="font-medium text-gray-900 mb-1">{data.date}</div>
      <div className="text-gray-700">
        <span className="text-gray-500">Cost:</span>{' '}
        <span className="font-medium">${data.cost_usd.toFixed(4)}</span>
      </div>
      <div className="text-gray-700">
        <span className="text-gray-500">Queries:</span>{' '}
        <span className="font-medium">{data.queries.toLocaleString()}</span>
      </div>
    </div>
  )
}
