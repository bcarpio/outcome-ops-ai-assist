import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Plus,
  Folder,
  Users,
  Loader2,
  LogOut,
  Shield,
  MessageSquare,
  Settings,
  BarChart3,
} from 'lucide-react'
import {
  listWorkspaces,
  createWorkspace,
  type Workspace,
} from '../lib/api'

export default function Workspaces() {
  const navigate = useNavigate()
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDescription, setNewDescription] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isOrgAdmin, setIsOrgAdmin] = useState(false)

  useEffect(() => {
    loadWorkspaces()
  }, [])

  async function loadWorkspaces() {
    setLoading(true)
    try {
      const data = await listWorkspaces()
      setWorkspaces(data)
      setIsOrgAdmin(data.some(w => w.role === 'org_admin'))
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
      const workspace = await createWorkspace(newName.trim(), newDescription.trim())
      setWorkspaces(prev => [workspace, ...prev])
      setShowCreateModal(false)
      setNewName('')
      setNewDescription('')
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
            <button
              onClick={() => setShowCreateModal(true)}
              className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
            >
              <Plus size={18} />
              New Workspace
            </button>
            )}
            <button
              onClick={() => navigate('/')}
              title="Go to Chat"
              className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
            >
              <MessageSquare size={20} />
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
                onClick={() => navigate('/analytics')}
                title="Analytics"
                className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
              >
                <BarChart3 size={20} />
              </button>
            )}
            <button
              onClick={() => navigate('/org/settings')}
              title="Organization Settings"
              className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
            >
              <Shield size={20} />
            </button>
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
                <button
                  onClick={() => setShowCreateModal(true)}
                  className="mt-6 inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
                >
                  <Plus size={18} />
                  Create Your First Workspace
                </button>
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
                  <h3 className="font-medium text-gray-900 truncate">{workspace.name}</h3>
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
              </div>
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex justify-end gap-3">
              <button
                onClick={() => {
                  setShowCreateModal(false)
                  setNewName('')
                  setNewDescription('')
                  setError(null)
                }}
                className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleCreate}
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
    </div>
  )
}
