import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import {
  ArrowLeft,
  Loader2,
  AlertCircle,
  Check,
  Wallet,
} from 'lucide-react'
import AdminNav from '../components/AdminNav'
import {
  listWorkspaceBudgets,
  setWorkspaceBudget,
  type WorkspaceBudgetSummary,
} from '../lib/api'

function formatCost(value: number): string {
  if (value >= 1000) return `$${value.toFixed(0)}`
  if (value >= 1) return `$${value.toFixed(2)}`
  return `$${value.toFixed(4)}`
}

export default function AnalyticsBudgets() {
  const navigate = useNavigate()
  const [rows, setRows] = useState<WorkspaceBudgetSummary[]>([])
  const [month, setMonth] = useState<string>('')
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [savingId, setSavingId] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  useEffect(() => {
    load()
  }, [])

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const data = await listWorkspaceBudgets()
      setRows(data.workspaces)
      setMonth(data.month)
      const map: Record<string, string> = {}
      for (const ws of data.workspaces) {
        map[ws.workspace_id] = ws.monthly_budget_usd != null ? String(ws.monthly_budget_usd) : ''
      }
      setInputs(map)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load budgets')
    } finally {
      setLoading(false)
    }
  }

  async function handleSave(workspaceId: string) {
    const raw = (inputs[workspaceId] ?? '').trim()
    let value: number | null = null
    if (raw !== '') {
      const parsed = Number(raw)
      if (!Number.isFinite(parsed) || parsed < 0) {
        setError('Budget must be a non-negative number')
        return
      }
      value = parsed
    }
    setSavingId(workspaceId)
    setError(null)
    try {
      const updated = await setWorkspaceBudget(workspaceId, value)
      setRows(prev =>
        prev.map(r =>
          r.workspace_id === workspaceId
            ? {
                ...r,
                monthly_budget_usd: updated.monthly_budget_usd,
                is_default: updated.is_default,
                updated_at: updated.updated_at,
                updated_by: updated.updated_by,
              }
            : r,
        ),
      )
      setToast(
        value === null
          ? `Cleared override — using org default`
          : `Saved $${value.toFixed(2)}/mo budget`,
      )
      setTimeout(() => setToast(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save budget')
    } finally {
      setSavingId(null)
    }
  }

  const totals = useMemo(() => {
    const totalSpend = rows.reduce((acc, r) => acc + r.mtd_spend_usd, 0)
    const overCount = rows.filter(
      r => r.monthly_budget_usd != null && r.mtd_spend_usd >= r.monthly_budget_usd,
    ).length
    const withBudget = rows.filter(r => !r.is_default).length
    return { totalSpend, overCount, withBudget }
  }, [rows])

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b">
        <div className="max-w-5xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button
              onClick={() => navigate('/analytics')}
              className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
              title="Back to analytics"
            >
              <ArrowLeft size={20} />
            </button>
            <Wallet className="text-blue-600" size={24} />
            <div>
              <h1 className="text-xl font-semibold">Workspace Budgets</h1>
              <p className="text-sm text-gray-500">
                Org-admin only — set monthly spending caps per workspace
              </p>
            </div>
          </div>
          <AdminNav />
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 py-8 space-y-6">
        {error && (
          <div className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-2 text-red-700">
            <AlertCircle size={20} className="shrink-0 mt-0.5" />
            <div className="flex-1">
              <div className="font-medium">Error</div>
              <div className="text-sm text-red-600 mt-1">{error}</div>
            </div>
          </div>
        )}
        {toast && (
          <div className="bg-emerald-50 border border-emerald-200 rounded-lg p-3 flex items-center gap-2 text-emerald-700 text-sm">
            <Check size={16} />
            {toast}
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="animate-spin text-blue-600" size={32} />
          </div>
        ) : rows.length === 0 ? (
          <div className="text-center py-16 text-gray-500">No workspaces yet.</div>
        ) : (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <SummaryCard
                label={`Total spend (${month})`}
                value={formatCost(totals.totalSpend)}
              />
              <SummaryCard
                label="With budget override"
                value={`${totals.withBudget} / ${rows.length}`}
              />
              <SummaryCard
                label="Over budget"
                value={String(totals.overCount)}
                accent={totals.overCount > 0 ? 'rose' : undefined}
              />
            </div>

            <section className="bg-white border rounded-lg shadow-sm overflow-hidden">
              <div className="px-4 py-3 border-b flex items-center justify-between">
                <div>
                  <h2 className="font-medium text-gray-900">Per-workspace budgets</h2>
                  <p className="text-xs text-gray-500 mt-0.5">
                    Empty input = use the org default from tfvars. A value of 0 also clears the override.
                  </p>
                </div>
                <div className="text-xs text-gray-500">Month: {month}</div>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-gray-50">
                    <tr className="text-left text-gray-500 text-xs uppercase tracking-wide">
                      <th className="px-4 py-2 font-medium">Workspace</th>
                      <th className="px-4 py-2 font-medium text-right">MTD Spend</th>
                      <th className="px-4 py-2 font-medium">Monthly Budget</th>
                      <th className="px-4 py-2 font-medium">Status</th>
                      <th className="px-4 py-2 font-medium text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map(row => {
                      const inputValue = inputs[row.workspace_id] ?? ''
                      const isSaving = savingId === row.workspace_id
                      const over =
                        row.monthly_budget_usd != null &&
                        row.mtd_spend_usd >= row.monthly_budget_usd
                      return (
                        <tr key={row.workspace_id} className="border-t hover:bg-gray-50">
                          <td className="px-4 py-2">
                            <button
                              onClick={() =>
                                navigate(`/analytics/workspace/${row.workspace_id}`)
                              }
                              className="text-gray-900 hover:text-blue-600 font-medium transition-colors"
                            >
                              {row.name}
                            </button>
                            {!row.is_default && row.updated_by && (
                              <div className="text-xs text-gray-400 mt-0.5">
                                Last updated by {row.updated_by}
                              </div>
                            )}
                          </td>
                          <td className="px-4 py-2 text-right text-gray-700 tabular-nums">
                            {formatCost(row.mtd_spend_usd)}
                          </td>
                          <td className="px-4 py-2">
                            <div className="relative w-32">
                              <span className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400 text-xs">$</span>
                              <input
                                type="number"
                                min="0"
                                step="1"
                                value={inputValue}
                                onChange={e =>
                                  setInputs(prev => ({
                                    ...prev,
                                    [row.workspace_id]: e.target.value,
                                  }))
                                }
                                placeholder="org default"
                                className="w-full pl-5 pr-2 py-1 text-sm border rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
                              />
                            </div>
                          </td>
                          <td className="px-4 py-2">
                            {row.is_default ? (
                              <span className="text-xs text-gray-500">org default</span>
                            ) : over ? (
                              <span className="inline-flex items-center gap-1 text-xs text-rose-600 font-medium">
                                <AlertCircle size={12} />
                                over budget
                              </span>
                            ) : (
                              <span className="text-xs text-emerald-600">within budget</span>
                            )}
                          </td>
                          <td className="px-4 py-2 text-right">
                            <button
                              onClick={() => handleSave(row.workspace_id)}
                              disabled={isSaving}
                              className="px-3 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 transition-colors inline-flex items-center gap-1"
                            >
                              {isSaving && <Loader2 size={12} className="animate-spin" />}
                              Save
                            </button>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          </>
        )}
      </main>
    </div>
  )
}

interface SummaryCardProps {
  label: string
  value: string
  accent?: 'rose'
}

function SummaryCard({ label, value, accent }: SummaryCardProps) {
  const valueClass = accent === 'rose' ? 'text-rose-600' : 'text-gray-900'
  return (
    <div className="bg-white border rounded-lg shadow-sm p-4">
      <div className="text-xs text-gray-500 uppercase tracking-wide">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${valueClass}`}>{value}</div>
    </div>
  )
}
