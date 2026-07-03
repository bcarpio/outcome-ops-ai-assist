import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Plus,
  Folder,
  Users,
  Loader2,
  Globe,
} from 'lucide-react'
import {
  listWorkspaces,
  createWorkspace,
  listOrgMembers,
  getCurrentUser,
  type Workspace,
} from '../lib/api'
import AdminNav from '../components/AdminNav'
import MutationButton from '../components/MutationButton'

export default function Workspaces() {
  const navigate = useNavigate()
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDescription, setNewDescription] = useState('')
  const [newIsGlobal, setNewIsGlobal] = useState(false)
  // Two-step confirm for is_global at creation. Off-toggle is a
  // de-escalation and doesn't warrant the same friction, but flipping
  // ON at creation broadens the read surface for the whole deploy --
  // ceremony matches the risk.
  const [showGlobalWarning, setShowGlobalWarning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [isOrgAdmin, setIsOrgAdmin] = useState(false)

  useEffect(() => {
    loadWorkspaces()
  }, [])

  async function loadWorkspaces() {
    setLoading(true)
    try {
      // Resolve workspaces + org-admin status in parallel. isOrgAdmin
      // used to be derived from `data.some(w => w.role === 'org_admin')`,
      // which silently returned false when the workspace list was
      // empty -- including the legitimate case of an org admin viewing
      // a fresh workspace they haven't populated yet. The empty-state
      // then mis-routed the org admin into the "ask an org admin" copy
      // (telling them to ask themselves). Surfaced 2026-06-04 on the
      // openai workspace's first login.
      //
      // listOrgMembers requires org_admin server-side, so the catch
      // doubles as the "not an org admin" signal; the email match
      // protects against the (current) endpoint widening that
      // permission later.
      const [data, members, me] = await Promise.all([
        listWorkspaces(),
        listOrgMembers().catch(() => [] as Awaited<ReturnType<typeof listOrgMembers>>),
        getCurrentUser().catch(() => ({ email: '' })),
      ])
      setWorkspaces(data)
      const myEmail = (me.email || '').toLowerCase()
      setIsOrgAdmin(
        members.some(
          (m) => m.user_email.toLowerCase() === myEmail && m.org_role === 'org_admin'
        )
      )
    } catch (err) {
      console.error('Failed to load workspaces:', err)
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate() {
    if (!newName.trim()) return
    setCreating(true)
    setError(null)

    try {
      const workspace = await createWorkspace(
        newName.trim(),
        newDescription.trim(),
        { is_global: newIsGlobal },
      )
      setWorkspaces(prev => [workspace, ...prev])
      setShowCreateModal(false)
      setNewName('')
      setNewDescription('')
      setNewIsGlobal(false)
      setShowGlobalWarning(false)
      // Navigate to the new workspace
      navigate(`/workspaces/${workspace.workspace_id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create workspace')
    } finally {
      setCreating(false)
    }
  }

  function handleOpenWorkspace(workspaceId: string) {
    navigate(`/workspaces/${workspaceId}`)
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b">
        <div className="max-w-6xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Folder className="text-blue-600" size={24} />
            <h1 className="text-xl font-semibold">Workspaces</h1>
          </div>
          <div className="flex items-center gap-3">
            {isOrgAdmin && (
              <MutationButton
                onClick={() => setShowCreateModal(true)}
                className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <Plus size={18} />
                New Workspace
              </MutationButton>
            )}
            <AdminNav />
          </div>
        </div>
      </header>

      {/* Content */}
      <main className="max-w-6xl mx-auto px-4 py-8">
        {loading ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="animate-spin text-blue-600" size={32} />
          </div>
        ) : workspaces.length === 0 ? (
          <div className="text-center py-16">
            <Folder className="mx-auto text-gray-300" size={64} />
            <h2 className="mt-4 text-xl font-medium text-gray-600">No workspaces yet</h2>
            {isOrgAdmin ? (
              <>
                <p className="mt-2 text-gray-500">Create a workspace to organize your repos and integrations</p>
                <MutationButton
                  onClick={() => setShowCreateModal(true)}
                  className="mt-6 inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <Plus size={18} />
                  Create Your First Workspace
                </MutationButton>
              </>
            ) : (
              <p className="mt-2 text-gray-500">Ask an org admin to add you to a workspace</p>
            )}
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {workspaces.map(workspace => (
              <div
                key={workspace.workspace_id}
                onClick={() => handleOpenWorkspace(workspace.workspace_id)}
                className="bg-white rounded-lg border shadow-sm hover:shadow-md hover:border-blue-300 transition-all cursor-pointer"
              >
                <div className="p-4">
                  <div className="flex items-center gap-2 min-w-0">
                    <h3 className="font-medium text-gray-900 truncate flex-1">{workspace.name}</h3>
                    {workspace.is_global && (
                      <span
                        className="text-xs px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 inline-flex items-center gap-1 flex-shrink-0"
                        title="Global workspace — content applies across the deployment"
                      >
                        <Globe size={11} />
                        Global
                      </span>
                    )}
                  </div>
                  {workspace.description && (
                    <p className="mt-1 text-sm text-gray-500 line-clamp-2">{workspace.description}</p>
                  )}
                  <div className="mt-4 flex items-center gap-1 text-sm text-gray-500">
                    <Users size={14} />
                    <span className="capitalize">{workspace.role?.replace('_', ' ') || 'Member'}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </main>

      {/* Create Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4">
            <div className="p-6">
              <h2 className="text-lg font-semibold mb-4">Create Workspace</h2>
              {error && (
                <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">
                  {error}
                </div>
              )}
              <div className="space-y-4">
                <div>
                  <label htmlFor="name" className="block text-sm font-medium text-gray-700 mb-1">
                    Name
                  </label>
                  <input
                    id="name"
                    type="text"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    placeholder="My Workspace"
                    className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                    autoFocus
                  />
                </div>
                <div>
                  <label htmlFor="description" className="block text-sm font-medium text-gray-700 mb-1">
                    Description (optional)
                  </label>
                  <textarea
                    id="description"
                    value={newDescription}
                    onChange={(e) => setNewDescription(e.target.value)}
                    placeholder="A brief description of this workspace"
                    rows={3}
                    className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
                  />
                </div>
                {/* Global toggle — org-admin only (whole create modal
                    is already gated to org admins upstream, so no
                    additional gate here). Bidirectional-scope
                    ceremony is handled by the two-step confirm below. */}
                <div className="pt-2 border-t">
                  <label className="flex items-start gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={newIsGlobal}
                      onChange={(e) => setNewIsGlobal(e.target.checked)}
                      className="mt-1 w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                    />
                    <div>
                      <div className="text-sm font-medium text-gray-700 flex items-center gap-1">
                        <Globe size={14} className="text-blue-600" />
                        Global workspace
                      </div>
                      <p className="text-xs text-gray-500 mt-0.5">
                        This workspace&rsquo;s content applies across the deployment (chat + PR checks).
                        Members get read across all other workspaces when querying from here.
                      </p>
                    </div>
                  </label>
                </div>
              </div>
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex justify-end gap-3">
              <button
                onClick={() => {
                  setShowCreateModal(false)
                  setNewName('')
                  setNewDescription('')
                  setNewIsGlobal(false)
                  setShowGlobalWarning(false)
                  setError(null)
                }}
                className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  // Two-step confirm if is_global was ticked. Non-global
                  // creates skip straight to handleCreate.
                  if (newIsGlobal) {
                    setShowGlobalWarning(true)
                  } else {
                    handleCreate()
                  }
                }}
                disabled={!newName.trim() || creating}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
              >
                {creating && <Loader2 size={16} className="animate-spin" />}
                Create
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Global-workspace two-step confirm. Renders on top of the
          create modal (higher z-index). */}
      {showGlobalWarning && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-[60]">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4">
            <div className="p-6">
              <div className="flex items-center gap-2 mb-3">
                <Globe className="text-blue-600" size={20} />
                <h2 className="text-lg font-semibold">Confirm Global workspace</h2>
              </div>
              <p className="text-sm text-gray-700 mb-3">
                Marking <span className="font-semibold">{newName.trim() || 'this workspace'}</span> as
                Global does two things:
              </p>
              <ul className="text-sm text-gray-700 space-y-2 mb-4 list-disc pl-5">
                <li>
                  Its content (ADRs, docs, code-maps) will be available in every user&rsquo;s chat
                  and every PR check across this deployment.
                </li>
                <li>
                  Its members will have implicit read access on every other workspace&rsquo;s
                  content when querying from this workspace.
                </li>
              </ul>
              <p className="text-sm text-gray-600 mb-4">
                Only enable for teams whose enterprise-wide read posture is intentional —
                typically Enterprise Architecture, Security, Compliance, IT policies, or
                (rarely) Legal shared-guides.
              </p>
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex justify-end gap-3">
              <button
                onClick={() => setShowGlobalWarning(false)}
                disabled={creating}
                className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={handleCreate}
                disabled={creating}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
              >
                {creating && <Loader2 size={16} className="animate-spin" />}
                Confirm — Create as Global
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
