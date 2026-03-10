import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ArrowLeft,
  Plus,
  Loader2,
  Trash2,
  Key,
  Copy,
  Check,
  X,
  ChevronDown,
  ChevronUp,
  User,
  Mail,
} from 'lucide-react'
import {
  getCurrentUser,
  listApiKeys,
  createApiKey,
  revokeApiKey,
  type UserProfile,
  type ApiKey,
} from '../lib/api'

const EXPIRATION_OPTIONS = [
  { label: 'No expiration', value: 0 },
  { label: '30 days', value: 30 },
  { label: '60 days', value: 60 },
  { label: '90 days', value: 90 },
  { label: '180 days', value: 180 },
  { label: '365 days', value: 365 },
]

export default function Settings() {
  const navigate = useNavigate()
  const [user, setUser] = useState<UserProfile | null>(null)
  const [keys, setKeys] = useState<ApiKey[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  // Create modal state
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [keyName, setKeyName] = useState('')
  const [expiresInDays, setExpiresInDays] = useState(0)
  const [saving, setSaving] = useState(false)

  // Newly created key (shown once)
  const [newKey, setNewKey] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  // Revoke confirmation
  const [revokingId, setRevokingId] = useState<string | null>(null)

  // Connection guide
  const [showGuide, setShowGuide] = useState(false)

  useEffect(() => {
    loadData()
  }, [])

  useEffect(() => {
    if (success) {
      const timer = setTimeout(() => setSuccess(null), 3000)
      return () => clearTimeout(timer)
    }
  }, [success])

  async function loadData() {
    setLoading(true)
    setError(null)
    try {
      const [userData, keysData] = await Promise.all([
        getCurrentUser(),
        listApiKeys(),
      ])
      setUser(userData)
      setKeys(keysData)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load settings')
    } finally {
      setLoading(false)
    }
  }

  async function loadKeys() {
    try {
      const data = await listApiKeys()
      setKeys(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load API keys')
    }
  }

  async function handleCreate() {
    if (!keyName.trim()) return
    setSaving(true)
    setError(null)
    try {
      const request: { name: string; expires_in_days?: number } = { name: keyName.trim() }
      if (expiresInDays > 0) {
        request.expires_in_days = expiresInDays
      }
      const result = await createApiKey(request)
      setNewKey(result.key)
      setCopied(false)
      await loadKeys()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create API key')
    } finally {
      setSaving(false)
    }
  }

  async function handleRevoke(keyId: string) {
    setError(null)
    try {
      await revokeApiKey(keyId)
      setSuccess('API key revoked')
      setRevokingId(null)
      await loadKeys()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke API key')
    }
  }

  function handleCopy() {
    if (newKey) {
      navigator.clipboard.writeText(newKey)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  function closeCreateModal() {
    setShowCreateModal(false)
    setNewKey(null)
    setKeyName('')
    setExpiresInDays(0)
    setCopied(false)
  }

  function formatDate(dateStr: string | null | undefined): string {
    if (!dateStr) return 'Never'
    return new Date(dateStr).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    })
  }

  const activeKeys = keys.filter((k) => k.status === 'active')
  const revokedKeys = keys.filter((k) => k.status === 'revoked')

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b">
        <div className="max-w-4xl mx-auto px-4 py-4">
          <div className="flex items-center gap-3">
            <button
              onClick={() => navigate('/workspaces')}
              className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
            >
              <ArrowLeft size={20} />
            </button>
            <div>
              <h1 className="text-xl font-semibold">Settings</h1>
              <p className="text-sm text-gray-500">Your profile and API keys</p>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-8">
        {error && !showCreateModal && (
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

        {loading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="w-6 h-6 animate-spin text-blue-600" />
          </div>
        ) : (
          <>
            {/* Profile Section */}
            <div className="bg-white rounded-lg border shadow-sm mb-6">
              <div className="p-4 border-b">
                <h3 className="font-medium flex items-center gap-2">
                  <User size={18} />
                  Profile
                </h3>
              </div>
              <div className="p-4">
                <div className="flex items-center gap-4">
                  <div className="w-14 h-14 bg-blue-100 rounded-full flex items-center justify-center flex-shrink-0">
                    <User className="w-7 h-7 text-blue-600" />
                  </div>
                  <div>
                    {user?.name && (
                      <div className="font-medium text-gray-900 text-lg">{user.name}</div>
                    )}
                    <div className="flex items-center gap-1.5 text-gray-500">
                      <Mail size={14} />
                      <span>{user?.email || 'Unknown'}</span>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {/* API Keys Section */}
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold flex items-center gap-2">
                <Key size={20} />
                API Keys
              </h2>
              <button
                onClick={() => { setShowCreateModal(true); setNewKey(null); setKeyName(''); setExpiresInDays(0) }}
                className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
              >
                <Plus size={18} />
                Create API Key
              </button>
            </div>

            {/* Info Banner */}
            <div className="mb-6 p-4 bg-blue-50 border border-blue-200 rounded-lg">
              <div className="flex items-start gap-3">
                <Key className="text-blue-600 mt-0.5" size={20} />
                <div>
                  <h3 className="font-medium text-blue-900">About API Keys</h3>
                  <p className="text-sm text-blue-700 mt-1">
                    API keys let you connect <strong>MCP-compatible tools</strong> (Claude Code, VS Code, Cursor) to the OutcomeOps knowledge base.
                    Each key is personal and scoped to your workspace access.
                  </p>
                </div>
              </div>
            </div>

            {/* Active Keys */}
            <div className="bg-white rounded-lg border shadow-sm">
              <div className="p-4 border-b">
                <h3 className="font-medium flex items-center gap-2">
                  <Key size={18} />
                  Active Keys ({activeKeys.length})
                </h3>
              </div>

              {activeKeys.length === 0 ? (
                <div className="text-center py-12">
                  <Key className="w-12 h-12 text-gray-300 mx-auto mb-4" />
                  <h3 className="text-lg font-medium text-gray-900">No API keys yet</h3>
                  <p className="text-gray-600 mt-1">
                    Create one to connect MCP clients like Claude Code or VS Code.
                  </p>
                </div>
              ) : (
                <div className="divide-y">
                  {activeKeys.map((key) => (
                    <div key={key.key_id} className="flex items-center justify-between p-4 hover:bg-gray-50">
                      <div className="flex items-center gap-4 min-w-0 flex-1">
                        <div className="w-10 h-10 bg-green-50 rounded-full flex items-center justify-center flex-shrink-0">
                          <Key className="w-5 h-5 text-green-600" />
                        </div>
                        <div className="min-w-0">
                          <div className="font-medium text-gray-900">{key.name}</div>
                          <div className="text-sm text-gray-500 flex items-center gap-3 mt-0.5">
                            <span className="font-mono">{key.key_prefix}...</span>
                            <span>Created {formatDate(key.created_at)}</span>
                            {key.expires_at && <span>Expires {formatDate(key.expires_at)}</span>}
                            <span>Last used {formatDate(key.last_used_at)}</span>
                          </div>
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        {revokingId === key.key_id ? (
                          <div className="flex items-center gap-2">
                            <span className="text-sm text-red-600">Revoke this key?</span>
                            <button
                              onClick={() => handleRevoke(key.key_id)}
                              className="px-3 py-1 bg-red-600 text-white text-sm rounded hover:bg-red-700"
                            >
                              Confirm
                            </button>
                            <button
                              onClick={() => setRevokingId(null)}
                              className="px-3 py-1 bg-gray-100 text-gray-700 text-sm rounded hover:bg-gray-200"
                            >
                              Cancel
                            </button>
                          </div>
                        ) : (
                          <button
                            onClick={() => setRevokingId(key.key_id)}
                            className="p-2 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                            title="Revoke key"
                          >
                            <Trash2 size={18} />
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Revoked Keys */}
            {revokedKeys.length > 0 && (
              <div className="bg-white rounded-lg border shadow-sm mt-6">
                <div className="p-4 border-b">
                  <h3 className="font-medium flex items-center gap-2 text-gray-500">
                    <Key size={18} />
                    Revoked Keys ({revokedKeys.length})
                  </h3>
                </div>
                <div className="divide-y">
                  {revokedKeys.map((key) => (
                    <div key={key.key_id} className="flex items-center gap-4 p-4 opacity-60">
                      <div className="w-10 h-10 bg-gray-100 rounded-full flex items-center justify-center flex-shrink-0">
                        <Key className="w-5 h-5 text-gray-400" />
                      </div>
                      <div className="min-w-0">
                        <div className="font-medium text-gray-500">{key.name}</div>
                        <div className="text-sm text-gray-400 flex items-center gap-3 mt-0.5">
                          <span className="font-mono">{key.key_prefix}...</span>
                          <span>Created {formatDate(key.created_at)}</span>
                          <span className="text-red-400">Revoked</span>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Connection Guide */}
            <div className="bg-white rounded-lg border shadow-sm mt-6">
              <button
                onClick={() => setShowGuide(!showGuide)}
                className="w-full p-4 flex items-center justify-between text-left hover:bg-gray-50 transition-colors"
              >
                <h3 className="font-medium flex items-center gap-2">
                  <Key size={18} />
                  Connection Guide
                </h3>
                {showGuide ? <ChevronUp size={18} /> : <ChevronDown size={18} />}
              </button>
              {showGuide && (
                <div className="px-4 pb-4 border-t pt-4">
                  <p className="text-sm text-gray-600 mb-4">
                    Configure your MCP client with the following settings. Replace the URL with your OutcomeOps domain and the key with your API key.
                  </p>
                  <h4 className="font-medium text-sm mb-2">Claude Code</h4>
                  <pre className="bg-gray-900 text-gray-100 text-sm p-4 rounded-lg overflow-x-auto mb-4">{`{
  "mcpServers": {
    "outcomeops": {
      "type": "streamableHttp",
      "url": "https://your-outcomeops-domain.com/mcp/v1",
      "headers": {
        "Authorization": "Bearer ooa_your_key_here"
      }
    }
  }
}`}</pre>
                  <h4 className="font-medium text-sm mb-2">VS Code / Cursor</h4>
                  <p className="text-sm text-gray-600">
                    Add the same configuration to your editor's MCP settings file. See the{' '}
                    <a href="/docs/mcp-server.md" className="text-blue-600 hover:underline">full documentation</a> for details.
                  </p>
                </div>
              )}
            </div>
          </>
        )}
      </main>

      {/* Create Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4">
            <div className="flex items-center justify-between p-6 border-b">
              <h2 className="text-lg font-semibold">{newKey ? 'API Key Created' : 'Create API Key'}</h2>
              <button onClick={closeCreateModal} className="p-2 text-gray-400 hover:text-gray-600 rounded-lg">
                <X size={20} />
              </button>
            </div>

            <div className="p-6">
              {newKey ? (
                <div>
                  <div className="mb-4 p-3 bg-yellow-50 border border-yellow-200 rounded-lg text-yellow-800 text-sm">
                    This key will only be shown once. Copy it now.
                  </div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Your API Key</label>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      readOnly
                      value={newKey}
                      className="flex-1 px-3 py-2 border rounded-lg bg-gray-50 font-mono text-sm"
                    />
                    <button
                      onClick={handleCopy}
                      className="flex items-center gap-1 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
                    >
                      {copied ? <Check size={16} /> : <Copy size={16} />}
                      {copied ? 'Copied' : 'Copy'}
                    </button>
                  </div>
                </div>
              ) : (
                <div className="space-y-4">
                  {error && (
                    <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">
                      {error}
                    </div>
                  )}
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Key Name</label>
                    <input
                      type="text"
                      value={keyName}
                      onChange={(e) => setKeyName(e.target.value)}
                      placeholder="e.g., Claude Code - Laptop"
                      className="w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                      autoFocus
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Expiration</label>
                    <select
                      value={expiresInDays}
                      onChange={(e) => setExpiresInDays(Number(e.target.value))}
                      className="w-full px-3 py-2 border rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    >
                      {EXPIRATION_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              )}
            </div>

            <div className="flex justify-end gap-3 p-6 border-t">
              {newKey ? (
                <button
                  onClick={closeCreateModal}
                  className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors"
                >
                  Done
                </button>
              ) : (
                <>
                  <button
                    onClick={closeCreateModal}
                    className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleCreate}
                    disabled={!keyName.trim() || saving}
                    className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
                  >
                    {saving && <Loader2 size={16} className="animate-spin" />}
                    Create Key
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
