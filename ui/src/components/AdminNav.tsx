import { useEffect, useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import {
  MessageSquare,
  Folder,
  Settings,
  BarChart3,
  Wallet,
  ShieldAlert,
  Shield,
  LogOut,
} from 'lucide-react'
import { getSearchableWorkspaces } from '../lib/api'

/**
 * Standard right-side navigation cluster for admin / authenticated pages.
 *
 * All admin pages render the same set of icons in the same order so the
 * user can always jump directly to another section. Icons that require
 * org_admin membership (Analytics, Budgets) are hidden otherwise.
 *
 * Icon order is load-bearing: Chat, Workspaces, Settings, [Analytics],
 * [Budgets], Org Settings, Sign out. Keep it in sync when adding new
 * admin surfaces.
 */
export default function AdminNav() {
  const navigate = useNavigate()
  const location = useLocation()
  const [isOrgAdmin, setIsOrgAdmin] = useState(false)

  useEffect(() => {
    let cancelled = false
    // Wrap in try/catch: some test files mock api.ts partially and the
    // function can be undefined, which would otherwise throw synchronously
    // before the .catch handler runs.
    try {
      getSearchableWorkspaces()
        .then(ws => {
          if (cancelled) return
          setIsOrgAdmin(ws.some(w => w.access_type === 'org_admin'))
        })
        .catch(() => {
          /* non-fatal: we just won't show admin-only buttons */
        })
    } catch {
      /* same reasoning: non-fatal */
    }
    return () => {
      cancelled = true
    }
  }, [])

  const path = location.pathname
  const isChat = path === '/' || path.startsWith('/chat')
  const isWorkspaces = path === '/workspaces'
  const isSettings = path.startsWith('/settings')
  const isAnalyticsRoot = path === '/analytics'
  const isBudgets = path.startsWith('/analytics/budgets')
  const isAudit = path.startsWith('/audit')
  const isOrgSettings = path.startsWith('/org/settings')

  return (
    <div className="flex items-center gap-2">
      {!isChat && <IconButton title="Chat" onClick={() => navigate('/')} icon={<MessageSquare size={20} />} />}
      {!isWorkspaces && <IconButton title="Workspaces" onClick={() => navigate('/workspaces')} icon={<Folder size={20} />} />}
      {!isSettings && <IconButton title="Settings" onClick={() => navigate('/settings')} icon={<Settings size={20} />} />}
      {isOrgAdmin && !isAnalyticsRoot && (
        <IconButton title="Analytics" onClick={() => navigate('/analytics')} icon={<BarChart3 size={20} />} />
      )}
      {isOrgAdmin && !isBudgets && (
        <IconButton title="Budgets" onClick={() => navigate('/analytics/budgets')} icon={<Wallet size={20} />} />
      )}
      {isOrgAdmin && !isAudit && (
        <IconButton title="Audit" onClick={() => navigate('/audit')} icon={<ShieldAlert size={20} />} />
      )}
      {!isOrgSettings && (
        <IconButton title="Organization Settings" onClick={() => navigate('/org/settings')} icon={<Shield size={20} />} />
      )}
      <IconButton
        title="Sign out"
        onClick={() => {
          window.location.href = '/api/logout'
        }}
        icon={<LogOut size={20} />}
      />
    </div>
  )
}

interface IconButtonProps {
  title: string
  onClick: () => void
  icon: React.ReactNode
}

function IconButton({ title, onClick, icon }: IconButtonProps) {
  return (
    <button
      onClick={onClick}
      title={title}
      aria-label={title}
      className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
    >
      {icon}
    </button>
  )
}
