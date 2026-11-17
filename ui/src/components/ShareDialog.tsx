// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect } from 'react'
import { X, Loader2, Check, Link, Trash2 } from 'lucide-react'
import {
  getConversation,
  shareConversation,
  type SearchableWorkspace,
} from '../lib/api'

interface ShareDialogProps {
  conversationId: string
  workspaces: SearchableWorkspace[]
  onClose: () => void
  onShareUpdate?: () => void
}

export default function ShareDialog({ conversationId, workspaces, onClose, onShareUpdate }: ShareDialogProps) {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [sharedWorkspaceIds, setSharedWorkspaceIds] = useState<Set<string>>(new Set())
  const [selectedWorkspaceIds, setSelectedWorkspaceIds] = useState<Set<string>>(new Set())
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Only show workspaces with access_type "member" or "org_admin"
  const eligibleWorkspaces = workspaces.filter(
    ws => ws.access_type === 'member' || ws.access_type === 'org_admin'
  )

  useEffect(() => {
    async function loadShareStatus() {
      try {
        const data = await getConversation(conversationId)
        const ids = new Set(data?.conversation?.shared_workspace_ids || [])
        setSharedWorkspaceIds(ids)
        setSelectedWorkspaceIds(new Set(ids))
      } catch (err) {
        // getConversation now throws instead of returning null.
        // For this dialog, both "not shared yet" (404 semantically) and a
        // legitimate server error land here; log so genuine failures
        // aren't silently swallowed. Empty shared_workspace_ids is the
        // safe default in either case.
        console.error('ShareDialog: failed to load share status', err)
      } finally {
        setLoading(false)
      }
    }
    loadShareStatus()
  }, [conversationId])

  async function handleSave() {
    setSaving(true)
    setError(null)
    try {
      // Single PUT call - replaces sharing entirely
      const result = await shareConversation(conversationId, Array.from(selectedWorkspaceIds))
      setSharedWorkspaceIds(new Set(result.shared_workspace_ids))
      onShareUpdate?.()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update sharing')
    } finally {
      setSaving(false)
    }
  }

  async function handleRemoveAll() {
    setSaving(true)
    setError(null)
    try {
      // PUT with empty list = unshare all
      await shareConversation(conversationId, [])
      setSharedWorkspaceIds(new Set())
      setSelectedWorkspaceIds(new Set())
      onShareUpdate?.()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to remove sharing')
    } finally {
      setSaving(false)
    }
  }

  function handleCopyLink() {
    const url = `${window.location.origin}/chat/${conversationId}`
    navigator.clipboard.writeText(url).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  function toggleWorkspace(workspaceId: string) {
    setSelectedWorkspaceIds(prev => {
      const next = new Set(prev)
      if (next.has(workspaceId)) {
        next.delete(workspaceId)
      } else {
        next.add(workspaceId)
      }
      return next
    })
  }

  return (
    <div
      className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-xl shadow-2xl w-full max-w-lg max-h-[80vh] flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b">
          <h2 className="text-lg font-semibold">Share Conversation</h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded">
            <X size={20} />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4">
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="animate-spin text-gray-400" size={24} />
            </div>
          ) : (
            <>
              {error && (
                <div className="mb-4 p-3 bg-red-50 text-red-700 rounded-lg text-sm">
                  {error}
                </div>
              )}

              {/* Copy Link */}
              <div className="mb-4">
                <button
                  onClick={handleCopyLink}
                  className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors text-sm"
                >
                  {copied ? (
                    <>
                      <Check size={16} className="text-green-500" />
                      <span className="text-green-600">Link Copied</span>
                    </>
                  ) : (
                    <>
                      <Link size={16} className="text-gray-500" />
                      <span>Copy Share Link</span>
                    </>
                  )}
                </button>
              </div>

              {/* Workspace checkboxes */}
              <div className="mb-4">
                <p className="text-sm font-medium text-gray-700 mb-2">Share with workspaces</p>
                {eligibleWorkspaces.length === 0 ? (
                  <p className="text-sm text-gray-500">No eligible workspaces available</p>
                ) : (
                  <div className="space-y-1">
                    {eligibleWorkspaces.map(ws => (
                      <label
                        key={ws.workspace_id}
                        className="flex items-center gap-3 px-3 py-2 hover:bg-gray-50 rounded-lg cursor-pointer"
                      >
                        <input
                          type="checkbox"
                          checked={selectedWorkspaceIds.has(ws.workspace_id)}
                          onChange={() => toggleWorkspace(ws.workspace_id)}
                          className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                        />
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium text-gray-900 truncate">{ws.name}</p>
                          <p className="text-xs text-gray-500 capitalize">{ws.access_type}</p>
                        </div>
                      </label>
                    ))}
                  </div>
                )}
              </div>

              {/* Remove All Sharing */}
              {sharedWorkspaceIds.size > 0 && (
                <button
                  onClick={handleRemoveAll}
                  disabled={saving}
                  className="w-full flex items-center justify-center gap-2 px-4 py-2 text-red-600 hover:bg-red-50 rounded-lg transition-colors text-sm disabled:opacity-50"
                >
                  <Trash2 size={16} />
                  Remove All Sharing
                </button>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        {!loading && (
          <div className="flex items-center justify-end gap-2 p-4 border-t">
            <button
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="px-4 py-2 text-sm text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition-colors disabled:opacity-50"
            >
              {saving ? (
                <Loader2 className="animate-spin" size={16} />
              ) : (
                'Save'
              )}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
