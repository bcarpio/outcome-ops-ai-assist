import { useState, useEffect, useMemo } from 'react'
import { Search, X, MessageSquare, Trash2, Loader2 } from 'lucide-react'
import { listConversations, deleteConversation, type Conversation } from '../lib/api'

interface AllChatsModalProps {
  onClose: () => void
  onSelect: (conv: Conversation) => void
  onDelete: (conversationId: string) => void
}

export default function AllChatsModal({ onClose, onSelect, onDelete }: AllChatsModalProps) {
  const [allConversations, setAllConversations] = useState<Conversation[]>([])
  const [searchQuery, setSearchQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [nextCursor, setNextCursor] = useState<string | undefined>()
  const [hasMore, setHasMore] = useState(false)

  useEffect(() => {
    loadInitial()
  }, [])

  async function loadInitial() {
    setLoading(true)
    const result = await listConversations(50)
    setAllConversations(result.conversations)
    setNextCursor(result.next_cursor)
    setHasMore(result.has_more)
    setLoading(false)
  }

  async function loadMore() {
    if (!nextCursor || loadingMore) return
    setLoadingMore(true)
    const result = await listConversations(50, nextCursor)
    setAllConversations(prev => [...prev, ...result.conversations])
    setNextCursor(result.next_cursor)
    setHasMore(result.has_more)
    setLoadingMore(false)
  }

  const filtered = useMemo(() => {
    if (!searchQuery.trim()) return allConversations
    const q = searchQuery.toLowerCase()
    return allConversations.filter(c => c.title.toLowerCase().includes(q))
  }, [allConversations, searchQuery])

  const grouped = useMemo(() => groupByDate(filtered), [filtered])

  async function handleDelete(e: React.MouseEvent, convId: string) {
    e.stopPropagation()
    await deleteConversation(convId)
    setAllConversations(prev => prev.filter(c => c.conversation_id !== convId))
    onDelete(convId)
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
          <h2 className="text-lg font-semibold">All Chats</h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded">
            <X size={20} />
          </button>
        </div>

        {/* Search */}
        <div className="p-3 border-b">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" size={16} />
            <input
              type="text"
              placeholder="Search conversations..."
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              className="w-full pl-9 pr-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              autoFocus
            />
          </div>
        </div>

        {/* Conversation list */}
        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="animate-spin text-gray-400" size={24} />
            </div>
          ) : filtered.length === 0 ? (
            <div className="text-center py-12 text-gray-500 text-sm">
              {searchQuery ? 'No matching conversations' : 'No conversations yet'}
            </div>
          ) : (
            <>
              {grouped.map(group => (
                <div key={group.label}>
                  <div className="px-4 py-2 text-xs font-medium text-gray-500 bg-gray-50 sticky top-0">
                    {group.label}
                  </div>
                  {group.conversations.map(conv => (
                    <div
                      key={conv.conversation_id}
                      onClick={() => onSelect(conv)}
                      className="flex items-center justify-between px-4 py-3 hover:bg-gray-50 cursor-pointer group"
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <MessageSquare className="text-gray-400 shrink-0" size={16} />
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-gray-900 truncate">{conv.title}</p>
                          <p className="text-xs text-gray-500">
                            {conv.message_count} messages
                          </p>
                        </div>
                      </div>
                      <button
                        onClick={e => handleDelete(e, conv.conversation_id)}
                        className="p-1 text-gray-300 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  ))}
                </div>
              ))}

              {hasMore && (
                <div className="p-3 border-t">
                  <button
                    onClick={loadMore}
                    disabled={loadingMore}
                    className="w-full py-2 text-sm text-blue-600 hover:bg-blue-50 rounded-lg transition-colors disabled:opacity-50"
                  >
                    {loadingMore ? (
                      <Loader2 className="animate-spin mx-auto" size={16} />
                    ) : (
                      'Show More'
                    )}
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function groupByDate(conversations: Conversation[]) {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const yesterday = new Date(today.getTime() - 86400000)
  const weekAgo = new Date(today.getTime() - 7 * 86400000)

  const groups: { label: string; conversations: Conversation[] }[] = [
    { label: 'Today', conversations: [] },
    { label: 'Yesterday', conversations: [] },
    { label: 'Previous 7 Days', conversations: [] },
    { label: 'Older', conversations: [] },
  ]

  for (const conv of conversations) {
    const date = new Date(conv.updated_at)
    if (date >= today) groups[0].conversations.push(conv)
    else if (date >= yesterday) groups[1].conversations.push(conv)
    else if (date >= weekAgo) groups[2].conversations.push(conv)
    else groups[3].conversations.push(conv)
  }

  return groups.filter(g => g.conversations.length > 0)
}
