/**
 * API client for OutcomeOps Chat
 */

// Chat API endpoint - uses same origin (Fargate server proxies to Lambda)
// For local dev, can override with VITE_API_URL env var
const CHAT_URL = import.meta.env.VITE_API_URL || '/api/chat'
const API_BASE = '/api'

export interface Source {
  repo: string
  type: string
  path: string
}

export interface StreamMetadata {
  type: 'metadata'
  conversation_id: string
  sources: Source[]
  title: string
}

export interface StreamToken {
  type: 'token'
  text: string
}

export interface StreamDone {
  type: 'done'
  message_id: string
  cost?: {
    input_tokens: number
    output_tokens: number
    cost: number
  }
}

export interface StreamError {
  type: 'error'
  error: string
  status_code?: number
}

export type StreamEvent = StreamMetadata | StreamToken | StreamDone | StreamError | StreamToolUse

export interface StreamToolUse {
  type: 'tool_use'
  tool: string
  status: string
}

export interface ChatRequest {
  conversation_id?: string
  message: string
  top_k?: number
  advanced?: boolean
  show_cost?: boolean
  workspace_ids?: string[]
  mcp_server_ids?: string[]
  prompt_workspace_id?: string
}

export interface Conversation {
  conversation_id: string
  title: string
  message_count: number
  created_at: string
  updated_at: string
  shared_workspace_ids?: string[]
  is_owner?: boolean
  owner_email?: string
}

export interface Message {
  message_id: string
  role: 'user' | 'assistant'
  content: string
  sources?: Source[]
  created_at: string
}

/**
 * Send a chat message and stream the response
 */
export async function sendChatMessage(
  request: ChatRequest,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  const response = await fetch(CHAT_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(request),
  })

  if (!response.ok) {
    throw new Error(`HTTP error: ${response.status}`)
  }

  const reader = response.body?.getReader()
  if (!reader) {
    throw new Error('No response body')
  }

  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })

    // Process complete lines (NDJSON)
    const lines = buffer.split('\n')
    buffer = lines.pop() || '' // Keep incomplete line in buffer

    for (const line of lines) {
      if (line.trim()) {
        try {
          const event = JSON.parse(line) as StreamEvent
          onEvent(event)
        } catch (e) {
          console.error('Failed to parse event:', line, e)
        }
      }
    }
  }

  // Process any remaining buffer
  if (buffer.trim()) {
    try {
      const event = JSON.parse(buffer) as StreamEvent
      onEvent(event)
    } catch (e) {
      console.error('Failed to parse final event:', buffer, e)
    }
  }
}

export interface ConversationListResponse {
  conversations: Conversation[]
  next_cursor?: string
  has_more: boolean
}

/**
 * List conversations from server with pagination support
 */
export async function listConversations(
  limit: number = 20,
  cursor?: string
): Promise<ConversationListResponse> {
  try {
    const params = new URLSearchParams({ limit: String(limit) })
    if (cursor) params.set('cursor', cursor)

    const response = await fetch(`${API_BASE}/conversations?${params}`)
    if (!response.ok) {
      console.error('Failed to list conversations:', response.status)
      return { conversations: [], has_more: false }
    }
    const data = await response.json()
    return {
      conversations: (data.conversations || []).map((c: Record<string, unknown>) => ({
        conversation_id: c.conversation_id as string,
        title: (c.title as string) || 'Untitled',
        message_count: (c.message_count as number) || 0,
        created_at: c.created_at as string,
        updated_at: c.updated_at as string,
      })),
      next_cursor: data.next_cursor,
      has_more: data.has_more ?? false,
    }
  } catch (error) {
    console.error('Error listing conversations:', error)
    return { conversations: [], has_more: false }
  }
}

/**
 * Get a conversation with its messages from server
 */
export async function getConversation(conversationId: string): Promise<{ conversation: Conversation; messages: Message[] } | null> {
  try {
    const response = await fetch(`${API_BASE}/conversations/${conversationId}`)
    if (!response.ok) {
      console.error('Failed to get conversation:', response.status)
      return null
    }
    return await response.json()
  } catch (error) {
    console.error('Error getting conversation:', error)
    return null
  }
}

/**
 * Delete a conversation on the server
 */
export async function deleteConversation(conversationId: string): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/conversations/${conversationId}`, {
      method: 'DELETE',
    })
    return response.ok
  } catch (error) {
    console.error('Error deleting conversation:', error)
    return false
  }
}

// ============================================================================
// Workspace Types and API
// ============================================================================

export interface Workspace {
  workspace_id: string
  name: string
  description?: string
  created_at: string
  updated_at: string
  created_by: string
  role?: 'workspace_admin' | 'member' | 'org_admin'
}

export interface WorkspaceMember {
  user_email: string
  role: 'workspace_admin' | 'member'
  joined_at: string
  workspace_id: string
}

export interface WorkspaceRepo {
  repo_name: string
  workspace_id: string
  enabled: boolean
  added_at: string
  added_by: string
}

export interface AvailableRepo {
  full_name: string
  name: string
  description: string
  private: boolean
  updated_at: string
}

/**
 * List user's workspaces
 */
export async function listWorkspaces(): Promise<Workspace[]> {
  const response = await fetch(`${API_BASE}/workspaces`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list workspaces: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.workspaces as Workspace[]) || []
}

/**
 * Helper to safely parse JSON response, returning null if empty
 */
async function safeJson(response: Response): Promise<Record<string, unknown> | null> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

/**
 * Create a new workspace
 */
export async function createWorkspace(name: string, description?: string): Promise<Workspace> {
  const response = await fetch(`${API_BASE}/workspaces`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to create workspace: ${response.status}`)
  }
  const data = await safeJson(response)
  if (!data?.workspace) {
    throw new Error('Invalid response from server')
  }
  return data.workspace as Workspace
}

/**
 * Get workspace details
 */
export async function getWorkspace(workspaceId: string): Promise<Workspace> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get workspace: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.workspace as Workspace
}

/**
 * Update workspace
 */
export async function updateWorkspace(
  workspaceId: string,
  updates: { name?: string; description?: string }
): Promise<Workspace> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to update workspace: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.workspace as Workspace
}

/**
 * Delete workspace
 */
export async function deleteWorkspace(workspaceId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to delete workspace: ${response.status}`)
  }
}

/**
 * List workspace members
 */
export async function listMembers(workspaceId: string): Promise<WorkspaceMember[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/members`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list members: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.members as WorkspaceMember[]) || []
}

/**
 * Add member to workspace
 */
export async function addMember(
  workspaceId: string,
  email: string,
  role: 'member' | 'workspace_admin' = 'member'
): Promise<WorkspaceMember> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/members`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, role }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to add member: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.member as WorkspaceMember
}

/**
 * Update member role
 */
export async function updateMember(
  workspaceId: string,
  email: string,
  role: 'member' | 'workspace_admin'
): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/members/${encodeURIComponent(email)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to update member: ${response.status}`)
  }
}

/**
 * Remove member from workspace
 */
export async function removeMember(workspaceId: string, email: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/members/${encodeURIComponent(email)}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to remove member: ${response.status}`)
  }
}

/**
 * List workspace repos
 */
export async function listWorkspaceRepos(workspaceId: string): Promise<WorkspaceRepo[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/repos`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list repos: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.repos as WorkspaceRepo[]) || []
}

/**
 * List available repos from GitHub
 */
export async function listAvailableRepos(workspaceId: string): Promise<AvailableRepo[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/repos/available`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list available repos: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.repos as AvailableRepo[]) || []
}

/**
 * Add repo to workspace
 */
export async function addWorkspaceRepo(workspaceId: string, repoName: string): Promise<WorkspaceRepo> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/repos`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo_name: repoName }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to add repo: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.repo as WorkspaceRepo
}

/**
 * Remove repo from workspace
 */
export async function removeWorkspaceRepo(workspaceId: string, repoName: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/repos/${encodeURIComponent(repoName)}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to remove repo: ${response.status}`)
  }
}

// ============================================================================
// Confluence Integration Types and API
// ============================================================================

export interface ConfluenceConnection {
  connection_id: string
  workspace_id: string
  cloud_id: string
  site_name: string
  site_url: string
  selected_spaces: ConfluenceSpace[]
  include_attachments: boolean
  last_sync_at: string | null
  last_sync_status: 'needs_configuration' | 'queued' | 'syncing' | 'completed' | 'failed'
  page_count: number
  attachment_count: number
  created_at: string
  updated_at: string
}

export interface ConfluenceSpace {
  id: string
  key: string
  name: string
  type: string
  selected?: boolean
}

/**
 * Get Confluence OAuth authorization URL
 */
export async function getConfluenceAuthUrl(workspaceId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/confluence/authorize?workspace_id=${encodeURIComponent(workspaceId)}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get auth URL: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.auth_url as string
}

/**
 * List Confluence connections for a workspace
 */
export async function listConfluenceConnections(workspaceId: string): Promise<ConfluenceConnection[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/confluence/connections`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list connections: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.connections as ConfluenceConnection[]) || []
}

/**
 * List available Confluence spaces for a connection
 */
export async function listConfluenceSpaces(
  workspaceId: string,
  connectionId: string
): Promise<{ spaces: ConfluenceSpace[]; selected_count: number }> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/confluence/connections/${connectionId}/spaces`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list spaces: ${response.status}`)
  }
  const data = await safeJson(response)
  return {
    spaces: (data?.spaces as ConfluenceSpace[]) || [],
    selected_count: (data?.selected_count as number) || 0,
  }
}

/**
 * Update selected Confluence spaces for a connection
 */
export async function updateConfluenceSpaces(
  workspaceId: string,
  connectionId: string,
  selectedSpaces: ConfluenceSpace[],
  includeAttachments: boolean = false
): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/confluence/connections/${connectionId}/spaces`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      selected_spaces: selectedSpaces,
      include_attachments: includeAttachments,
    }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to update spaces: ${response.status}`)
  }
}

/**
 * Trigger Confluence sync for a connection
 */
export async function triggerConfluenceSync(workspaceId: string, connectionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/confluence/connections/${connectionId}/sync`, {
    method: 'POST',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to trigger sync: ${response.status}`)
  }
}

/**
 * Delete a Confluence connection
 */
export async function deleteConfluenceConnection(workspaceId: string, connectionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/confluence/connections/${connectionId}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to delete connection: ${response.status}`)
  }
}

// ============================================================================
// User Search API
// ============================================================================

export interface SearchUser {
  user_email: string
  name: string
  is_service_account: boolean
}

export async function searchUsers(query: string): Promise<SearchUser[]> {
  const response = await fetch(`${API_BASE}/users/search?q=${encodeURIComponent(query)}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to search users: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.users as SearchUser[]) || []
}

// ============================================================================
// Org Management Types and API
// ============================================================================

export interface OrgMemberWorkspace {
  workspace_id: string
  role: string
}

export interface OrgMember {
  user_email: string
  org_role: 'org_admin' | 'member'
  joined_at: string
  invited_by?: string
  is_service_account?: boolean
  workspaces?: OrgMemberWorkspace[]
}

/**
 * List all org members (org_admin only)
 */
export async function listOrgMembers(): Promise<OrgMember[]> {
  const response = await fetch(`${API_BASE}/org/members`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list org members: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.members as OrgMember[]) || []
}

/**
 * Add org member (org_admin only)
 */
export async function addOrgMember(
  email: string,
  orgRole: 'org_admin' | 'member' = 'member'
): Promise<OrgMember> {
  const response = await fetch(`${API_BASE}/org/members`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, org_role: orgRole }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to add org member: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.member as OrgMember
}

/**
 * Update org member role (org_admin only)
 */
export async function updateOrgMember(
  email: string,
  orgRole: 'org_admin' | 'member'
): Promise<void> {
  const response = await fetch(`${API_BASE}/org/members/${encodeURIComponent(email)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ org_role: orgRole }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to update org member: ${response.status}`)
  }
}

/**
 * Remove org member (org_admin only)
 */
export async function removeOrgMember(email: string): Promise<void> {
  const response = await fetch(`${API_BASE}/org/members/${encodeURIComponent(email)}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to remove org member: ${response.status}`)
  }
}

// ============================================================================
// Workspace Sharing Types and API
// ============================================================================

export interface WorkspaceSearchAccess {
  workspace_id: string
  name: string
}

/**
 * Get which workspaces can search this workspace (workspace_admin only)
 */
export async function getSearchAccess(workspaceId: string): Promise<WorkspaceSearchAccess[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/sharing`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get search access: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.search_allowed_workspaces as WorkspaceSearchAccess[]) || []
}

/**
 * Update which workspaces can search this workspace (workspace_admin only)
 */
export async function updateSearchAccess(
  workspaceId: string,
  allowedWorkspaceIds: string[]
): Promise<WorkspaceSearchAccess[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/sharing`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ search_allowed_workspaces: allowedWorkspaceIds }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to update search access: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.search_allowed_workspaces as WorkspaceSearchAccess[]) || []
}

/**
 * List all workspaces available for sharing (workspace_admin only)
 */
export async function listSharingOptions(workspaceId: string): Promise<WorkspaceSearchAccess[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/sharing/available`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list sharing options: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.workspaces as WorkspaceSearchAccess[]) || []
}

// ============================================================================
// Searchable Workspaces for Chat
// ============================================================================

export interface SearchableWorkspace {
  workspace_id: string
  name: string
  access_type: 'member' | 'shared' | 'org_admin'
}

/**
 * Get workspaces the user can search in chat
 */
export async function getSearchableWorkspaces(): Promise<SearchableWorkspace[]> {
  const response = await fetch(`${API_BASE}/chat/searchable-workspaces`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get searchable workspaces: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.workspaces as SearchableWorkspace[]) || []
}

// ============================================================================
// Chat Available Prompts
// ============================================================================

export interface AvailablePrompt {
  workspace_id: string
  workspace_name: string
  prompt_preview: string
}

/**
 * Get workspaces with custom system prompts available for chat
 */
export async function getChatAvailablePrompts(): Promise<AvailablePrompt[]> {
  const response = await fetch(`${API_BASE}/chat/available-prompts`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get available prompts: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.prompts as AvailablePrompt[]) || []
}

// ============================================================================
// Document Upload Types and API
// ============================================================================

export interface WorkspaceDocument {
  path: string
  s3_key: string
  filename: string
  size: number
  last_modified: string
}

export interface DocumentUploadUrlResponse {
  upload_url: string
  s3_key: string
  bucket: string
  expires_in: number
}

/**
 * Get a presigned URL for document upload
 */
export async function getDocumentUploadUrl(
  workspaceId: string,
  filename: string,
  contentType: string = 'application/octet-stream',
  folderPath: string = ''
): Promise<DocumentUploadUrlResponse> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/documents/upload`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      filename,
      content_type: contentType,
      folder_path: folderPath,
    }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get upload URL: ${response.status}`)
  }
  const data = await safeJson(response)
  if (!data) {
    throw new Error('Empty response from server')
  }
  return {
    upload_url: data.upload_url as string,
    s3_key: data.s3_key as string,
    bucket: data.bucket as string,
    expires_in: data.expires_in as number,
  }
}

/**
 * Upload a document directly to S3 using presigned URL with progress tracking
 */
export async function uploadDocumentToS3(
  uploadUrl: string,
  file: File,
  onProgress?: (progress: number) => void
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()

    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && onProgress) {
        const progress = Math.round((event.loaded / event.total) * 100)
        onProgress(progress)
      }
    })

    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve()
      } else {
        reject(new Error(`Upload failed: ${xhr.status} ${xhr.statusText}`))
      }
    })

    xhr.addEventListener('error', () => {
      reject(new Error('Upload failed: Network error'))
    })

    xhr.addEventListener('abort', () => {
      reject(new Error('Upload aborted'))
    })

    xhr.open('PUT', uploadUrl)
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream')
    xhr.send(file)
  })
}

/**
 * List documents in a workspace
 */
export async function listDocuments(
  workspaceId: string,
  prefix: string = ''
): Promise<WorkspaceDocument[]> {
  const url = prefix
    ? `${API_BASE}/workspaces/${workspaceId}/documents?prefix=${encodeURIComponent(prefix)}`
    : `${API_BASE}/workspaces/${workspaceId}/documents`

  const response = await fetch(url)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list documents: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.documents as WorkspaceDocument[]) || []
}

/**
 * Delete a document from workspace
 */
export async function deleteDocument(workspaceId: string, documentPath: string): Promise<void> {
  const response = await fetch(
    `${API_BASE}/workspaces/${workspaceId}/documents/${encodeURIComponent(documentPath)}`,
    { method: 'DELETE' }
  )
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to delete document: ${response.status}`)
  }
}

// ============================================================================
// Jira Integration Types and API
// ============================================================================

export interface JiraConnection {
  connection_id: string
  workspace_id: string
  cloud_id: string
  site_name: string
  site_url: string
  selected_projects: JiraProject[]
  include_attachments: boolean
  include_comments: boolean
  last_sync_at: string | null
  last_sync_status: 'needs_configuration' | 'queued' | 'in_progress' | 'success' | 'failed' | 'auth_error'
  last_sync_error: string | null
  issue_count: number
  created_at: string
}

export interface JiraProject {
  id: string
  key: string
  name: string
  projectTypeKey?: string
  selected?: boolean
}

/**
 * Get Jira OAuth authorization URL
 */
export async function getJiraAuthUrl(workspaceId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/jira/authorize?workspace_id=${encodeURIComponent(workspaceId)}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get auth URL: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.auth_url as string
}

/**
 * List Jira connections for a workspace
 */
export async function listJiraConnections(workspaceId: string): Promise<JiraConnection[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/jira/connections`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list Jira connections: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.connections as JiraConnection[]) || []
}

/**
 * List available Jira projects for a connection
 */
export async function listJiraProjects(
  workspaceId: string,
  connectionId: string
): Promise<{ projects: JiraProject[]; selected_count: number }> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/jira/connections/${connectionId}/projects`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list projects: ${response.status}`)
  }
  const data = await safeJson(response)
  return {
    projects: (data?.projects as JiraProject[]) || [],
    selected_count: (data?.selected_count as number) || 0,
  }
}

/**
 * Update selected Jira projects for a connection
 */
export async function updateJiraProjects(
  workspaceId: string,
  connectionId: string,
  projectKeys: string[],
  includeAttachments: boolean = true,
  includeComments: boolean = true
): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/jira/connections/${connectionId}/projects`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      project_keys: projectKeys,
      include_attachments: includeAttachments,
      include_comments: includeComments,
    }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to update projects: ${response.status}`)
  }
}

/**
 * Trigger Jira sync for a connection
 */
export async function triggerJiraSync(workspaceId: string, connectionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/jira/connections/${connectionId}/sync`, {
    method: 'POST',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to trigger sync: ${response.status}`)
  }
}

/**
 * Delete a Jira connection
 */
export async function deleteJiraConnection(workspaceId: string, connectionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/jira/connections/${connectionId}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to delete connection: ${response.status}`)
  }
}

// ============================================================================
// Outlook Integration Types and API
// ============================================================================

export interface OutlookConnection {
  connection_id: string
  workspace_id: string
  user_email: string
  display_name: string
  selected_folders: OutlookFolder[]
  last_sync_at: string | null
  last_sync_status: 'needs_configuration' | 'queued' | 'syncing' | 'completed' | 'failed' | 'auth_error'
  last_sync_error?: string | null
  email_count: number
  created_at: string
  updated_at: string
}

export interface OutlookFolder {
  id: string
  displayName: string
  totalItemCount?: number
  unreadItemCount?: number
  selected?: boolean
}

/**
 * Get Outlook OAuth authorization URL
 */
export async function getOutlookAuthUrl(workspaceId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/outlook/authorize?workspace_id=${encodeURIComponent(workspaceId)}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get auth URL: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.auth_url as string
}

/**
 * List Outlook connections for a workspace
 */
export async function listOutlookConnections(workspaceId: string): Promise<OutlookConnection[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/outlook/connections`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list Outlook connections: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.connections as OutlookConnection[]) || []
}

/**
 * List available Outlook mail folders for a connection
 */
export async function listOutlookFolders(
  workspaceId: string,
  connectionId: string
): Promise<{ folders: OutlookFolder[]; selected_count: number }> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/outlook/connections/${connectionId}/folders`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list folders: ${response.status}`)
  }
  const data = await safeJson(response)
  return {
    folders: (data?.folders as OutlookFolder[]) || [],
    selected_count: (data?.selected_count as number) || 0,
  }
}

/**
 * Update selected Outlook folders for a connection
 */
export async function updateOutlookFolders(
  workspaceId: string,
  connectionId: string,
  selectedFolders: OutlookFolder[]
): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/outlook/connections/${connectionId}/folders`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      selected_folders: selectedFolders,
    }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to update folders: ${response.status}`)
  }
}

/**
 * Trigger Outlook sync for a connection
 */
export async function triggerOutlookSync(workspaceId: string, connectionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/outlook/connections/${connectionId}/sync`, {
    method: 'POST',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to trigger sync: ${response.status}`)
  }
}

/**
 * Delete an Outlook connection
 */
export async function deleteOutlookConnection(workspaceId: string, connectionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/outlook/connections/${connectionId}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to delete connection: ${response.status}`)
  }
}

// ============================================================================
// Teams Integration Types and API
// ============================================================================

export interface TeamsConnection {
  connection_id: string
  workspace_id: string
  user_email: string
  display_name: string
  config: {
    days_back: number
    include_channels: TeamsChannelConfig[]
    sync_files: boolean
  }
  last_sync_at: string | null
  last_sync_status: 'needs_configuration' | 'queued' | 'syncing' | 'completed' | 'failed' | 'auth_error'
  last_sync_error?: string | null
  message_count: number
  file_count: number
  created_at: string
  updated_at: string
}

export interface TeamsChannelConfig {
  team_id: string
  team_name: string
  channel_id: string
  channel_name: string
}

export interface TeamsTeam {
  id: string
  displayName: string
  description?: string
}

export interface TeamsChannel {
  id: string
  displayName: string
  description?: string
  membershipType?: string
}

export async function getTeamsAuthUrl(workspaceId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/teams/authorize?workspace_id=${encodeURIComponent(workspaceId)}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get auth URL: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.auth_url as string
}

export async function listTeamsConnections(workspaceId: string): Promise<TeamsConnection[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/teams/connections`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list Teams connections: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.connections as TeamsConnection[]) || []
}

export async function listTeamsTeams(
  workspaceId: string,
  connectionId: string
): Promise<TeamsTeam[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/teams/connections/${connectionId}/teams`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list teams: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.teams as TeamsTeam[]) || []
}

export async function listTeamsChannels(
  workspaceId: string,
  connectionId: string,
  teamId: string
): Promise<TeamsChannel[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/teams/connections/${connectionId}/channels?team_id=${encodeURIComponent(teamId)}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list channels: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.channels as TeamsChannel[]) || []
}

export async function saveTeamsConfig(
  workspaceId: string,
  connectionId: string,
  config: { days_back: number; include_channels: TeamsChannelConfig[]; sync_files: boolean }
): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/teams/connections/${connectionId}/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to save config: ${response.status}`)
  }
}

export async function triggerTeamsSync(workspaceId: string, connectionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/teams/connections/${connectionId}/sync`, {
    method: 'POST',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to trigger sync: ${response.status}`)
  }
}

export async function deleteTeamsConnection(workspaceId: string, connectionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/teams/connections/${connectionId}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to delete connection: ${response.status}`)
  }
}

// ============================================================================
// SharePoint Integration Types and API
// ============================================================================

export interface SharePointConnection {
  connection_id: string
  workspace_id: string
  user_email: string
  display_name: string
  selected_sites: SharePointSiteConfig[]
  include_pages: boolean
  last_sync_at: string | null
  last_sync_status: 'needs_configuration' | 'queued' | 'syncing' | 'completed' | 'failed' | 'auth_error'
  last_sync_error?: string | null
  file_count: number
  page_count: number
  created_at: string
  updated_at: string
}

export interface SharePointSiteConfig {
  site_id: string
  site_name: string
  drives: { id: string; name: string }[]
}

export interface SharePointSite {
  id: string
  displayName: string
  name: string
  webUrl: string
  friendly_name: string
  drives: { id: string; name: string }[]
  selected?: boolean
}

export async function getSharePointAuthUrl(workspaceId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/sharepoint/authorize?workspace_id=${encodeURIComponent(workspaceId)}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get auth URL: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.auth_url as string
}

export async function listSharePointConnections(workspaceId: string): Promise<SharePointConnection[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/sharepoint/connections`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list SharePoint connections: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.connections as SharePointConnection[]) || []
}

export async function listSharePointSites(
  workspaceId: string,
  connectionId: string
): Promise<SharePointSite[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/sharepoint/connections/${connectionId}/sites`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list sites: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.sites as SharePointSite[]) || []
}

export async function updateSharePointSites(
  workspaceId: string,
  connectionId: string,
  selectedSites: SharePointSiteConfig[],
  includePages: boolean
): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/sharepoint/connections/${connectionId}/sites`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      selected_sites: selectedSites,
      include_pages: includePages,
    }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to update sites: ${response.status}`)
  }
}

export async function triggerSharePointSync(workspaceId: string, connectionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/sharepoint/connections/${connectionId}/sync`, {
    method: 'POST',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to trigger sync: ${response.status}`)
  }
}

export async function deleteSharePointConnection(workspaceId: string, connectionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/sharepoint/connections/${connectionId}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to delete connection: ${response.status}`)
  }
}

// ============================================================================
// OneDrive Integration Types and API
// ============================================================================

export interface OneDriveConnection {
  connection_id: string
  workspace_id: string
  user_email: string
  display_name: string
  drive_id: string
  drive_name: string
  selected_folders: OneDriveFolderConfig[]
  last_sync_at: string | null
  last_sync_status:
    | 'needs_configuration'
    | 'queued'
    | 'syncing'
    | 'completed'
    | 'failed'
    | 'auth_error'
    | 'no_folders_selected'
    | 'blocked_license_limit'
  last_sync_error?: string | null
  file_count: number
  created_at: string
  updated_at: string
}

export interface OneDriveFolderConfig {
  folder_id: string
  folder_name: string
  folder_path: string
}

export interface OneDriveFolder {
  folder_id: string
  folder_name: string
  folder_path: string
  selected?: boolean
}

export async function getOneDriveAuthUrl(workspaceId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/onedrive/authorize?workspace_id=${encodeURIComponent(workspaceId)}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get auth URL: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.auth_url as string
}

export async function listOneDriveConnections(workspaceId: string): Promise<OneDriveConnection[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/onedrive/connections`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list OneDrive connections: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.connections as OneDriveConnection[]) || []
}

export async function listOneDriveFolders(
  workspaceId: string,
  connectionId: string
): Promise<OneDriveFolder[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/onedrive/connections/${connectionId}/folders`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list folders: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.folders as OneDriveFolder[]) || []
}

export async function updateOneDriveFolders(
  workspaceId: string,
  connectionId: string,
  selectedFolders: OneDriveFolderConfig[]
): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/onedrive/connections/${connectionId}/folders`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      selected_folders: selectedFolders,
    }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to update folders: ${response.status}`)
  }
}

export async function triggerOneDriveSync(workspaceId: string, connectionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/onedrive/connections/${connectionId}/sync`, {
    method: 'POST',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to trigger sync: ${response.status}`)
  }
}

export async function deleteOneDriveConnection(workspaceId: string, connectionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/onedrive/connections/${connectionId}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to delete connection: ${response.status}`)
  }
}

// ============================================================================
// GitHub Integration Types and API
// ============================================================================

export interface GitHubInstallation {
  installation_id: string
  workspace_id: string
  account_login: string
  account_type: 'Organization' | 'User'
  permissions: Record<string, string>
  created_by: string
  created_at: string
}

export type GitHubRepoType = 'application' | 'standards'

export interface GitHubRepo {
  repo_name: string
  workspace_id: string
  installation_id: string
  enabled: boolean
  include_code_maps: boolean
  repo_type: GitHubRepoType
  last_sync_at: string | null
  last_sync_status: 'needs_configuration' | 'queued' | 'in_progress' | 'success' | 'failed' | null
  last_sync_error: string | null
  last_commit_sha: string | null
  added_at: string
  added_by: string
}

export interface GitHubAvailableRepo {
  full_name: string
  name: string
  description: string | null
  private: boolean
  default_branch: string
  updated_at: string
}

/**
 * Get GitHub App installation URL for connecting GitHub to a workspace
 */
export async function getGitHubInstallUrl(workspaceId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/github/authorize?workspace_id=${encodeURIComponent(workspaceId)}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get GitHub install URL: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.install_url as string
}

/**
 * List GitHub App installations for a workspace
 */
export async function listGitHubInstallations(workspaceId: string): Promise<GitHubInstallation[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/github/installations`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list GitHub installations: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.installations as GitHubInstallation[]) || []
}

/**
 * List repos available from a GitHub App installation
 */
export async function listGitHubInstallationRepos(
  workspaceId: string,
  installationId: string
): Promise<GitHubAvailableRepo[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/github/installations/${installationId}/repos`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list installation repos: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.repos as GitHubAvailableRepo[]) || []
}

/**
 * List GitHub repos added to a workspace (for code maps)
 */
export async function listGitHubRepos(workspaceId: string): Promise<GitHubRepo[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/github/repos`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list GitHub repos: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.repos as GitHubRepo[]) || []
}

/**
 * Add GitHub repos to a workspace for code map generation
 */
export async function addGitHubRepos(
  workspaceId: string,
  repos: { repo_name: string; installation_id: string; include_code_maps?: boolean; repo_type?: GitHubRepoType }[]
): Promise<GitHubRepo[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/github/repos`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repos }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to add GitHub repos: ${response.status}`)
  }
  const data = await safeJson(response)
  return (data?.repos as GitHubRepo[]) || []
}

/**
 * Update a GitHub repo's settings
 */
export async function updateGitHubRepo(
  workspaceId: string,
  repoName: string,
  updates: { enabled?: boolean; include_code_maps?: boolean }
): Promise<GitHubRepo> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/github/repos/${encodeURIComponent(repoName)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to update GitHub repo: ${response.status}`)
  }
  const data = await safeJson(response)
  return data?.repo as GitHubRepo
}

/**
 * Remove a GitHub repo from workspace
 */
export async function removeGitHubRepo(workspaceId: string, repoName: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/github/repos/${encodeURIComponent(repoName)}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to remove GitHub repo: ${response.status}`)
  }
}

/**
 * Trigger GitHub sync for workspace repos
 */
export async function triggerGitHubSync(workspaceId: string, repoName?: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/github/sync`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(repoName ? { repo_name: repoName } : {}),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to trigger GitHub sync: ${response.status}`)
  }
}

// ============================================================================
// MCP Server Management
// ============================================================================

export interface McpServer {
  mcp_id: string
  name: string
  url: string
  auth_type: 'none' | 'api_key' | 'bearer'
  has_credentials: boolean
  is_global: boolean
  refresh_interval_days: number
  status: 'active' | 'error'
  tool_count: number
  last_refreshed_at: string | null
  created_by: string
  created_at: string
  updated_at: string
}

export interface CreateMcpServerRequest {
  name: string
  url: string
  auth_type: 'none' | 'api_key' | 'bearer'
  credentials?: string
  is_global: boolean
  refresh_interval_days: number
}

export interface UpdateMcpServerRequest {
  name?: string
  url?: string
  auth_type?: 'none' | 'api_key' | 'bearer'
  credentials?: string
  is_global?: boolean
  refresh_interval_days?: number
}

export interface ChatMcpServer {
  mcp_id: string
  name: string
  status: 'active' | 'error'
  tool_count: number
  is_global: boolean
}

export async function listMcpServers(): Promise<McpServer[]> {
  const response = await fetch(`${API_BASE}/mcp/servers`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list MCP servers: ${response.status}`)
  }
  const data = await response.json()
  return data.mcp_servers || []
}

export async function createMcpServer(request: CreateMcpServerRequest): Promise<McpServer> {
  const response = await fetch(`${API_BASE}/mcp/servers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to create MCP server: ${response.status}`)
  }
  const data = await response.json()
  return data.mcp_server
}

export async function getMcpServer(id: string): Promise<McpServer> {
  const response = await fetch(`${API_BASE}/mcp/servers/${id}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get MCP server: ${response.status}`)
  }
  const data = await response.json()
  return data.mcp_server
}

export async function updateMcpServer(id: string, request: UpdateMcpServerRequest): Promise<McpServer> {
  const response = await fetch(`${API_BASE}/mcp/servers/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to update MCP server: ${response.status}`)
  }
  const data = await response.json()
  return data.mcp_server
}

export async function deleteMcpServer(id: string): Promise<void> {
  const response = await fetch(`${API_BASE}/mcp/servers/${id}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to delete MCP server: ${response.status}`)
  }
}

export async function refreshMcpTools(id: string): Promise<McpServer> {
  const response = await fetch(`${API_BASE}/mcp/servers/${id}/refresh`, {
    method: 'POST',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to refresh MCP tools: ${response.status}`)
  }
  const data = await response.json()
  return data.mcp_server
}

export async function getMcpServerWorkspaces(mcpId: string): Promise<string[]> {
  const response = await fetch(`${API_BASE}/mcp/servers/${mcpId}/workspaces`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get MCP server workspaces: ${response.status}`)
  }
  const data = await response.json()
  return data.workspace_ids || []
}

export async function assignMcpToWorkspace(workspaceId: string, mcpId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/mcp-servers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mcp_id: mcpId }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to assign MCP server: ${response.status}`)
  }
}

export async function removeMcpFromWorkspace(workspaceId: string, mcpId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/mcp-servers/${mcpId}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to remove MCP server: ${response.status}`)
  }
}

export async function listWorkspaceMcpServers(workspaceId: string): Promise<McpServer[]> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/mcp-servers`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list workspace MCP servers: ${response.status}`)
  }
  const data = await response.json()
  return data.mcp_servers || []
}

export async function getChatMcpServers(): Promise<ChatMcpServer[]> {
  const response = await fetch(`${API_BASE}/chat/mcp-servers`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to fetch MCP servers: ${response.status}`)
  }
  const data = await response.json()
  return data.mcp_servers || []
}

// ============================================================================
// User Profile
// ============================================================================

export interface UserProfile {
  email: string
  name?: string
}

export async function getCurrentUser(): Promise<UserProfile> {
  const response = await fetch(`${API_BASE}/me`)
  if (!response.ok) {
    throw new Error(`Failed to get user profile: ${response.status}`)
  }
  const data = await response.json()
  const user = data.user || {}
  return {
    email: user.email || '',
    name: data.claims?.name || '',
  }
}

// ============================================================================
// API Key Management
// ============================================================================

export interface ApiKey {
  key_id: string
  key_prefix: string
  name: string
  created_at: string
  expires_at?: string
  last_used_at?: string
  status: 'active' | 'revoked'
}

export interface CreateApiKeyRequest {
  name: string
  expires_in_days?: number
}

export interface CreateApiKeyResponse {
  key_id: string
  key: string
  key_prefix: string
  name: string
  created_at: string
  expires_at?: string
}

export async function createApiKey(request: CreateApiKeyRequest): Promise<CreateApiKeyResponse> {
  const response = await fetch(`${API_BASE}/api-keys`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to create API key: ${response.status}`)
  }
  return response.json()
}

export async function listApiKeys(): Promise<ApiKey[]> {
  const response = await fetch(`${API_BASE}/api-keys`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list API keys: ${response.status}`)
  }
  const data = await response.json()
  return data.api_keys || []
}

export async function revokeApiKey(keyId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api-keys/${keyId}`, { method: 'DELETE' })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to revoke API key: ${response.status}`)
  }
}

// ============================================================================
// Workspace System Prompt API
// ============================================================================

export interface WorkspaceSystemPrompt {
  custom_system_prompt: string | null
  updated_by: string | null
  updated_at: string | null
}

/**
 * Get the custom system prompt for a workspace
 */
export async function getWorkspaceSystemPrompt(workspaceId: string): Promise<WorkspaceSystemPrompt> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/system-prompt`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to fetch system prompt: ${response.status}`)
  }
  const data = await safeJson(response)
  return data as unknown as WorkspaceSystemPrompt
}

/**
 * Update the custom system prompt for a workspace
 */
export async function updateWorkspaceSystemPrompt(workspaceId: string, prompt: string): Promise<WorkspaceSystemPrompt> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/system-prompt`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ custom_system_prompt: prompt }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to update system prompt: ${response.status}`)
  }
  const data = await safeJson(response)
  return data as unknown as WorkspaceSystemPrompt
}

/**
 * Reset the custom system prompt for a workspace (revert to default)
 */
export async function resetWorkspaceSystemPrompt(workspaceId: string): Promise<WorkspaceSystemPrompt> {
  const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/system-prompt`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to reset system prompt: ${response.status}`)
  }
  const data = await safeJson(response)
  return data as unknown as WorkspaceSystemPrompt
}

// ============================================================================
// Conversation Link Sharing Types and API
// ============================================================================

export interface ShareResponse {
  shared_workspace_ids: string[]
  share_url: string
}

export interface SharedConversationListItem {
  conversation_id: string
  title: string
  message_count: number
  shared_by: string
  updated_at: string
  workspace_id: string
}

export interface SharedConversationListResponse {
  conversations: SharedConversationListItem[]
  next_cursor?: string
  has_more: boolean
}

/**
 * Share/unshare a conversation with workspaces (PUT).
 * Pass non-empty workspaceIds to share, empty array to unshare all.
 */
export async function shareConversation(conversationId: string, workspaceIds: string[]): Promise<ShareResponse> {
  const response = await fetch(`${API_BASE}/conversations/${conversationId}/share`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ workspace_ids: workspaceIds }),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    throw new Error(error.error || 'Failed to share conversation')
  }
  return response.json()
}

/**
 * List shared conversations accessible to the user
 */
export async function listSharedConversations(workspaceId?: string, limit?: number, cursor?: string): Promise<SharedConversationListResponse> {
  const params = new URLSearchParams()
  if (workspaceId) params.set('workspace_id', workspaceId)
  if (limit) params.set('limit', String(limit))
  if (cursor) params.set('cursor', cursor)
  const response = await fetch(`${API_BASE}/shared?${params.toString()}`)
  if (!response.ok) throw new Error('Failed to list shared conversations')
  return response.json()
}

// ============================================================================
// Workspace Budget API
// ============================================================================

export interface WorkspaceBudget {
  workspace_id: string
  monthly_budget_usd: number | null
  updated_at?: string
  updated_by?: string
  is_default: boolean
}

export async function getWorkspaceBudget(workspaceId: string): Promise<WorkspaceBudget> {
  const response = await fetch(`${API_BASE}/workspaces/${encodeURIComponent(workspaceId)}/budget`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get budget: ${response.status}`)
  }
  return (await response.json()) as WorkspaceBudget
}

/**
 * Set or clear the per-workspace monthly budget. Pass 0 (or null) to clear.
 */
export async function setWorkspaceBudget(
  workspaceId: string,
  monthlyBudgetUsd: number | null,
): Promise<WorkspaceBudget> {
  const response = await fetch(`${API_BASE}/workspaces/${encodeURIComponent(workspaceId)}/budget`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ monthly_budget_usd: monthlyBudgetUsd }),
  })
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to set budget: ${response.status}`)
  }
  return (await response.json()) as WorkspaceBudget
}

// ============================================================================
// Workspace Privacy (PII Redaction) API
// ============================================================================

export type PrivacyMode = 'STRICT' | 'NONE'

export interface WorkspacePrivacy {
  workspace_id: string
  privacy_mode: PrivacyMode
  redaction_enabled_at_org_level: boolean
  is_effective: boolean
  updated_at?: string
  updated_by?: string
}

export async function getWorkspacePrivacy(
  workspaceId: string,
): Promise<WorkspacePrivacy> {
  const response = await fetch(
    `${API_BASE}/workspaces/${encodeURIComponent(workspaceId)}/privacy`,
  )
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error(
      (data?.error as string) ||
        `Failed to get privacy mode: ${response.status}`,
    )
  }
  return (await response.json()) as WorkspacePrivacy
}

export async function updateWorkspacePrivacy(
  workspaceId: string,
  privacy_mode: PrivacyMode,
): Promise<WorkspacePrivacy> {
  const response = await fetch(
    `${API_BASE}/workspaces/${encodeURIComponent(workspaceId)}/privacy`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ privacy_mode }),
    },
  )
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error(
      (data?.error as string) ||
        `Failed to update privacy mode: ${response.status}`,
    )
  }
  return (await response.json()) as WorkspacePrivacy
}

export interface WorkspaceBudgetSummary {
  workspace_id: string
  name: string
  monthly_budget_usd: number | null
  is_default: boolean
  mtd_spend_usd: number
  updated_at?: string
  updated_by?: string
}

export interface WorkspaceBudgetsResponse {
  workspaces: WorkspaceBudgetSummary[]
  month: string
}

/**
 * Org-admin-only list of all workspace budgets + current-month spend.
 */
export async function listWorkspaceBudgets(): Promise<WorkspaceBudgetsResponse> {
  const response = await fetch(`${API_BASE}/analytics/budgets`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list budgets: ${response.status}`)
  }
  return (await response.json()) as WorkspaceBudgetsResponse
}

// ============================================================================
// Analytics Types and API
// ============================================================================

export interface AnalyticsModelBreakdown {
  queries: number
  cost_usd: number
}

export interface AnalyticsWorkspaceBreakdown {
  workspace_id: string
  name: string
  queries: number
  cost_usd: number
}

export interface AnalyticsDailyPoint {
  date: string
  queries: number
  cost_usd: number
}

export interface AnalyticsSummary {
  total_cost_usd: number
  total_queries: number
  total_input_tokens: number
  total_output_tokens: number
  by_model: Record<string, AnalyticsModelBreakdown>
  by_workspace: AnalyticsWorkspaceBreakdown[]
  daily_trend: AnalyticsDailyPoint[]
  start_date?: string
  end_date?: string
}

export interface AnalyticsUser {
  email: string
  queries: number
  cost_usd: number
  input_tokens: number
  output_tokens: number
  last_active: string
}

export interface AnalyticsWorkspaceUser {
  email: string
  queries: number
  cost_usd: number
}

export interface AnalyticsWorkspaceDetail {
  workspace_id: string
  workspace_name: string
  total_cost_usd: number
  total_queries: number
  avg_duration_ms: number
  refusal_count: number
  error_count: number
  start_date?: string
  end_date?: string
  by_user: AnalyticsWorkspaceUser[]
  by_model: Record<string, AnalyticsModelBreakdown>
  daily_trend: AnalyticsDailyPoint[]
}

/**
 * Get org-level analytics summary for a date range.
 * Dates are ISO 8601 strings.
 */
export async function getAnalyticsSummary(
  startDate?: string,
  endDate?: string,
  workspaceId?: string,
): Promise<AnalyticsSummary> {
  const params = new URLSearchParams()
  if (startDate) params.set('start_date', startDate)
  if (endDate) params.set('end_date', endDate)
  if (workspaceId) params.set('workspace_id', workspaceId)
  const qs = params.toString()
  const response = await fetch(`${API_BASE}/analytics/summary${qs ? `?${qs}` : ''}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get analytics summary: ${response.status}`)
  }
  return (await response.json()) as AnalyticsSummary
}

/**
 * Get top users by query count (admin only).
 */
export async function getAnalyticsUsers(
  startDate?: string,
  endDate?: string,
  workspaceId?: string,
  limit?: number,
): Promise<{ users: AnalyticsUser[] }> {
  const params = new URLSearchParams()
  if (startDate) params.set('start_date', startDate)
  if (endDate) params.set('end_date', endDate)
  if (workspaceId) params.set('workspace_id', workspaceId)
  if (limit) params.set('limit', String(limit))
  const qs = params.toString()
  const response = await fetch(`${API_BASE}/analytics/users${qs ? `?${qs}` : ''}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get analytics users: ${response.status}`)
  }
  return (await response.json()) as { users: AnalyticsUser[] }
}

/**
 * Get detailed analytics for a workspace.
 */
export async function getAnalyticsWorkspace(
  workspaceId: string,
  startDate?: string,
  endDate?: string,
): Promise<AnalyticsWorkspaceDetail> {
  const params = new URLSearchParams()
  if (startDate) params.set('start_date', startDate)
  if (endDate) params.set('end_date', endDate)
  const qs = params.toString()
  const response = await fetch(
    `${API_BASE}/analytics/workspace/${encodeURIComponent(workspaceId)}${qs ? `?${qs}` : ''}`,
  )
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get workspace analytics: ${response.status}`)
  }
  return (await response.json()) as AnalyticsWorkspaceDetail
}

// ============================================================================
// Audit (chunk 05)
// ============================================================================

export interface AuditActionCount {
  action: string
  count: number
}

export interface AuditWorkspaceCount {
  workspace_id: string
  workspace_name: string
  count: number
}

export interface AuditActor {
  user_email: string
  count: number
  last_active: string
}

export interface AuditTrendPoint {
  date: string
  count: number
}

export interface AuditSummary {
  actions_total: number
  actions_by_type: AuditActionCount[]
  actions_by_workspace: AuditWorkspaceCount[]
  top_actors: AuditActor[]
  trend: AuditTrendPoint[]
  start_date: string
  end_date: string
}

export interface AuditWorkspaceSummary {
  workspace_id: string
  workspace_name: string
  actions_total: number
  actions_by_type: AuditActionCount[]
  top_actors: AuditActor[]
  trend: AuditTrendPoint[]
  start_date: string
  end_date: string
}

export interface AuditEvent {
  request_id: string
  timestamp: string
  category: string
  action: string
  user_email: string
  workspace_id: string
  source_ip: string
  user_agent: string
  details: Record<string, unknown>
  // Bedrock-only fields, present on refusal rows. Empty on admin events.
  status?: string
  flagged_reason?: string
  lambda_name?: string
  model_id?: string
  input_text?: string
  output_text?: string
}

export interface AuditEventsPage {
  events: AuditEvent[]
  next_cursor: string | null
  limit: number
  start_date: string
  end_date: string
}

export interface AuditEventFilters {
  startDate?: string
  endDate?: string
  workspaceId?: string
  userEmail?: string
  action?: string
  category?: string
  /** Filter by row status. Used to fetch refusals (status="refusal"
   * on bedrock-category rows) for the audit dashboard's inline view. */
  status?: string
  cursor?: string
  limit?: number
}

/** Org-wide audit summary (org admin only). */
export async function getAuditSummary(
  startDate?: string,
  endDate?: string,
): Promise<AuditSummary> {
  const params = new URLSearchParams()
  if (startDate) params.set('start_date', startDate)
  if (endDate) params.set('end_date', endDate)
  const qs = params.toString()
  const response = await fetch(`${API_BASE}/audit/summary${qs ? `?${qs}` : ''}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get audit summary: ${response.status}`)
  }
  return (await response.json()) as AuditSummary
}

/** Per-workspace audit summary (org-admin or workspace-admin). */
export async function getAuditWorkspace(
  workspaceId: string,
  startDate?: string,
  endDate?: string,
): Promise<AuditWorkspaceSummary> {
  const params = new URLSearchParams()
  if (startDate) params.set('start_date', startDate)
  if (endDate) params.set('end_date', endDate)
  const qs = params.toString()
  const response = await fetch(
    `${API_BASE}/audit/workspace/${encodeURIComponent(workspaceId)}${qs ? `?${qs}` : ''}`,
  )
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error(
      (data?.error as string) || `Failed to get workspace audit summary: ${response.status}`,
    )
  }
  return (await response.json()) as AuditWorkspaceSummary
}

/** Paginated raw audit events. */
export async function getAuditEvents(
  filters: AuditEventFilters = {},
): Promise<AuditEventsPage> {
  const params = new URLSearchParams()
  if (filters.startDate) params.set('start_date', filters.startDate)
  if (filters.endDate) params.set('end_date', filters.endDate)
  if (filters.workspaceId) params.set('workspace_id', filters.workspaceId)
  if (filters.userEmail) params.set('user_email', filters.userEmail)
  if (filters.action) params.set('action', filters.action)
  if (filters.category) params.set('category', filters.category)
  if (filters.status) params.set('status', filters.status)
  if (filters.cursor) params.set('cursor', filters.cursor)
  if (filters.limit) params.set('limit', String(filters.limit))
  const qs = params.toString()
  const response = await fetch(`${API_BASE}/audit/events${qs ? `?${qs}` : ''}`)
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to list audit events: ${response.status}`)
  }
  return (await response.json()) as AuditEventsPage
}

/** Single audit event detail (drawer). */
export async function getAuditEvent(
  workspaceId: string,
  requestId: string,
): Promise<{ event: AuditEvent }> {
  const response = await fetch(
    `${API_BASE}/audit/event/${encodeURIComponent(workspaceId)}/${encodeURIComponent(requestId)}`,
  )
  if (!response.ok) {
    const data = await safeJson(response)
    throw new Error((data?.error as string) || `Failed to get audit event: ${response.status}`)
  }
  return (await response.json()) as { event: AuditEvent }
}
