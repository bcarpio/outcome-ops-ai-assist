import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  getConfluenceAuthUrl,
  listConfluenceConnections,
  listConfluenceSpaces,
  updateConfluenceSpaces,
  triggerConfluenceSync,
  deleteConfluenceConnection,
  getJiraAuthUrl,
  listJiraConnections,
  listJiraProjects,
  updateJiraProjects,
  triggerJiraSync,
  deleteJiraConnection,
  getOutlookAuthUrl,
  listOutlookConnections,
  listOutlookFolders,
  updateOutlookFolders,
  triggerOutlookSync,
  deleteOutlookConnection,
  getGitHubInstallUrl,
  listGitHubInstallations,
  listGitHubInstallationRepos,
  listGitHubRepos,
  addGitHubRepos,
  updateGitHubRepo,
  removeGitHubRepo,
  triggerGitHubSync,
} from '../../../src/lib/api'
import {
  createConfluenceConnection,
  createJiraConnection,
  createOutlookConnection,
  createGitHubInstallation,
  createGitHubRepo,
  createGitHubAvailableRepo,
  createMockFetchResponse,
} from '../../helpers/mocks'

beforeEach(() => {
  vi.restoreAllMocks()
})

// ============================================================================
// Confluence
// ============================================================================

describe('getConfluenceAuthUrl', () => {
  it('returns auth URL', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ auth_url: 'https://auth.atlassian.com/xyz' }))
    const result = await getConfluenceAuthUrl('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/confluence/authorize?workspace_id=ws-1')
    expect(result).toBe('https://auth.atlassian.com/xyz')
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Forbidden' }, false, 403))
    await expect(getConfluenceAuthUrl('ws-1')).rejects.toThrow('Forbidden')
  })
})

describe('listConfluenceConnections', () => {
  it('returns connections', async () => {
    const conn = createConfluenceConnection()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ connections: [conn] }))
    const result = await listConfluenceConnections('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/confluence/connections')
    expect(result).toHaveLength(1)
    expect(result[0].connection_id).toBe('conn-1')
  })

  it('throws on error', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ error: 'Not found' }, false, 404))
    await expect(listConfluenceConnections('ws-1')).rejects.toThrow('Not found')
  })
})

describe('listConfluenceSpaces', () => {
  it('returns spaces with count', async () => {
    const space = { id: 's1', key: 'SP', name: 'Space 1', type: 'global' }
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ spaces: [space], selected_count: 1 }))
    const result = await listConfluenceSpaces('ws-1', 'conn-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/confluence/connections/conn-1/spaces')
    expect(result.spaces).toHaveLength(1)
    expect(result.selected_count).toBe(1)
  })
})

describe('updateConfluenceSpaces', () => {
  it('sends PUT with spaces and attachments flag', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    const spaces = [{ id: 's1', key: 'SP', name: 'Space 1', type: 'global' }]
    await updateConfluenceSpaces('ws-1', 'conn-1', spaces, true)
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/confluence/connections/conn-1/spaces',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ selected_spaces: spaces, include_attachments: true }),
      })
    )
  })
})

describe('triggerConfluenceSync', () => {
  it('triggers sync', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await triggerConfluenceSync('ws-1', 'conn-1')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/confluence/connections/conn-1/sync',
      expect.objectContaining({ method: 'POST' })
    )
  })
})

describe('deleteConfluenceConnection', () => {
  it('deletes connection', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await deleteConfluenceConnection('ws-1', 'conn-1')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/confluence/connections/conn-1',
      expect.objectContaining({ method: 'DELETE' })
    )
  })
})

// ============================================================================
// Jira
// ============================================================================

describe('getJiraAuthUrl', () => {
  it('returns auth URL', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ auth_url: 'https://auth.atlassian.com/jira' }))
    const result = await getJiraAuthUrl('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/jira/authorize?workspace_id=ws-1')
    expect(result).toBe('https://auth.atlassian.com/jira')
  })
})

describe('listJiraConnections', () => {
  it('returns connections', async () => {
    const conn = createJiraConnection()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ connections: [conn] }))
    const result = await listJiraConnections('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/jira/connections')
    expect(result).toHaveLength(1)
  })
})

describe('listJiraProjects', () => {
  it('returns projects with count', async () => {
    const project = { id: 'p1', key: 'PROJ', name: 'Project 1' }
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ projects: [project], selected_count: 0 }))
    const result = await listJiraProjects('ws-1', 'jira-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/jira/connections/jira-1/projects')
    expect(result.projects).toHaveLength(1)
    expect(result.selected_count).toBe(0)
  })
})

describe('updateJiraProjects', () => {
  it('sends PUT with project keys and options', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await updateJiraProjects('ws-1', 'jira-1', ['PROJ'], true, false)
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/jira/connections/jira-1/projects',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ project_keys: ['PROJ'], include_attachments: true, include_comments: false }),
      })
    )
  })
})

describe('triggerJiraSync', () => {
  it('triggers sync', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await triggerJiraSync('ws-1', 'jira-1')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/jira/connections/jira-1/sync',
      expect.objectContaining({ method: 'POST' })
    )
  })
})

describe('deleteJiraConnection', () => {
  it('deletes connection', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await deleteJiraConnection('ws-1', 'jira-1')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/jira/connections/jira-1',
      expect.objectContaining({ method: 'DELETE' })
    )
  })
})

// ============================================================================
// Outlook
// ============================================================================

describe('getOutlookAuthUrl', () => {
  it('returns auth URL', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ auth_url: 'https://login.microsoft.com/xyz' }))
    const result = await getOutlookAuthUrl('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/outlook/authorize?workspace_id=ws-1')
    expect(result).toBe('https://login.microsoft.com/xyz')
  })
})

describe('listOutlookConnections', () => {
  it('returns connections', async () => {
    const conn = createOutlookConnection()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ connections: [conn] }))
    const result = await listOutlookConnections('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/outlook/connections')
    expect(result).toHaveLength(1)
  })
})

describe('listOutlookFolders', () => {
  it('returns folders with count', async () => {
    const folder = { id: 'f1', displayName: 'Inbox' }
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ folders: [folder], selected_count: 0 }))
    const result = await listOutlookFolders('ws-1', 'outlook-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/outlook/connections/outlook-1/folders')
    expect(result.folders).toHaveLength(1)
    expect(result.selected_count).toBe(0)
  })
})

describe('updateOutlookFolders', () => {
  it('sends PUT with folders', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    const folders = [{ id: 'f1', displayName: 'Inbox' }]
    await updateOutlookFolders('ws-1', 'outlook-1', folders)
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/outlook/connections/outlook-1/folders',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ selected_folders: folders }),
      })
    )
  })
})

describe('triggerOutlookSync', () => {
  it('triggers sync', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await triggerOutlookSync('ws-1', 'outlook-1')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/outlook/connections/outlook-1/sync',
      expect.objectContaining({ method: 'POST' })
    )
  })
})

describe('deleteOutlookConnection', () => {
  it('deletes connection', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await deleteOutlookConnection('ws-1', 'outlook-1')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/outlook/connections/outlook-1',
      expect.objectContaining({ method: 'DELETE' })
    )
  })
})

// ============================================================================
// GitHub
// ============================================================================

describe('getGitHubInstallUrl', () => {
  it('returns install URL', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ install_url: 'https://github.com/apps/test/install' }))
    const result = await getGitHubInstallUrl('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/github/authorize?workspace_id=ws-1')
    expect(result).toBe('https://github.com/apps/test/install')
  })
})

describe('listGitHubInstallations', () => {
  it('returns installations', async () => {
    const inst = createGitHubInstallation()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ installations: [inst] }))
    const result = await listGitHubInstallations('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/github/installations')
    expect(result).toHaveLength(1)
    expect(result[0].account_login).toBe('test-org')
  })
})

describe('listGitHubInstallationRepos', () => {
  it('returns available repos for installation', async () => {
    const repo = createGitHubAvailableRepo()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ repos: [repo] }))
    const result = await listGitHubInstallationRepos('ws-1', 'install-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/github/installations/install-1/repos')
    expect(result).toHaveLength(1)
    expect(result[0].full_name).toBe('test-org/available-repo')
  })
})

describe('listGitHubRepos', () => {
  it('returns workspace repos', async () => {
    const repo = createGitHubRepo()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ repos: [repo] }))
    const result = await listGitHubRepos('ws-1')
    expect(fetch).toHaveBeenCalledWith('/api/workspaces/ws-1/github/repos')
    expect(result).toHaveLength(1)
  })
})

describe('addGitHubRepos', () => {
  it('adds repos', async () => {
    const repo = createGitHubRepo()
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ repos: [repo] }))
    const result = await addGitHubRepos('ws-1', [
      { repo_name: 'test-org/test-repo', installation_id: 'install-1' },
    ])
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/github/repos',
      expect.objectContaining({ method: 'POST' })
    )
    expect(result).toHaveLength(1)
  })
})

describe('updateGitHubRepo', () => {
  it('updates repo settings', async () => {
    const repo = createGitHubRepo({ enabled: false })
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse({ repo }))
    const result = await updateGitHubRepo('ws-1', 'test-org/test-repo', { enabled: false })
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/github/repos/test-org%2Ftest-repo',
      expect.objectContaining({ method: 'PUT' })
    )
    expect(result.enabled).toBe(false)
  })
})

describe('removeGitHubRepo', () => {
  it('removes a repo', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await removeGitHubRepo('ws-1', 'test-org/test-repo')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/github/repos/test-org%2Ftest-repo',
      expect.objectContaining({ method: 'DELETE' })
    )
  })
})

describe('triggerGitHubSync', () => {
  it('triggers sync for all repos', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await triggerGitHubSync('ws-1')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/github/sync',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({}),
      })
    )
  })

  it('triggers sync for specific repo', async () => {
    global.fetch = vi.fn().mockResolvedValue(createMockFetchResponse(null))
    await triggerGitHubSync('ws-1', 'test-org/test-repo')
    expect(fetch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/github/sync',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ repo_name: 'test-org/test-repo' }),
      })
    )
  })
})
