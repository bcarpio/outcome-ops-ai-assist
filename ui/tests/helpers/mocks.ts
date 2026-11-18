// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import type {
  Workspace,
  Conversation,
  Message,
  OrgMember,
  WorkspaceMember,
  SearchableWorkspace,
  WorkspaceSearchAccess,
  WorkspaceDocument,
  ConfluenceConnection,
  JiraConnection,
  OutlookConnection,
  GitHubInstallation,
  GitHubRepo,
  GitHubAvailableRepo,
  DocumentUploadUrlResponse,
  McpServer,
} from '../../src/lib/api'

export function createWorkspace(overrides?: Partial<Workspace>): Workspace {
  return {
    workspace_id: 'ws-1',
    name: 'Test Workspace',
    description: 'A test workspace',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    created_by: 'user@example.com',
    role: 'workspace_admin',
    ...overrides,
  }
}

export function createConversation(overrides?: Partial<Conversation>): Conversation {
  return {
    conversation_id: 'conv-1',
    title: 'Test Conversation',
    message_count: 3,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

export function createMessage(overrides?: Partial<Message>): Message {
  return {
    message_id: 'msg-1',
    role: 'user',
    content: 'Hello',
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

export function createOrgMember(overrides?: Partial<OrgMember>): OrgMember {
  return {
    user_email: 'user@example.com',
    org_role: 'member',
    joined_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

export function createWorkspaceMember(overrides?: Partial<WorkspaceMember>): WorkspaceMember {
  return {
    user_email: 'user@example.com',
    role: 'member',
    joined_at: '2024-01-01T00:00:00Z',
    workspace_id: 'ws-1',
    ...overrides,
  }
}

export function createSearchableWorkspace(overrides?: Partial<SearchableWorkspace>): SearchableWorkspace {
  return {
    workspace_id: 'ws-1',
    name: 'Test Workspace',
    access_type: 'member',
    ...overrides,
  }
}

export function createWorkspaceSearchAccess(overrides?: Partial<WorkspaceSearchAccess>): WorkspaceSearchAccess {
  return {
    workspace_id: 'ws-1',
    name: 'Test Workspace',
    ...overrides,
  }
}

export function createWorkspaceDocument(overrides?: Partial<WorkspaceDocument>): WorkspaceDocument {
  return {
    path: 'docs/readme.md',
    s3_key: 'workspaces/ws-1/docs/readme.md',
    filename: 'readme.md',
    size: 1024,
    last_modified: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

export function createConfluenceConnection(overrides?: Partial<ConfluenceConnection>): ConfluenceConnection {
  return {
    connection_id: 'conn-1',
    workspace_id: 'ws-1',
    cloud_id: 'cloud-1',
    site_name: 'test-site',
    site_url: 'https://test.atlassian.net',
    selected_spaces: [],
    include_attachments: false,
    last_sync_at: null,
    last_sync_status: 'needs_configuration',
    page_count: 0,
    attachment_count: 0,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

export function createJiraConnection(overrides?: Partial<JiraConnection>): JiraConnection {
  return {
    connection_id: 'jira-1',
    workspace_id: 'ws-1',
    cloud_id: 'cloud-1',
    site_name: 'test-site',
    site_url: 'https://test.atlassian.net',
    selected_projects: [],
    include_attachments: true,
    include_comments: true,
    last_sync_at: null,
    last_sync_status: 'needs_configuration',
    last_sync_error: null,
    issue_count: 0,
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

export function createOutlookConnection(overrides?: Partial<OutlookConnection>): OutlookConnection {
  return {
    connection_id: 'outlook-1',
    workspace_id: 'ws-1',
    user_email: 'user@example.com',
    display_name: 'Test User',
    selected_folders: [],
    last_sync_at: null,
    last_sync_status: 'needs_configuration',
    email_count: 0,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

export function createGitHubInstallation(overrides?: Partial<GitHubInstallation>): GitHubInstallation {
  return {
    installation_id: 'install-1',
    workspace_id: 'ws-1',
    account_login: 'test-org',
    account_type: 'Organization',
    permissions: { contents: 'read' },
    created_by: 'user@example.com',
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

export function createGitHubRepo(overrides?: Partial<GitHubRepo>): GitHubRepo {
  return {
    repo_name: 'test-org/test-repo',
    workspace_id: 'ws-1',
    installation_id: 'install-1',
    enabled: true,
    include_code_maps: true,
    repo_type: 'application',
    last_sync_at: null,
    last_sync_status: null,
    last_sync_error: null,
    last_commit_sha: null,
    added_at: '2024-01-01T00:00:00Z',
    added_by: 'user@example.com',
    ...overrides,
  }
}

export function createGitHubAvailableRepo(overrides?: Partial<GitHubAvailableRepo>): GitHubAvailableRepo {
  return {
    full_name: 'test-org/available-repo',
    name: 'available-repo',
    description: 'An available repo',
    private: false,
    default_branch: 'main',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

export function createDocumentUploadUrlResponse(overrides?: Partial<DocumentUploadUrlResponse>): DocumentUploadUrlResponse {
  return {
    upload_url: 'https://s3.amazonaws.com/bucket/key?presigned',
    s3_key: 'workspaces/ws-1/docs/file.pdf',
    bucket: 'test-bucket',
    expires_in: 3600,
    ...overrides,
  }
}

export function createMcpServer(overrides?: Partial<McpServer>): McpServer {
  return {
    mcp_id: 'mcp-1',
    name: 'Test MCP Server',
    url: 'https://mcp.example.com/mcp',
    auth_type: 'none',
    has_credentials: false,
    is_global: true,
    refresh_interval_days: 7,
    status: 'active',
    tool_count: 5,
    last_refreshed_at: '2024-01-01T00:00:00Z',
    created_by: 'admin@example.com',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

export function createMockFetchResponse(body: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  } as Response
}

export function createMockStreamResponse(events: object[]) {
  const ndjson = events.map(e => JSON.stringify(e)).join('\n')
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(ndjson))
      controller.close()
    },
  })
  return new Response(stream, { status: 200 })
}
