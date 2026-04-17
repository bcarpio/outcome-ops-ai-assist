import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ArrowLeft,
  Users,
  Shield,
  User,
  MoreVertical,
  Trash2,
  Loader2,
  Search,
  X,
  Check,
  Plus,
  Server,
} from 'lucide-react'
import AdminNav from '../components/AdminNav'
import {
  listOrgMembers,
  addOrgMember,
  updateOrgMember,
  removeOrgMember,
  type OrgMember,
} from '../lib/api'

export default function OrgSettings() {
  const navigate = useNavigate()
  const [members, setMembers] = useState<OrgMember[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [actionMenuOpen, setActionMenuOpen] = useState<string | null>(null)
  const [updatingRole, setUpdatingRole] = useState<string | null>(null)
  const [showAddModal, setShowAddModal] = useState(false)
  const [newEmail, setNewEmail] = useState('')
  const [newRole, setNewRole] = useState<'org_admin' | 'member'>('member')
  const [adding, setAdding] = useState(false)

  useEffect(() => {
    loadMembers()
  }, [])

  async function loadMembers() {
    setLoading(true)
    setError(null)
    try {
      const data = await listOrgMembers()
      setMembers(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load org members')
    } finally {
      setLoading(false)
    }
  }

  // Filter members based on search query
  const filteredMembers = members.filter((m) => {
    if (!searchQuery.trim()) return true
    const query = searchQuery.toLowerCase()
    return m.user_email.toLowerCase().includes(query)
  })

  async function handleRoleChange(email: string, newRole: 'org_admin' | 'member') {
    setUpdatingRole(email)
    setError(null)
    try {
      await updateOrgMember(email, newRole)
      setMembers(members.map((m) => (m.user_email === email ? { ...m, org_role: newRole } : m)))
      setSuccess(`Updated ${email} to ${newRole === 'org_admin' ? 'Org Admin' : 'Member'}`)
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update role')
    } finally {
      setUpdatingRole(null)
      setActionMenuOpen(null)
    }
  }

  async function handleRemoveUser(email: string) {
    if (!confirm(`Are you sure you want to remove ${email} from the organization? This will also remove them from all workspaces.`)) {
      return
    }
    setError(null)
    try {
      await removeOrgMember(email)
      setMembers(members.filter((m) => m.user_email !== email))
      setSuccess(`Removed ${email} from organization`)
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to remove user')
    }
    setActionMenuOpen(null)
  }

  async function handleAddMember() {
    if (!newEmail.trim()) return
    setAdding(true)
    setError(null)
    try {
      const member = await addOrgMember(newEmail.trim(), newRole)
      setMembers([...members, member])
      setSuccess(`Added ${newEmail} as ${newRole === 'org_admin' ? 'Org Admin' : 'Member'}`)
      setTimeout(() => setSuccess(null), 3000)
      setShowAddModal(false)
      setNewEmail('')
      setNewRole('member')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add member')
    } finally {
      setAdding(false)
    }
  }

  // Count admins to prevent removing last one
  const adminCount = members.filter((m) => m.org_role === 'org_admin').length

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
                <h1 className="text-xl font-semibold">Organization Settings</h1>
                <p className="text-sm text-gray-500">Manage org admins and members</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => navigate('/org/mcp-servers')}
                className="flex items-center gap-2 px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 transition-colors"
              >
                <Server size={18} />
                MCP Servers
              </button>
              <button
                onClick={() => setShowAddModal(true)}
                className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
              >
                <Plus size={18} />
                Add Member
              </button>
              <AdminNav />
            </div>
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
            <Shield className="text-blue-600 mt-0.5" size={20} />
            <div>
              <h3 className="font-medium text-blue-900">About Organization Roles</h3>
              <p className="text-sm text-blue-700 mt-1">
                <strong>Org Admins</strong> can access all workspaces and manage all users.
                <strong className="ml-2">Members</strong> can only access workspaces they're explicitly added to.
              </p>
              <p className="text-sm text-blue-600 mt-2">
                Users are automatically added when they sign in via Azure AD.
              </p>
            </div>
          </div>
        </div>

        {/* Search */}
        <div className="relative mb-6">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search by email..."
            className="w-full sm:w-80 pl-10 pr-4 py-2 border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
        </div>

        {/* Members List */}
        <div className="bg-white rounded-lg border shadow-sm">
          <div className="p-4 border-b">
            <h3 className="font-medium flex items-center gap-2">
              <Users size={18} />
              Organization Members ({members.length})
            </h3>
          </div>

          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-6 h-6 animate-spin text-blue-600" />
            </div>
          ) : filteredMembers.length === 0 ? (
            <div className="text-center py-12">
              <Users className="w-12 h-12 text-gray-300 mx-auto mb-4" />
              <h3 className="text-lg font-medium text-gray-900">
                {searchQuery ? 'No matching members' : 'No members yet'}
              </h3>
              <p className="text-gray-600 mt-1">
                {searchQuery
                  ? 'Try a different search term.'
                  : 'Members will appear here after they sign in via Azure AD.'}
              </p>
            </div>
          ) : (
            <ul className="divide-y">
              {filteredMembers.map((member) => (
                <li
                  key={member.user_email}
                  className="flex items-center justify-between p-4 hover:bg-gray-50"
                >
                  <div className="flex items-center gap-4">
                    <div className="w-10 h-10 bg-gray-100 rounded-full flex items-center justify-center">
                      {member.org_role === 'org_admin' ? (
                        <Shield className="w-5 h-5 text-amber-500" />
                      ) : (
                        <User className="w-5 h-5 text-gray-500" />
                      )}
                    </div>
                    <div>
                      <p className="font-medium text-gray-900">
                        {member.user_email}
                        {member.is_service_account && (
                          <span className="ml-2 text-xs bg-gray-100 text-gray-500 px-1.5 py-0.5 rounded">Service Account</span>
                        )}
                      </p>
                      <p className="text-sm text-gray-500">
                        Joined {new Date(member.joined_at).toLocaleDateString()}
                        {member.invited_by && ` · Invited by ${member.invited_by}`}
                        {member.workspaces && member.workspaces.length > 0 && (
                          <span> · {member.workspaces.length} workspace{member.workspaces.length !== 1 ? 's' : ''}</span>
                        )}
                      </p>
                    </div>
                  </div>

                  <div className="flex items-center gap-4">
                    <span
                      className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium ${
                        member.org_role === 'org_admin'
                          ? 'bg-amber-100 text-amber-700'
                          : 'bg-gray-100 text-gray-700'
                      }`}
                    >
                      {member.org_role === 'org_admin' ? (
                        <Shield className="w-3 h-3" />
                      ) : (
                        <User className="w-3 h-3" />
                      )}
                      {member.org_role === 'org_admin' ? 'Org Admin' : 'Member'}
                    </span>

                    <div className="relative">
                      <button
                        onClick={() =>
                          setActionMenuOpen(
                            actionMenuOpen === member.user_email ? null : member.user_email
                          )
                        }
                        className="p-1 hover:bg-gray-100 rounded"
                      >
                        <MoreVertical className="w-4 h-4 text-gray-400" />
                      </button>

                      {actionMenuOpen === member.user_email && (
                        <>
                          {/* Backdrop */}
                          <div
                            className="fixed inset-0 z-10"
                            onClick={() => setActionMenuOpen(null)}
                          />

                          {/* Menu */}
                          <div className="absolute right-0 mt-1 w-48 bg-white rounded-lg shadow-lg border border-gray-100 py-1 z-20">
                            {member.org_role === 'member' ? (
                              <button
                                onClick={() => handleRoleChange(member.user_email, 'org_admin')}
                                disabled={updatingRole === member.user_email}
                                className="w-full flex items-center gap-2 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                              >
                                {updatingRole === member.user_email ? (
                                  <Loader2 className="w-4 h-4 animate-spin" />
                                ) : (
                                  <Shield className="w-4 h-4" />
                                )}
                                Make Org Admin
                              </button>
                            ) : (
                              <button
                                onClick={() => handleRoleChange(member.user_email, 'member')}
                                disabled={updatingRole === member.user_email || adminCount <= 1}
                                className="w-full flex items-center gap-2 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                                title={adminCount <= 1 ? 'Cannot demote the last org admin' : undefined}
                              >
                                {updatingRole === member.user_email ? (
                                  <Loader2 className="w-4 h-4 animate-spin" />
                                ) : (
                                  <User className="w-4 h-4" />
                                )}
                                Make Member
                              </button>
                            )}
                            <button
                              onClick={() => handleRemoveUser(member.user_email)}
                              disabled={member.org_role === 'org_admin' && adminCount <= 1}
                              className="w-full flex items-center gap-2 px-4 py-2 text-sm text-red-600 hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed"
                              title={member.org_role === 'org_admin' && adminCount <= 1 ? 'Cannot remove the last org admin' : undefined}
                            >
                              <Trash2 className="w-4 h-4" />
                              Remove from Org
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </main>

      {/* Add Member Modal */}
      {showAddModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4">
            <div className="p-6">
              <h2 className="text-lg font-semibold mb-4">Add Organization Member</h2>
              <p className="text-sm text-gray-600 mb-4">
                Add a user to the organization. They will be able to access workspaces based on their role.
              </p>
              {error && (
                <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">
                  {error}
                </div>
              )}
              <div className="space-y-4">
                <div>
                  <label htmlFor="email" className="block text-sm font-medium text-gray-700 mb-1">
                    Email Address
                  </label>
                  <input
                    id="email"
                    type="email"
                    value={newEmail}
                    onChange={(e) => setNewEmail(e.target.value)}
                    placeholder="user@example.com"
                    className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-2">Role</label>
                  <div className="flex gap-4">
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="radio"
                        name="role"
                        value="member"
                        checked={newRole === 'member'}
                        onChange={() => setNewRole('member')}
                        className="text-blue-600"
                      />
                      <User size={16} className="text-gray-500" />
                      <span>Member</span>
                    </label>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="radio"
                        name="role"
                        value="org_admin"
                        checked={newRole === 'org_admin'}
                        onChange={() => setNewRole('org_admin')}
                        className="text-blue-600"
                      />
                      <Shield size={16} className="text-amber-500" />
                      <span>Org Admin</span>
                    </label>
                  </div>
                </div>
              </div>
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex justify-end gap-3">
              <button
                onClick={() => {
                  setShowAddModal(false)
                  setNewEmail('')
                  setNewRole('member')
                  setError(null)
                }}
                className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleAddMember}
                disabled={!newEmail.trim() || adding}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
              >
                {adding && <Loader2 size={16} className="animate-spin" />}
                Add Member
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
