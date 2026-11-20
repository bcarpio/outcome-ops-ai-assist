// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router'
import { Bell, X, AlertTriangle, AlertCircle } from 'lucide-react'
import {
  listNotifications,
  dismissNotification,
  type Notification,
} from '../lib/api'

export const NOTIFICATION_POLL_MS = 60_000
const MAX_ITEMS = 5
const MAX_FILES = 5

function sortNewestFirst(items: Notification[]): Notification[] {
  return [...items].sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
}

/**
 * Bell with a count of the caller's visible notifications (stalled repos,
 * failed files). Hidden when there is nothing to show. Polls every 60s
 * while the page is visible and refreshes when the tab becomes visible.
 */
export default function NotificationBell() {
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const items = await listNotifications()
      setNotifications(sortNewestFirst(Array.isArray(items) ? items : []))
    } catch {
      // Best-effort: keep the last known list on a transient failure.
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(() => {
      if (typeof document === 'undefined' || document.visibilityState !== 'hidden') {
        refresh()
      }
    }, NOTIFICATION_POLL_MS)
    const onVisibility = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [refresh])

  useEffect(() => {
    if (!open) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open])

  const handleDismiss = async (item: Notification) => {
    setError(null)
    setNotifications((prev) => prev.filter((n) => n.notification_id !== item.notification_id))
    try {
      await dismissNotification(item.workspace_id, item.notification_id)
    } catch (err) {
      setNotifications((prev) =>
        prev.some((n) => n.notification_id === item.notification_id)
          ? prev
          : sortNewestFirst([...prev, item]),
      )
      setError(err instanceof Error ? err.message : 'Failed to dismiss notification')
    }
  }

  if (notifications.length === 0 && !error) return null

  const count = notifications.length
  const visible = notifications.slice(0, MAX_ITEMS)

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        aria-label={`Notifications (${count})`}
        aria-expanded={open}
        className="relative p-2 hover:bg-gray-100 rounded-lg text-gray-600 hover:text-gray-800 transition-colors"
      >
        <Bell size={20} />
        {count > 0 && (
          <span
            data-testid="notification-count"
            className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 flex items-center justify-center text-[10px] font-semibold text-white bg-red-600 rounded-full"
          >
            {count}
          </span>
        )}
      </button>

      {open && (
        <>
          <button
            type="button"
            aria-label="Close notifications"
            tabIndex={-1}
            className="fixed inset-0 z-10 cursor-default"
            onClick={() => setOpen(false)}
          />
          <section
            aria-label="Notifications"
            className="absolute right-0 mt-2 w-96 max-h-[70vh] overflow-y-auto bg-white border border-gray-200 rounded-lg shadow-lg z-20"
          >
            <div className="px-4 py-2 border-b text-sm font-medium text-gray-700">
              Notifications
            </div>
            {error && (
              <div role="alert" className="px-4 py-2 text-sm text-red-700 bg-red-50 border-b border-red-100">
                {error}
              </div>
            )}
            {visible.length === 0 ? (
              <div className="px-4 py-3 text-sm text-gray-500">No notifications</div>
            ) : (
              <ul className="divide-y divide-gray-100">
                {visible.map((n) => {
                  const isError = n.severity === 'error'
                  const files = n.failed_files || []
                  const total = Math.max(n.failed_files_total || 0, files.length)
                  const shown = files.slice(0, MAX_FILES)
                  const more = total - shown.length
                  return (
                    <li
                      key={n.notification_id}
                      className={`px-4 py-3 border-l-4 ${isError ? 'border-red-500 bg-red-50/40' : 'border-amber-500 bg-amber-50/40'}`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="flex items-start gap-2 min-w-0">
                          {isError ? (
                            <AlertCircle size={16} className="text-red-600 mt-0.5 shrink-0" />
                          ) : (
                            <AlertTriangle size={16} className="text-amber-600 mt-0.5 shrink-0" />
                          )}
                          <div className="min-w-0">
                            <p className={`text-sm font-medium ${isError ? 'text-red-800' : 'text-amber-800'}`}>
                              {n.title}
                            </p>
                            <p className="text-xs text-gray-500 truncate">
                              {n.workspace_id}
                              {n.repo_name ? ` / ${n.repo_name}` : ''}
                            </p>
                          </div>
                        </div>
                        <button
                          onClick={() => handleDismiss(n)}
                          aria-label={`Dismiss ${n.title}`}
                          className="p-1 text-gray-400 hover:text-gray-700 hover:bg-gray-100 rounded"
                        >
                          <X size={14} />
                        </button>
                      </div>
                      {n.message && <p className="mt-1 text-sm text-gray-700">{n.message}</p>}
                      {shown.length > 0 && (
                        <ul className="mt-1 text-xs text-gray-600 font-mono list-disc list-inside">
                          {shown.map((f) => (
                            <li key={f} className="truncate">{f}</li>
                          ))}
                          {more > 0 && (
                            <li className="list-none font-sans text-gray-500">and {more} more</li>
                          )}
                        </ul>
                      )}
                      <Link
                        to={`/workspaces/${n.workspace_id}/settings`}
                        onClick={() => setOpen(false)}
                        className="mt-2 inline-block text-xs text-blue-600 hover:underline"
                      >
                        Workspace settings
                      </Link>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  )
}
