import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Loader2, MessageSquare, Users } from 'lucide-react'
import {
  listSharedConversations,
  type SharedConversationListItem,
} from '../lib/api'

export default function SharedConversationsList() {
  const navigate = useNavigate()
  const [conversations, setConversations] = useState<SharedConversationListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [nextCursor, setNextCursor] = useState<string | undefined>()
  const [hasMore, setHasMore] = useState(false)

  useEffect(() => {
    loadInitial()
  }, [])

  async function loadInitial() {
    setLoading(true)
    try {
      const result = await listSharedConversations()
      setConversations(result.conversations)
      setNextCursor(result.next_cursor)
      setHasMore(result.has_more)
    } catch {
      // Silently fail - shared conversations are optional
    } finally {
      setLoading(false)
    }
  }

  async function loadMore() {
    if (!nextCursor || loadingMore) return
    setLoadingMore(true)
    try {
      const result = await listSharedConversations(undefined, undefined, nextCursor)
      setConversations(prev => [...prev, ...result.conversations])
      setNextCursor(result.next_cursor)
      setHasMore(result.has_more)
    } catch {
      // Silently fail
    } finally {
      setLoadingMore(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-4">
        <Loader2 className="animate-spin text-gray-400" size={16} />
      </div>
    )
  }

  if (conversations.length === 0) {
    return null
  }

  return (
    <div>
      <div className="px-4 py-2 text-xs font-medium text-gray-400 uppercase tracking-wider flex items-center gap-1">
        <Users size={12} />
        Shared with me
      </div>
      {conversations.map(conv => (
        <div
          key={conv.conversation_id}
          onClick={() => navigate(`/chat/${conv.conversation_id}`)}
          className="flex items-center gap-2 px-4 py-3 cursor-pointer hover:bg-gray-800 group"
        >
          <MessageSquare size={16} className="text-gray-400 flex-shrink-0" />
          <div className="flex-1 min-w-0">
            <span className="block truncate text-sm">{conv.title}</span>
            <span className="block truncate text-xs text-gray-500">{conv.shared_by}</span>
          </div>
        </div>
      ))}
      {hasMore && (
        <button
          onClick={loadMore}
          disabled={loadingMore}
          className="w-full flex items-center justify-center gap-2 py-2 mt-1 text-sm text-gray-400 hover:text-white hover:bg-gray-700 rounded transition-colors disabled:opacity-50"
        >
          {loadingMore ? (
            <Loader2 className="animate-spin" size={16} />
          ) : (
            'Load More'
          )}
        </button>
      )}
    </div>
  )
}
