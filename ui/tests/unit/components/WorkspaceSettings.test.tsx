import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import WorkspaceSettings from '../../../src/pages/WorkspaceSettings'
import { renderWithRouter } from '../../helpers/render'
import { createWorkspace } from '../../helpers/mocks'

vi.mock('../../../src/lib/api', () => ({
  getWorkspace: vi.fn(),
  updateWorkspace: vi.fn(),
  deleteWorkspace: vi.fn(),
  listMembers: vi.fn(),
  addMember: vi.fn(),
  updateMember: vi.fn(),
  removeMember: vi.fn(),
  listConfluenceConnections: vi.fn(),
  getConfluenceAuthUrl: vi.fn(),
  listConfluenceSpaces: vi.fn(),
  updateConfluenceSpaces: vi.fn(),
  triggerConfluenceSync: vi.fn(),
  deleteConfluenceConnection: vi.fn(),
  listJiraConnections: vi.fn(),
  getJiraAuthUrl: vi.fn(),
  listJiraProjects: vi.fn(),
  updateJiraProjects: vi.fn(),
  triggerJiraSync: vi.fn(),
  deleteJiraConnection: vi.fn(),
  getSearchAccess: vi.fn(),
  updateSearchAccess: vi.fn(),
  listSharingOptions: vi.fn(),
  listDocuments: vi.fn(),
  getDocumentUploadUrl: vi.fn(),
  uploadDocumentToS3: vi.fn(),
  deleteDocument: vi.fn(),
  listOutlookConnections: vi.fn(),
  getOutlookAuthUrl: vi.fn(),
  listOutlookFolders: vi.fn(),
  updateOutlookFolders: vi.fn(),
  triggerOutlookSync: vi.fn(),
  deleteOutlookConnection: vi.fn(),
  getGitHubInstallUrl: vi.fn(),
  listGitHubInstallations: vi.fn(),
  listGitHubInstallationRepos: vi.fn(),
  listGitHubRepos: vi.fn(),
  addGitHubRepos: vi.fn(),
  updateGitHubRepo: vi.fn(),
  removeGitHubRepo: vi.fn(),
  triggerGitHubSync: vi.fn(),
  listTeamsConnections: vi.fn(),
  getTeamsAuthUrl: vi.fn(),
  listTeamsTeams: vi.fn(),
  listTeamsChannels: vi.fn(),
  saveTeamsConfig: vi.fn(),
  triggerTeamsSync: vi.fn(),
  deleteTeamsConnection: vi.fn(),
  listSharePointConnections: vi.fn(),
  getSharePointAuthUrl: vi.fn(),
  listSharePointSites: vi.fn(),
  updateSharePointSites: vi.fn(),
  triggerSharePointSync: vi.fn(),
  deleteSharePointConnection: vi.fn(),
  searchUsers: vi.fn(),
}))

import { getWorkspace, listMembers, listConfluenceConnections, listJiraConnections, getSearchAccess, listSharingOptions, listDocuments, listOutlookConnections, listGitHubInstallations, listGitHubRepos, listTeamsConnections, listSharePointConnections, getTeamsAuthUrl, deleteTeamsConnection, triggerTeamsSync, getSharePointAuthUrl, deleteSharePointConnection, triggerSharePointSync, listTeamsTeams, listTeamsChannels, saveTeamsConfig, listSharePointSites, updateSharePointSites } from '../../../src/lib/api'

const mockGetWorkspace = vi.mocked(getWorkspace)
const mockListMembers = vi.mocked(listMembers)
const mockListConfluenceConnections = vi.mocked(listConfluenceConnections)
const mockListJiraConnections = vi.mocked(listJiraConnections)
const mockGetSearchAccess = vi.mocked(getSearchAccess)
const mockListSharingOptions = vi.mocked(listSharingOptions)
const mockListDocuments = vi.mocked(listDocuments)
const mockListOutlookConnections = vi.mocked(listOutlookConnections)
const mockListGitHubInstallations = vi.mocked(listGitHubInstallations)
const mockListGitHubRepos = vi.mocked(listGitHubRepos)
const mockListTeamsConnections = vi.mocked(listTeamsConnections)
const mockListSharePointConnections = vi.mocked(listSharePointConnections)
const mockGetTeamsAuthUrl = vi.mocked(getTeamsAuthUrl)
const mockDeleteTeamsConnection = vi.mocked(deleteTeamsConnection)
const mockTriggerTeamsSync = vi.mocked(triggerTeamsSync)
const mockGetSharePointAuthUrl = vi.mocked(getSharePointAuthUrl)
const mockDeleteSharePointConnection = vi.mocked(deleteSharePointConnection)
const mockTriggerSharePointSync = vi.mocked(triggerSharePointSync)
const mockListTeamsTeams = vi.mocked(listTeamsTeams)
const mockListTeamsChannels = vi.mocked(listTeamsChannels)
const mockSaveTeamsConfig = vi.mocked(saveTeamsConfig)
const mockListSharePointSites = vi.mocked(listSharePointSites)
const mockUpdateSharePointSites = vi.mocked(updateSharePointSites)

function setupDefaultMocks() {
  const ws = createWorkspace({ workspace_id: 'ws-1', name: 'Test WS', role: 'workspace_admin' })
  mockGetWorkspace.mockResolvedValue(ws)
  mockListMembers.mockResolvedValue([])
  mockListConfluenceConnections.mockResolvedValue([])
  mockListJiraConnections.mockResolvedValue([])
  mockGetSearchAccess.mockResolvedValue([])
  mockListSharingOptions.mockResolvedValue([])
  mockListDocuments.mockResolvedValue([])
  mockListOutlookConnections.mockResolvedValue([])
  mockListGitHubInstallations.mockResolvedValue([])
  mockListGitHubRepos.mockResolvedValue([])
  mockListTeamsConnections.mockResolvedValue([])
  mockListSharePointConnections.mockResolvedValue([])
}

beforeEach(() => {
  vi.clearAllMocks()
  setupDefaultMocks()
})

function renderSettings(tab?: string) {
  const path = tab
    ? `/workspaces/ws-1?tab=${tab}`
    : '/workspaces/ws-1'
  return renderWithRouter(<WorkspaceSettings />, {
    initialEntries: [path],
    path: '/workspaces/:workspaceId',
  })
}

describe('WorkspaceSettings', () => {
  it('renders workspace name in header', async () => {
    renderSettings()

    await waitFor(() => {
      expect(screen.getByDisplayValue('Test WS')).toBeInTheDocument()
    })
  })

  it('shows General tab by default', async () => {
    renderSettings()

    await waitFor(() => {
      expect(screen.getByDisplayValue('Test WS')).toBeInTheDocument()
    })
  })

  it('renders all tab buttons', async () => {
    renderSettings()

    await waitFor(() => {
      expect(screen.getByText('General')).toBeInTheDocument()
    })
    expect(screen.getByText('Members')).toBeInTheDocument()
    expect(screen.getByText('Integrations')).toBeInTheDocument()
    expect(screen.getByText('Sharing')).toBeInTheDocument()
    expect(screen.getByText('Documents')).toBeInTheDocument()
  })

  it('switches to Members tab', async () => {
    renderSettings()

    await waitFor(() => {
      expect(screen.getByText('Members')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Members'))

    await waitFor(() => {
      expect(mockListMembers).toHaveBeenCalledWith('ws-1')
    })
  })

  it('switches to Integrations tab', async () => {
    renderSettings()

    await waitFor(() => {
      expect(screen.getByText('Integrations')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Integrations'))

    await waitFor(() => {
      expect(screen.getByText('GitHub')).toBeInTheDocument()
    })
  })

  it('switches to Sharing tab', async () => {
    renderSettings()

    await waitFor(() => {
      expect(screen.getByText('Sharing')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Sharing'))

    await waitFor(() => {
      expect(mockGetSearchAccess).toHaveBeenCalled()
    })
  })

  it('switches to Documents tab', async () => {
    renderSettings()

    await waitFor(() => {
      expect(screen.getByText('Documents')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Documents'))

    await waitFor(() => {
      expect(mockListDocuments).toHaveBeenCalled()
    })
  })

  it('shows error when workspace fails to load', async () => {
    mockGetWorkspace.mockRejectedValue(new Error('Not found'))

    renderSettings()

    await waitFor(() => {
      expect(screen.getByText('Workspace not found')).toBeInTheDocument()
    })
  })

  it('opens with specified tab from URL', async () => {
    renderSettings('members')

    await waitFor(() => {
      expect(mockListMembers).toHaveBeenCalledWith('ws-1')
    })
  })

  it('shows Teams and SharePoint cards on Integrations tab', async () => {
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Microsoft Teams')).toBeInTheDocument()
    })
    expect(screen.getByText('SharePoint')).toBeInTheDocument()
  })

  it('shows Teams connection with status', async () => {
    mockListTeamsConnections.mockResolvedValue([{
      connection_id: 'tc-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'Test User',
      config: { days_back: 30, include_channels: [], sync_files: true },
      last_sync_at: '2026-03-01',
      last_sync_status: 'completed',
      message_count: 10,
      file_count: 5,
      created_at: '2026-01-01',
    }])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Test User')).toBeInTheDocument()
    })
  })

  it('shows SharePoint connection with status', async () => {
    mockListSharePointConnections.mockResolvedValue([{
      connection_id: 'sp-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'SP User',
      selected_sites: [],
      include_pages: true,
      last_sync_at: '2026-03-01',
      last_sync_status: 'completed',
      file_count: 20,
      page_count: 3,
      created_at: '2026-01-01',
      updated_at: '2026-01-01',
    }])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('SP User')).toBeInTheDocument()
    })
  })

  it('calls getTeamsAuthUrl on connect click', async () => {
    mockGetTeamsAuthUrl.mockResolvedValue('https://login.microsoft.com/teams')
    const windowOpen = vi.spyOn(window, 'open').mockImplementation(() => null)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Microsoft Teams')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    // Connect buttons: Confluence(0), Jira(1), Outlook(2), Teams(3), SharePoint(4)
    const connectButtons = screen.getAllByText('Connect')
    await user.click(connectButtons[3])

    await waitFor(() => {
      expect(mockGetTeamsAuthUrl).toHaveBeenCalledWith('ws-1')
    })
    windowOpen.mockRestore()
  })

  it('calls getSharePointAuthUrl on connect click', async () => {
    mockGetSharePointAuthUrl.mockResolvedValue('https://login.microsoft.com/sharepoint')
    const windowOpen = vi.spyOn(window, 'open').mockImplementation(() => null)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('SharePoint')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    // Connect buttons: Confluence(0), Jira(1), Outlook(2), Teams(3), SharePoint(4)
    const connectButtons = screen.getAllByText('Connect')
    await user.click(connectButtons[4])

    await waitFor(() => {
      expect(mockGetSharePointAuthUrl).toHaveBeenCalledWith('ws-1')
    })
    windowOpen.mockRestore()
  })

  it('triggers Teams sync', async () => {
    mockListTeamsConnections.mockResolvedValue([{
      connection_id: 'tc-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'Test User',
      config: { days_back: 30, include_channels: [{ team_id: 't1', team_name: 'Team', channel_id: 'c1', channel_name: 'General' }], sync_files: true },
      last_sync_at: null,
      last_sync_status: 'completed',
      message_count: 0,
      file_count: 0,
      created_at: '2026-01-01',
    }])
    mockTriggerTeamsSync.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Test User')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const syncButtons = screen.getAllByTitle('Sync now')
    await user.click(syncButtons[syncButtons.length - 1])

    await waitFor(() => {
      expect(mockTriggerTeamsSync).toHaveBeenCalledWith('ws-1', 'tc-1')
    })
  })

  it('triggers SharePoint sync', async () => {
    mockListSharePointConnections.mockResolvedValue([{
      connection_id: 'sp-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'SP User',
      selected_sites: [{ site_id: 's1', site_name: 'Site 1', drives: [] }],
      include_pages: true,
      last_sync_at: null,
      last_sync_status: 'completed',
      file_count: 0,
      page_count: 0,
      created_at: '2026-01-01',
      updated_at: '2026-01-01',
    }])
    mockTriggerSharePointSync.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('SP User')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const syncButtons = screen.getAllByTitle('Sync now')
    await user.click(syncButtons[syncButtons.length - 1])

    await waitFor(() => {
      expect(mockTriggerSharePointSync).toHaveBeenCalledWith('ws-1', 'sp-1')
    })
  })

  it('opens Teams configuration modal', async () => {
    mockListTeamsConnections.mockResolvedValue([{
      connection_id: 'tc-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'Test User',
      config: { days_back: 30, include_channels: [], sync_files: true },
      last_sync_at: null,
      last_sync_status: 'needs_configuration',
      message_count: 0,
      file_count: 0,
      created_at: '2026-01-01',
    }])
    mockListTeamsTeams.mockResolvedValue([
      { id: 't1', displayName: 'Engineering', description: 'Eng team' }
    ])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Test User')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    // Configure buttons appear for each connected integration
    const configButtons = screen.getAllByText('Configure')
    // Click the last Configure button (Teams)
    await user.click(configButtons[configButtons.length - 1])

    await waitFor(() => {
      expect(mockListTeamsTeams).toHaveBeenCalledWith('ws-1', 'tc-1')
    })
  })

  it('opens SharePoint configuration modal', async () => {
    mockListSharePointConnections.mockResolvedValue([{
      connection_id: 'sp-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'SP User',
      selected_sites: [],
      include_pages: true,
      last_sync_at: null,
      last_sync_status: 'needs_configuration',
      file_count: 0,
      page_count: 0,
      created_at: '2026-01-01',
      updated_at: '2026-01-01',
    }])
    mockListSharePointSites.mockResolvedValue([
      { id: 's1', displayName: 'Engineering Site', webUrl: 'https://test.sharepoint.com/sites/eng', friendly_name: 'Engineering Site', drives: [] }
    ])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('SP User')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const configButtons = screen.getAllByText('Configure')
    await user.click(configButtons[configButtons.length - 1])

    await waitFor(() => {
      expect(mockListSharePointSites).toHaveBeenCalledWith('ws-1', 'sp-1')
    })
  })

  it('deletes Teams connection', async () => {
    mockListTeamsConnections.mockResolvedValue([{
      connection_id: 'tc-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'Test User',
      config: { days_back: 30, include_channels: [], sync_files: true },
      last_sync_at: null,
      last_sync_status: 'completed',
      message_count: 0,
      file_count: 0,
      created_at: '2026-01-01',
    }])
    mockDeleteTeamsConnection.mockResolvedValue(undefined)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Test User')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const disconnectButtons = screen.getAllByTitle('Disconnect')
    await user.click(disconnectButtons[disconnectButtons.length - 1])

    await waitFor(() => {
      expect(mockDeleteTeamsConnection).toHaveBeenCalledWith('ws-1', 'tc-1')
    })
  })

  it('deletes SharePoint connection', async () => {
    mockListSharePointConnections.mockResolvedValue([{
      connection_id: 'sp-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'SP User',
      selected_sites: [],
      include_pages: true,
      last_sync_at: null,
      last_sync_status: 'completed',
      file_count: 0,
      page_count: 0,
      created_at: '2026-01-01',
      updated_at: '2026-01-01',
    }])
    mockDeleteSharePointConnection.mockResolvedValue(undefined)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('SP User')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const disconnectButtons = screen.getAllByTitle('Disconnect')
    await user.click(disconnectButtons[disconnectButtons.length - 1])

    await waitFor(() => {
      expect(mockDeleteSharePointConnection).toHaveBeenCalledWith('ws-1', 'sp-1')
    })
  })
})
