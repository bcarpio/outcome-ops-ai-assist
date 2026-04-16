import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  BarChart3,
  Loader2,
  AlertCircle,
  AlertTriangle,
  Clock,
  TrendingUp,
} from 'lucide-react'
import {
  getAnalyticsWorkspace,
  type AnalyticsWorkspaceDetail,
} from '../lib/api'

type DateRangeOption = 30 | 60 | 90

function formatCost(value: number): string {
  if (value >= 1000) return `$${value.toFixed(0)}`
  if (value >= 1) return `$${value.toFixed(2)}`
  return `$${value.toFixed(4)}`
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

function isoDaysAgo(days: number): string {
  const d = new Date()
  d.setUTCDate(d.getUTCDate() - days)
  return d.toISOString()
}

export default function WorkspaceAnalytics() {
  const navigate = useNavigate()
  const { workspaceId } = useParams<{ workspaceId: string }>()
  const [range, setRange] = useState<DateRangeOption>(30)
  const [detail, setDetail] = useState<AnalyticsWorkspaceDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!workspaceId) return
    let cancelled = false
    setLoading(true)
    setError(null)
    getAnalyticsWorkspace(workspaceId, isoDaysAgo(range))
      .then(data => {
        if (cancelled) return
        setDetail(data)
      })
      .catch(err => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Failed to load workspace analytics')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [workspaceId, range])

  const maxDailyCost = useMemo(() => {
    if (!detail?.daily_trend.length) return 0
    return Math.max(...detail.daily_trend.map(d => d.cost_usd))
  }, [detail])

  const maxUserCost = useMemo(() => {
    if (!detail?.by_user.length) return 0
    return Math.max(...detail.by_user.map(u => u.cost_usd))
  }, [detail])

  const sortedModels = useMemo(() => {
    if (!detail) return []
    return Object.entries(detail.by_model)
      .map(([model, data]) => ({ model, ...data }))
      .sort((a, b) => b.cost_usd - a.cost_usd)
  }, [detail])

  const maxModelCost = useMemo(() => {
    if (!sortedModels.length) return 0
    return Math.max(...sortedModels.map(m => m.cost_usd))
  }, [sortedModels])

  const refusalRate = useMemo(() => {
    if (!detail || detail.total_queries === 0) return 0
    return detail.refusal_count / detail.total_queries
  }, [detail])

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b">
        <div className="max-w-6xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button
              onClick={() => navigate('/analytics')}
              className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
              title="Back to analytics"
            >
              <ArrowLeft size={20} />
            </button>
            <BarChart3 className="text-blue-600" size={24} />
            <div>
              <h1 className="text-xl font-semibold">
                {detail?.workspace_name || 'Workspace Analytics'}
              </h1>
              <p className="text-sm text-gray-500">Workspace usage breakdown</p>
            </div>
          </div>
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
              <div className="font-medium">Failed to load workspace analytics</div>
              <div className="text-sm text-red-600 mt-1">{error}</div>
            </div>
          </div>
        ) : !detail ? (
          <div className="text-center py-16 text-gray-500">No data for this workspace.</div>
        ) : (
          <div className="space-y-6">
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
              <StatCard label="Total Cost" value={formatCost(detail.total_cost_usd)} sublabel={`last ${range} days`} />
              <StatCard label="Queries" value={formatNumber(detail.total_queries)} sublabel="total" />
              <StatCard
                label="Avg Response"
                value={`${formatNumber(detail.avg_duration_ms)} ms`}
                sublabel="per query"
                icon={<Clock size={14} />}
              />
              <StatCard
                label="Refusals"
                value={formatNumber(detail.refusal_count)}
                sublabel={`${(refusalRate * 100).toFixed(2)}% rate`}
                icon={<AlertTriangle size={14} className="text-amber-500" />}
                accent={detail.refusal_count > 0 ? 'amber' : undefined}
              />
            </div>

            <section className="bg-white border rounded-lg shadow-sm p-4">
              <div className="flex items-center gap-2 mb-4">
                <TrendingUp size={16} className="text-gray-500" />
                <h2 className="font-medium text-gray-900">Daily Cost Trend</h2>
                <span className="text-sm text-gray-500 ml-1">(last {range} days)</span>
              </div>
              {detail.daily_trend.length === 0 ? (
                <p className="text-sm text-gray-500 py-4">No activity in this range.</p>
              ) : (
                <div className="flex items-end gap-1 h-32">
                  {detail.daily_trend.map(day => {
                    const heightPct = maxDailyCost > 0 ? (day.cost_usd / maxDailyCost) * 100 : 0
                    return (
                      <div
                        key={day.date}
                        className="flex-1 bg-blue-500 hover:bg-blue-600 rounded-t transition-colors min-w-[4px]"
                        style={{ height: `${Math.max(heightPct, 2)}%` }}
                        title={`${day.date}: ${formatCost(day.cost_usd)} · ${formatNumber(day.queries)} queries`}
                      />
                    )
                  })}
                </div>
              )}
            </section>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <section className="bg-white border rounded-lg shadow-sm p-4">
                <h2 className="font-medium text-gray-900 mb-4">Cost by User</h2>
                {detail.by_user.length === 0 ? (
                  <p className="text-sm text-gray-500">No user activity.</p>
                ) : (
                  <div className="space-y-3">
                    {detail.by_user.slice(0, 12).map(u => {
                      const pct = maxUserCost > 0 ? (u.cost_usd / maxUserCost) * 100 : 0
                      return (
                        <div key={u.email}>
                          <div className="flex items-center justify-between text-sm mb-1">
                            <span className="text-gray-700 truncate pr-2">{u.email}</span>
                            <span className="text-gray-900 font-medium">
                              {formatCost(u.cost_usd)}
                              <span className="text-gray-400 font-normal ml-2">
                                {formatNumber(u.queries)}
                              </span>
                            </span>
                          </div>
                          <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
                            <div className="h-full bg-sky-500" style={{ width: `${pct}%` }} />
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </section>

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
                            <div className="h-full bg-purple-500" style={{ width: `${pct}%` }} />
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </section>
            </div>
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
  icon?: React.ReactNode
  accent?: 'amber'
}

function StatCard({ label, value, sublabel, icon, accent }: StatCardProps) {
  const valueClass = accent === 'amber' ? 'text-amber-600' : 'text-gray-900'
  return (
    <div className="bg-white border rounded-lg shadow-sm p-4">
      <div className="flex items-center gap-1.5 text-xs text-gray-500 uppercase tracking-wide">
        {icon}
        {label}
      </div>
      <div className={`mt-1 text-2xl font-semibold ${valueClass}`}>{value}</div>
      {sublabel && <div className="mt-1 text-xs text-gray-500">{sublabel}</div>}
    </div>
  )
}
