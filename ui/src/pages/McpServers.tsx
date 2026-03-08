import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ArrowLeft,
  Plus,
  Loader2,
  Trash2,
  RefreshCw,
  Settings,
  Globe,
  Server,
  X,
  Check,
  ChevronDown,
  ChevronRight,
} from 'lucide-react'
import {
  listMcpServers,
  createMcpServer,
  updateMcpServer,
  deleteMcpServer,
  refreshMcpTools,
  listWorkspaces,
  assignMcpToWorkspace,
  removeMcpFromWorkspace,
  listWorkspaceMcpServers,
  type McpServer,
  type Workspace,
  type CreateMcpServerRequest,
} from '../lib/api'

interface FormState {
  name: string
  url: string
  auth_type: 'none' | 'api_key' | 'bearer'
  credentials: string
  is_global: boolean
  refresh_interval_days: number
}

const emptyForm: FormState = {
  name: '',
  url: '',
  auth_type: 'none',
  credentials: '',
  is_global: false,
  refresh_interval_days: 7,
}

export default function McpServers() {
  const navigate = useNavigate()
  const [servers, setServers] = useState<McpServer[]>([])
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  // Modal state
  const [showModal, setShowModal] = useState(false)
  const [editingServer, setEditingServer] = useState<McpServer | null>(null)
  const [form, setForm] = useState<FormState>(emptyForm)
  const [saving, setSaving] = useState(false)

  // Refresh state
  const [refreshingId, setRefreshingId] = useState<string | null>(null)

  // Workspace assignment state
  const [expandedServerId, setExpandedServerId] = useState<string | null>(null)
  const [workspaceAssignments, setWorkspaceAssignments] = useState<Record<string, string[]>>({})
  const [assignmentLoading, setAssignmentLoading] = useState<string | null>(null)

  useEffect(() => {
    loadData()
  }, [])

  async function loadData() {
    setLoading(true)
    setError(null)
    try {
      const [serversData, workspacesData] = await Promise.all([
        listMcpServers(),
        listWorkspaces(),
      ])
      setServers(serversData)
      setWorkspaces(workspacesData)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load MCP servers')
    } finally {
      setLoading(false)
    }
  }

  async function loadWorkspaceAssignments(mcpId: string) {
    const assignments: string[] = []
    for (const ws of workspaces) {
      try {
        const wsServers = await listWorkspaceMcpServers(ws.workspace_id)
        if (wsServers.some((s) => s.mcp_id === mcpId)) {
          assignments.push(ws.workspace_id)
        }
      } catch {
        // skip workspaces we can't access
      }
    }
    setWorkspaceAssignments((prev) => ({ ...prev, [mcpId]: assignments }))
  }

  function handleExpand(mcpId: string) {
    if (expandedServerId === mcpId) {
      setExpandedServerId(null)
    } else {
      setExpandedServerId(mcpId)
      if (!workspaceAssignments[mcpId]) {
        loadWorkspaceAssignments(mcpId)
      }
    }
  }

  async function handleToggleWorkspace(mcpId: string, workspaceId: string, assigned: boolean) {
    setAssignmentLoading(`${mcpId}:${workspaceId}`)
    setError(null)
    try {
      if (assigned) {
        await removeMcpFromWorkspace(workspaceId, mcpId)
        setWorkspaceAssignments((prev) => ({
          ...prev,
          [mcpId]: (prev[mcpId] || []).filter((id) => id !== workspaceId),
        }))
      } else {
        await assignMcpToWorkspace(workspaceId, mcpId)
        setWorkspaceAssignments((prev) => ({
          ...prev,
          [mcpId]: [...(prev[mcpId] || []), workspaceId],
        }))
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update workspace assignment')
    } finally {
      setAssignmentLoading(null)
    }
  }

  function openCreateModal() {
    setEditingServer(null)
    setForm(emptyForm)
    setShowModal(true)
  }

  function openEditModal(server: McpServer) {
    setEditingServer(server)
    setForm({
      name: server.name,
      url: server.url,
      auth_type: server.auth_type,
      credentials: '',
      is_global: server.is_global,
      refresh_interval_days: server.refresh_interval_days,
    })
    setShowModal(true)
  }

  function closeModal() {
    setShowModal(false)
    setEditingServer(null)
    setForm(emptyForm)
    setError(null)
  }

  async function handleSave() {
    if (!form.name.trim() || !form.url.trim()) return
    setSaving(true)
    setError(null)
    try {
      if (editingServer) {
        const updated = await updateMcpServer(editingServer.mcp_id, {
          name: form.name,
          url: form.url,
          auth_type: form.auth_type,
          credentials: form.credentials || undefined,
          is_global: form.is_global,
          refresh_interval_days: form.refresh_interval_days,
        })
        setServers(servers.map((s) => (s.mcp_id === updated.mcp_id ? updated : s)))
        setSuccess(`Updated MCP server "${updated.name}"`)
      } else {
        const request: CreateMcpServerRequest = {
          name: form.name,
          url: form.url,
          auth_type: form.auth_type,
          credentials: form.credentials || undefined,
          is_global: form.is_global,
          refresh_interval_days: form.refresh_interval_days,
        }
        const created = await createMcpServer(request)
        setServers([...servers, created])
        setSuccess(`Created MCP server "${created.name}"`)
      }
      setTimeout(() => setSuccess(null), 3000)
      closeModal()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save MCP server')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(server: McpServer) {
    if (!confirm(`Are you sure you want to delete the MCP server "${server.name}"? This will remove it from all workspaces.`)) {
      return
    }
    setError(null)
    try {
      await deleteMcpServer(server.mcp_id)
      setServers(servers.filter((s) => s.mcp_id !== server.mcp_id))
      setSuccess(`Deleted MCP server "${server.name}"`)
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete MCP server')
    }
  }

  async function handleRefresh(server: McpServer) {
    setRefreshingId(server.mcp_id)
    setError(null)
    try {
      const updated = await refreshMcpTools(server.mcp_id)
      setServers(servers.map((s) => (s.mcp_id === updated.mcp_id ? updated : s)))
      setSuccess(`Refreshed tools for "${updated.name}" (${updated.tool_count} tools)`)
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to refresh MCP tools')
    } finally {
      setRefreshingId(null)
    }
  }

  function truncateUrl(url: string, maxLen = 40): string {
    return url.length > maxLen ? url.slice(0, maxLen) + '...' : url
  }

  function formatDate(dateStr: string | null): string {
    if (!dateStr) return 'Never'
    return new Date(dateStr).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b">
        <div className="max-w-4xl mx-auto px-4 py-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <button
                onClick={() => navigate('/workspaces')}
                className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
              >
                <ArrowLeft size={20} />
              </button>
              <div>
                <h1 className="text-xl font-semibold">MCP Servers</h1>
                <p className="text-sm text-gray-500">Manage Model Context Protocol servers for your organization</p>
              </div>
            </div>
            <button
              onClick={openCreateModal}
              className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
            >
              <Plus size={18} />
              Add MCP Server
            </button>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="max-w-4xl mx-auto px-4 py-8">
        {error && (
          <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-lg text-red-600 flex items-center justify-between">
            {error}
            <button onClick={() => setError(null)} className="text-red-400 hover:text-red-600">
              <X size={18} />
            </button>
          </div>
        )}

        {success && (
          <div className="mb-6 p-4 bg-green-50 border border-green-200 rounded-lg text-green-600 flex items-center gap-2">
            <Check size={18} />
            {success}
          </div>
        )}

        {/* Info Banner */}
        <div className="mb-6 p-4 bg-blue-50 border border-blue-200 rounded-lg">
          <div className="flex items-start gap-3">
            <Server className="text-blue-600 mt-0.5" size={20} />
            <div>
              <h3 className="font-medium text-blue-900">About MCP Servers</h3>
              <p className="text-sm text-blue-700 mt-1">
                <strong>MCP servers</strong> provide additional tools that the AI assistant can use during conversations.
                <strong className="ml-1">Global servers</strong> are available to all workspaces.
                <strong className="ml-1">Non-global servers</strong> must be assigned to specific workspaces.
              </p>
            </div>
          </div>
        </div>

        {/* Servers List */}
        <div className="bg-white rounded-lg border shadow-sm">
          <div className="p-4 border-b">
            <h3 className="font-medium flex items-center gap-2">
              <Server size={18} />
              MCP Servers ({servers.length})
            </h3>
          </div>

          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-6 h-6 animate-spin text-blue-600" />
            </div>
          ) : servers.length === 0 ? (
            <div className="text-center py-12">
              <Server className="w-12 h-12 text-gray-300 mx-auto mb-4" />
              <h3 className="text-lg font-medium text-gray-900">No MCP servers configured</h3>
              <p className="text-gray-600 mt-1">
                Add an MCP server to provide additional tools to the AI assistant.
              </p>
            </div>
          ) : (
            <div className="divide-y">
              {servers.map((server) => {
                const isExpanded = expandedServerId === server.mcp_id
                const assignments = workspaceAssignments[server.mcp_id]
                return (
                  <div key={server.mcp_id}>
                    <div className="flex items-center justify-between p-4 hover:bg-gray-50">
                      <div className="flex items-center gap-4 min-w-0 flex-1">
                        <div className="w-10 h-10 bg-gray-100 rounded-full flex items-center justify-center flex-shrink-0">
                          {server.is_global ? (
                            <Globe className="w-5 h-5 text-blue-500" />
                          ) : (
                            <Server className="w-5 h-5 text-gray-500" />
                          )}
                        </div>
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <p className="font-medium text-gray-900">{server.name}</p>
                            <span
                              className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                                server.status === 'active'
                                  ? 'bg-green-100 text-green-700'
                                  : 'bg-red-100 text-red-700'
                              }`}
                            >
                              {server.status === 'active' ? 'Active' : 'Error'}
                            </span>
                            {server.is_global && (
                              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-700">
                                Global
                              </span>
                            )}
                          </div>
                          <p className="text-sm text-gray-500 truncate" title={server.url}>
                            {truncateUrl(server.url)}
                          </p>
                          <p className="text-xs text-gray-400 mt-0.5">
                            {server.tool_count} tool{server.tool_count !== 1 ? 's' : ''}
                            {' -- '}
                            Last refreshed: {formatDate(server.last_refreshed_at)}
                          </p>
                        </div>
                      </div>

                      <div className="flex items-center gap-2 flex-shrink-0 ml-4">
                        <button
                          onClick={() => handleRefresh(server)}
                          disabled={refreshingId === server.mcp_id}
                          className="p-2 text-gray-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-colors disabled:opacity-50"
                          title="Refresh tools"
                        >
                          <RefreshCw
                            size={16}
                            className={refreshingId === server.mcp_id ? 'animate-spin' : ''}
                          />
                        </button>
                        <button
                          onClick={() => openEditModal(server)}
                          className="p-2 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
                          title="Edit server"
                        >
                          <Settings size={16} />
                        </button>
                        <button
                          onClick={() => handleDelete(server)}
                          className="p-2 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                          title="Delete server"
                        >
                          <Trash2 size={16} />
                        </button>
                        {!server.is_global && (
                          <button
                            onClick={() => handleExpand(server.mcp_id)}
                            className="p-2 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
                            title="Workspace assignments"
                          >
                            {isExpanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                          </button>
                        )}
                      </div>
                    </div>

                    {/* Workspace Assignments (expandable) */}
                    {isExpanded && !server.is_global && (
                      <div className="px-4 pb-4 pl-18">
                        <div className="ml-14 p-3 bg-gray-50 rounded-lg border">
                          <h4 className="text-sm font-medium text-gray-700 mb-2">Workspace Assignments</h4>
                          {!assignments ? (
                            <div className="flex items-center gap-2 text-sm text-gray-500">
                              <Loader2 size={14} className="animate-spin" />
                              Loading workspaces...
                            </div>
                          ) : workspaces.length === 0 ? (
                            <p className="text-sm text-gray-500">No workspaces available.</p>
                          ) : (
                            <div className="space-y-1">
                              {workspaces.map((ws) => {
                                const isAssigned = assignments.includes(ws.workspace_id)
                                const isToggling = assignmentLoading === `${server.mcp_id}:${ws.workspace_id}`
                                return (
                                  <label
                                    key={ws.workspace_id}
                                    className="flex items-center gap-2 py-1 px-2 rounded hover:bg-gray-100 cursor-pointer text-sm"
                                  >
                                    {isToggling ? (
                                      <Loader2 size={14} className="animate-spin text-blue-600" />
                                    ) : (
                                      <input
                                        type="checkbox"
                                        checked={isAssigned}
                                        onChange={() => handleToggleWorkspace(server.mcp_id, ws.workspace_id, isAssigned)}
                                        className="rounded text-blue-600"
                                      />
                                    )}
                                    <span className="text-gray-700">{ws.name}</span>
                                  </label>
                                )
                              })}
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </main>

      {/* Create/Edit Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4">
            <div className="p-6">
              <h2 className="text-lg font-semibold mb-4">
                {editingServer ? 'Edit MCP Server' : 'Add MCP Server'}
              </h2>
              {error && (
                <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">
                  {error}
                </div>
              )}
              <div className="space-y-4">
                <div>
                  <label htmlFor="mcp-name" className="block text-sm font-medium text-gray-700 mb-1">
                    Name
                  </label>
                  <input
                    id="mcp-name"
                    type="text"
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder="My MCP Server"
                    className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                    autoFocus
                  />
                </div>
                <div>
                  <label htmlFor="mcp-url" className="block text-sm font-medium text-gray-700 mb-1">
                    URL
                  </label>
                  <input
                    id="mcp-url"
                    type="text"
                    value={form.url}
                    onChange={(e) => setForm({ ...form, url: e.target.value })}
                    placeholder="https://mcp.example.com/sse"
                    className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
                <div>
                  <label htmlFor="mcp-auth" className="block text-sm font-medium text-gray-700 mb-1">
                    Auth Type
                  </label>
                  <select
                    id="mcp-auth"
                    value={form.auth_type}
                    onChange={(e) =>
                      setForm({ ...form, auth_type: e.target.value as 'none' | 'api_key' | 'bearer', credentials: '' })
                    }
                    className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
                  >
                    <option value="none">None</option>
                    <option value="api_key">API Key</option>
                    <option value="bearer">Bearer Token</option>
                  </select>
                </div>
                {form.auth_type !== 'none' && (
                  <div>
                    <label htmlFor="mcp-creds" className="block text-sm font-medium text-gray-700 mb-1">
                      {form.auth_type === 'api_key' ? 'API Key' : 'Bearer Token'}
                    </label>
                    <input
                      id="mcp-creds"
                      type="password"
                      value={form.credentials}
                      onChange={(e) => setForm({ ...form, credentials: e.target.value })}
                      placeholder={editingServer ? 'Leave blank to keep existing' : 'Enter credentials'}
                      className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                )}
                <div className="flex items-center gap-2">
                  <input
                    id="mcp-global"
                    type="checkbox"
                    checked={form.is_global}
                    onChange={(e) => setForm({ ...form, is_global: e.target.checked })}
                    className="rounded text-blue-600"
                  />
                  <label htmlFor="mcp-global" className="text-sm text-gray-700 flex items-center gap-1">
                    <Globe size={14} className="text-blue-500" />
                    Available to all workspaces
                  </label>
                </div>
                <div>
                  <label htmlFor="mcp-refresh" className="block text-sm font-medium text-gray-700 mb-1">
                    Refresh Interval (days)
                  </label>
                  <input
                    id="mcp-refresh"
                    type="number"
                    min={1}
                    value={form.refresh_interval_days}
                    onChange={(e) => setForm({ ...form, refresh_interval_days: parseInt(e.target.value) || 7 })}
                    className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
              </div>
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex justify-end gap-3">
              <button
                onClick={closeModal}
                className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleSave}
                disabled={!form.name.trim() || !form.url.trim() || saving}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
              >
                {saving && <Loader2 size={16} className="animate-spin" />}
                {editingServer ? 'Save Changes' : 'Add Server'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
