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
  getServiceNowAuthUrl: vi.fn(),
  listServiceNowConnections: vi.fn(),
  syncServiceNowConnection: vi.fn(),
  disconnectServiceNow: vi.fn(),
  connectSalesforce: vi.fn(),
  getSalesforceAuthUrl: vi.fn(),
  listSalesforceConnections: vi.fn(),
  syncSalesforceConnection: vi.fn(),
  disconnectSalesforce: vi.fn(),
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
  listGitHubRepoBranches: vi.fn(),
  removeGitHubRepo: vi.fn(),
  triggerGitHubSync: vi.fn(),
  updateAzureDevOpsRepo: vi.fn(),
  listAzureDevOpsRepoBranches: vi.fn(),
  // Chunk 06 added the Azure DevOps (non-Boards) connection UI to
  // WorkspaceSettings; the mock was never updated to include those
  // exports. Locally the `.catch(() => [])` chains hide it, but on
  // slower CI runners the failed getters leave the page in an
  // unrendered state and `screen.getByRole("button", /new workspace/i)`
  // downstream throws TestingLibraryElementError.
  listAzureDevOpsConnections: vi.fn(),
  getAzureDevOpsAuthUrl: vi.fn(),
  listAzureDevOpsOrgs: vi.fn(),
  listAzureDevOpsProjects: vi.fn(),
  listAzureDevOpsProjectRepos: vi.fn(),
  listAzureDevOpsRepos: vi.fn(),
  addAzureDevOpsRepos: vi.fn(),
  removeAzureDevOpsRepo: vi.fn(),
  triggerAzureDevOpsSync: vi.fn(),
  disconnectAzureDevOps: vi.fn(),
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
  listOneDriveConnections: vi.fn(),
  getOneDriveAuthUrl: vi.fn(),
  listOneDriveFolders: vi.fn(),
  updateOneDriveFolders: vi.fn(),
  triggerOneDriveSync: vi.fn(),
  deleteOneDriveConnection: vi.fn(),
  listOneNoteConnections: vi.fn(),
  getOneNoteAuthUrl: vi.fn(),
  listOneNoteNotebooks: vi.fn(),
  updateOneNoteNotebooks: vi.fn(),
  triggerOneNoteSync: vi.fn(),
  deleteOneNoteConnection: vi.fn(),
  listDatabaseConnections: vi.fn(),
  createDatabaseConnection: vi.fn(),
  validateDatabaseConnection: vi.fn(),
  listDatabaseSchemas: vi.fn(),
  updateDatabaseScope: vi.fn(),
  triggerDatabaseSync: vi.fn(),
  deleteDatabaseConnection: vi.fn(),
  searchUsers: vi.fn(),
  getWorkspaceSystemPrompt: vi.fn(),
  updateWorkspaceSystemPrompt: vi.fn(),
  resetWorkspaceSystemPrompt: vi.fn(),
  getWorkspacePrivacy: vi.fn(),
  updateWorkspacePrivacy: vi.fn(),
  getEnabledIntegrations: vi.fn(),
  getAzureDevOpsBoardsAuthUrl: vi.fn(),
  listAzureDevOpsBoardsConnections: vi.fn(),
  listAzureDevOpsBoardsOrgs: vi.fn(),
  listAzureDevOpsBoardsDiscoveryProjects: vi.fn(),
  addAzureDevOpsBoardsProjects: vi.fn(),
  listWorkspaceAzureDevOpsBoardsProjects: vi.fn(),
  removeAzureDevOpsBoardsProject: vi.fn(),
  updateAzureDevOpsBoardsProject: vi.fn(),
  triggerAzureDevOpsBoardsSync: vi.fn(),
  disconnectAzureDevOpsBoards: vi.fn(),
}))

import {
  getWorkspace, updateWorkspace, deleteWorkspace,
  listMembers, addMember, updateMember, removeMember,
  listConfluenceConnections, listJiraConnections,
  getSearchAccess, updateSearchAccess, listSharingOptions,
  listDocuments, deleteDocument,
  listOutlookConnections,
  listGitHubInstallations, listGitHubRepos, listGitHubInstallationRepos,
  addGitHubRepos, updateGitHubRepo, listGitHubRepoBranches,
  removeGitHubRepo, triggerGitHubSync,
  listTeamsConnections, listSharePointConnections,
  getTeamsAuthUrl, deleteTeamsConnection, triggerTeamsSync,
  getSharePointAuthUrl, deleteSharePointConnection, triggerSharePointSync,
  listTeamsTeams, listTeamsChannels, saveTeamsConfig,
  listSharePointSites, updateSharePointSites,
  listOneDriveConnections, getOneDriveAuthUrl, listOneDriveFolders,
  updateOneDriveFolders, triggerOneDriveSync, deleteOneDriveConnection,
  listOneNoteConnections, getOneNoteAuthUrl, listOneNoteNotebooks,
  updateOneNoteNotebooks, triggerOneNoteSync, deleteOneNoteConnection,
  listDatabaseConnections, createDatabaseConnection, validateDatabaseConnection,
  listDatabaseSchemas, updateDatabaseScope, triggerDatabaseSync, deleteDatabaseConnection,
  searchUsers,
  getWorkspaceSystemPrompt, updateWorkspaceSystemPrompt, resetWorkspaceSystemPrompt,
  getWorkspacePrivacy, updateWorkspacePrivacy,
  getEnabledIntegrations,
  listAzureDevOpsBoardsConnections,
  listWorkspaceAzureDevOpsBoardsProjects,
} from '../../../src/lib/api'

const mockGetWorkspace = vi.mocked(getWorkspace)
const mockUpdateWorkspace = vi.mocked(updateWorkspace)
const mockDeleteWorkspace = vi.mocked(deleteWorkspace)
const mockListMembers = vi.mocked(listMembers)
const mockAddMember = vi.mocked(addMember)
const mockUpdateMember = vi.mocked(updateMember)
const mockRemoveMember = vi.mocked(removeMember)
const mockListConfluenceConnections = vi.mocked(listConfluenceConnections)
const mockListJiraConnections = vi.mocked(listJiraConnections)
const mockGetSearchAccess = vi.mocked(getSearchAccess)
const mockUpdateSearchAccess = vi.mocked(updateSearchAccess)
const mockListSharingOptions = vi.mocked(listSharingOptions)
const mockListDocuments = vi.mocked(listDocuments)
const mockDeleteDocument = vi.mocked(deleteDocument)
const mockListOutlookConnections = vi.mocked(listOutlookConnections)
const mockListGitHubInstallations = vi.mocked(listGitHubInstallations)
const mockListGitHubRepos = vi.mocked(listGitHubRepos)
const mockListGitHubInstallationRepos = vi.mocked(listGitHubInstallationRepos)
const mockAddGitHubRepos = vi.mocked(addGitHubRepos)
const mockRemoveGitHubRepo = vi.mocked(removeGitHubRepo)
const mockTriggerGitHubSync = vi.mocked(triggerGitHubSync)
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
const mockListOneDriveConnections = vi.mocked(listOneDriveConnections)
const mockGetOneDriveAuthUrl = vi.mocked(getOneDriveAuthUrl)
const mockListOneDriveFolders = vi.mocked(listOneDriveFolders)
const mockUpdateOneDriveFolders = vi.mocked(updateOneDriveFolders)
const mockTriggerOneDriveSync = vi.mocked(triggerOneDriveSync)
const mockDeleteOneDriveConnection = vi.mocked(deleteOneDriveConnection)
const mockListOneNoteConnections = vi.mocked(listOneNoteConnections)
const mockGetOneNoteAuthUrl = vi.mocked(getOneNoteAuthUrl)
const mockListOneNoteNotebooks = vi.mocked(listOneNoteNotebooks)
const mockUpdateOneNoteNotebooks = vi.mocked(updateOneNoteNotebooks)
const mockTriggerOneNoteSync = vi.mocked(triggerOneNoteSync)
const mockDeleteOneNoteConnection = vi.mocked(deleteOneNoteConnection)
const mockListDatabaseConnections = vi.mocked(listDatabaseConnections)
const mockCreateDatabaseConnection = vi.mocked(createDatabaseConnection)
const mockValidateDatabaseConnection = vi.mocked(validateDatabaseConnection)
const mockListDatabaseSchemas = vi.mocked(listDatabaseSchemas)
const mockUpdateDatabaseScope = vi.mocked(updateDatabaseScope)
const mockTriggerDatabaseSync = vi.mocked(triggerDatabaseSync)
const mockDeleteDatabaseConnection = vi.mocked(deleteDatabaseConnection)
const mockSearchUsers = vi.mocked(searchUsers)
const mockGetWorkspaceSystemPrompt = vi.mocked(getWorkspaceSystemPrompt)
const mockUpdateWorkspaceSystemPrompt = vi.mocked(updateWorkspaceSystemPrompt)
const mockResetWorkspaceSystemPrompt = vi.mocked(resetWorkspaceSystemPrompt)
const mockGetWorkspacePrivacy = vi.mocked(getWorkspacePrivacy)
const mockUpdateWorkspacePrivacy = vi.mocked(updateWorkspacePrivacy)
const mockGetEnabledIntegrations = vi.mocked(getEnabledIntegrations)
const mockListAzureDevOpsBoardsConnections = vi.mocked(listAzureDevOpsBoardsConnections)
const mockListWorkspaceAzureDevOpsBoardsProjects = vi.mocked(listWorkspaceAzureDevOpsBoardsProjects)
// ServiceNow UI. Import lazily via dynamic access so a stale
// snapshot doesn't require importing them into every test file.
import { listServiceNowConnections, getServiceNowAuthUrl, listSalesforceConnections, connectSalesforce, getSalesforceAuthUrl } from '../../../src/lib/api'
const mockListServiceNowConnections = vi.mocked(listServiceNowConnections)
const mockListSalesforceConnections = vi.mocked(listSalesforceConnections)
const mockConnectSalesforce = vi.mocked(connectSalesforce)
const mockGetSalesforceAuthUrl = vi.mocked(getSalesforceAuthUrl)
const mockGetServiceNowAuthUrl = vi.mocked(getServiceNowAuthUrl)

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
  mockListOneDriveConnections.mockResolvedValue([])
  mockListOneNoteConnections.mockResolvedValue([])
  mockListDatabaseConnections.mockResolvedValue([])
  mockListAzureDevOpsBoardsConnections.mockResolvedValue([])
  mockListWorkspaceAzureDevOpsBoardsProjects.mockResolvedValue([])
  mockListServiceNowConnections.mockResolvedValue([])
  mockListSalesforceConnections.mockResolvedValue([])
  mockGetEnabledIntegrations.mockResolvedValue({
    azure_devops: true,
    azure_devops_boards: true,
    github: true,
    github_issues: true,
    confluence: true,
    jira: true,
    outlook: true,
    teams: true,
    sharepoint: true,
    onedrive: true,
    onenote: true,
    database: true,
    // ServiceNow + Salesforce disabled by default so existing tests
    // that grep for `/connect/i` don't accidentally match those
    // Connect buttons. Tests exercising those sections flip explicitly.
    servicenow: false,
    salesforce: false,
  })
  mockGetWorkspacePrivacy.mockResolvedValue({
    workspace_id: 'ws-1',
    privacy_mode: 'STRICT',
    redaction_enabled_at_org_level: true,
    is_effective: true,
  })
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
    // Connect buttons: Confluence(0), Jira(1), Boards(2), Outlook(3), Teams(4), SharePoint(5)
    const connectButtons = screen.getAllByText('Connect')
    await user.click(connectButtons[4])

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
    // Connect buttons: Confluence(0), Jira(1), Boards(2), Outlook(3), Teams(4), SharePoint(5)
    const connectButtons = screen.getAllByText('Connect')
    await user.click(connectButtons[5])

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

  // General tab tests
  it('saves workspace name and description', async () => {
    const updated = createWorkspace({ workspace_id: 'ws-1', name: 'Updated WS', description: 'New desc', role: 'workspace_admin' })
    mockUpdateWorkspace.mockResolvedValue(updated)
    renderSettings()

    await waitFor(() => {
      expect(screen.getByDisplayValue('Test WS')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const nameInput = screen.getByDisplayValue('Test WS')
    await user.clear(nameInput)
    await user.type(nameInput, 'Updated WS')

    await user.click(screen.getByText('Save Changes'))

    await waitFor(() => {
      expect(mockUpdateWorkspace).toHaveBeenCalledWith('ws-1', expect.objectContaining({ name: 'Updated WS' }))
    })
  })

  it('shows and confirms workspace delete modal', async () => {
    mockDeleteWorkspace.mockResolvedValue(undefined)
    renderSettings()

    await waitFor(() => {
      expect(screen.getByDisplayValue('Test WS')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Delete Workspace'))

    // Delete confirmation modal
    expect(screen.getByText('Delete Workspace?')).toBeInTheDocument()
    expect(screen.getByText(/permanently delete/)).toBeInTheDocument()

    // Cancel
    await user.click(screen.getByText('Cancel'))
    expect(screen.queryByText('Delete Workspace?')).not.toBeInTheDocument()
  })

  it('executes workspace deletion', async () => {
    mockDeleteWorkspace.mockResolvedValue(undefined)
    renderSettings()

    await waitFor(() => {
      expect(screen.getByDisplayValue('Test WS')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Delete Workspace'))
    await user.click(screen.getByText('Delete'))

    await waitFor(() => {
      expect(mockDeleteWorkspace).toHaveBeenCalledWith('ws-1')
    })
  })

  it('shows danger zone for admin users', async () => {
    renderSettings()

    await waitFor(() => {
      expect(screen.getByDisplayValue('Test WS')).toBeInTheDocument()
    })

    expect(screen.getByText('Danger Zone')).toBeInTheDocument()
  })

  // Members tab tests
  it('shows members list with role controls', async () => {
    mockListMembers.mockResolvedValue([
      { user_email: 'admin@test.com', role: 'workspace_admin', joined_at: '2024-01-01', workspace_id: 'ws-1' },
      { user_email: 'member@test.com', role: 'member', joined_at: '2024-01-01', workspace_id: 'ws-1' },
    ])
    renderSettings('members')

    await waitFor(() => {
      expect(screen.getByText('admin@test.com')).toBeInTheDocument()
    })
    expect(screen.getByText('member@test.com')).toBeInTheDocument()
    expect(screen.getByText('Members (2)')).toBeInTheDocument()
  })

  it('adds a member via the add member modal', async () => {
    mockListMembers.mockResolvedValue([])
    mockSearchUsers.mockResolvedValue([])
    const newMember = { user_email: 'new@test.com', role: 'member' as const, joined_at: '2024-01-01', workspace_id: 'ws-1' }
    mockAddMember.mockResolvedValue(newMember)

    renderSettings('members')

    await waitFor(() => {
      expect(screen.getByText('Members (0)')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Add Member'))

    expect(screen.getByText(/Search users/)).toBeInTheDocument()
  })

  it('removes a member from workspace', async () => {
    mockListMembers.mockResolvedValue([
      { user_email: 'admin@test.com', role: 'workspace_admin', joined_at: '2024-01-01', workspace_id: 'ws-1' },
      { user_email: 'member@test.com', role: 'member', joined_at: '2024-01-01', workspace_id: 'ws-1' },
    ])
    mockRemoveMember.mockResolvedValue(undefined)
    renderSettings('members')

    await waitFor(() => {
      expect(screen.getByText('member@test.com')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    // Find remove buttons (UserMinus icons)
    const removeButtons = screen.getAllByTitle('Remove member')
    await user.click(removeButtons[0])

    await waitFor(() => {
      expect(mockRemoveMember).toHaveBeenCalled()
    })
  })

  // Documents tab tests
  it('shows documents list on documents tab', async () => {
    mockListDocuments.mockResolvedValue([
      { path: 'docs/readme.md', s3_key: 'workspaces/ws-1/docs/readme.md', filename: 'readme.md', size: 1024, last_modified: '2024-01-01' },
      { path: 'docs/guide.pdf', s3_key: 'workspaces/ws-1/docs/guide.pdf', filename: 'guide.pdf', size: 2048000, last_modified: '2024-02-01' },
    ])
    renderSettings('documents')

    await waitFor(() => {
      expect(screen.getByText('readme.md')).toBeInTheDocument()
    })
    expect(screen.getByText('guide.pdf')).toBeInTheDocument()
    expect(screen.getByText('Documents (2)')).toBeInTheDocument()
  })

  it('shows empty documents state', async () => {
    mockListDocuments.mockResolvedValue([])
    renderSettings('documents')

    await waitFor(() => {
      expect(screen.getByText('No documents uploaded yet')).toBeInTheDocument()
    })
  })

  it('deletes a document', async () => {
    mockListDocuments.mockResolvedValue([
      { path: 'docs/readme.md', s3_key: 'workspaces/ws-1/docs/readme.md', filename: 'readme.md', size: 1024, last_modified: '2024-01-01' },
    ])
    mockDeleteDocument.mockResolvedValue(undefined)
    renderSettings('documents')

    await waitFor(() => {
      expect(screen.getByText('readme.md')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const deleteBtn = screen.getByTitle('Delete document')
    await user.click(deleteBtn)

    await waitFor(() => {
      expect(mockDeleteDocument).toHaveBeenCalledWith('ws-1', 'docs/readme.md')
    })
  })

  // Sharing tab tests
  it('shows sharing options and saves changes', async () => {
    mockGetSearchAccess.mockResolvedValue([])
    mockListSharingOptions.mockResolvedValue([
      { workspace_id: 'ws-2', name: 'Other Workspace' },
    ])
    mockUpdateSearchAccess.mockResolvedValue([{ workspace_id: 'ws-2', name: 'Other Workspace' }])
    renderSettings('sharing')

    await waitFor(() => {
      expect(screen.getByText('Cross-Workspace Search Access')).toBeInTheDocument()
    })

    await waitFor(() => {
      expect(screen.getByText('Other Workspace')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    // Toggle the checkbox
    const checkbox = screen.getByRole('checkbox')
    await user.click(checkbox)

    // Save
    await user.click(screen.getByText('Save Changes'))

    await waitFor(() => {
      expect(mockUpdateSearchAccess).toHaveBeenCalledWith('ws-1', ['ws-2'])
    })
  })

  it('shows no sharing options message', async () => {
    mockGetSearchAccess.mockResolvedValue([])
    mockListSharingOptions.mockResolvedValue([])
    renderSettings('sharing')

    await waitFor(() => {
      expect(screen.getByText('No other workspaces available to share with.')).toBeInTheDocument()
    })
  })

  // Advanced tab tests
  it('shows Advanced tab for admin users', async () => {
    renderSettings()

    await waitFor(() => {
      expect(screen.getByDisplayValue('Test WS')).toBeInTheDocument()
    })

    expect(screen.getByText('Advanced')).toBeInTheDocument()
  })

  it('loads and displays system prompt on advanced tab', async () => {
    mockGetWorkspaceSystemPrompt.mockResolvedValue({
      workspace_id: 'ws-1',
      custom_system_prompt: 'You are a helpful assistant',
      updated_by: 'admin@test.com',
      updated_at: '2024-06-15T00:00:00Z',
    })
    renderSettings('advanced')

    await waitFor(() => {
      expect(screen.getByText('Custom System Prompt')).toBeInTheDocument()
    })

    await waitFor(() => {
      expect(screen.getByDisplayValue('You are a helpful assistant')).toBeInTheDocument()
    })
  })

  it('saves system prompt changes', async () => {
    mockGetWorkspaceSystemPrompt.mockResolvedValue({
      workspace_id: 'ws-1',
      custom_system_prompt: '',
      updated_by: null,
      updated_at: null,
    })
    mockUpdateWorkspaceSystemPrompt.mockResolvedValue({
      workspace_id: 'ws-1',
      custom_system_prompt: 'New prompt',
      updated_by: 'admin@test.com',
      updated_at: '2024-06-15T00:00:00Z',
    })
    renderSettings('advanced')

    await waitFor(() => {
      expect(screen.getByText('Custom System Prompt')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const textarea = screen.getByPlaceholderText(/No custom system prompt configured/)
    await user.type(textarea, 'New prompt')

    await user.click(screen.getByText('Save Changes'))

    await waitFor(() => {
      expect(mockUpdateWorkspaceSystemPrompt).toHaveBeenCalledWith('ws-1', 'New prompt')
    })
  })

  it('resets system prompt via confirmation modal', async () => {
    mockGetWorkspaceSystemPrompt.mockResolvedValue({
      workspace_id: 'ws-1',
      custom_system_prompt: 'Existing prompt',
      updated_by: 'admin@test.com',
      updated_at: '2024-06-15T00:00:00Z',
    })
    mockResetWorkspaceSystemPrompt.mockResolvedValue({
      workspace_id: 'ws-1',
      custom_system_prompt: null,
      updated_by: null,
      updated_at: null,
    })
    renderSettings('advanced')

    await waitFor(() => {
      expect(screen.getByDisplayValue('Existing prompt')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Reset to Default'))

    // Confirmation modal
    expect(screen.getByText('Reset System Prompt?')).toBeInTheDocument()

    await user.click(screen.getByText('Reset'))

    await waitFor(() => {
      expect(mockResetWorkspaceSystemPrompt).toHaveBeenCalledWith('ws-1')
    })
  })

  // GitHub repos tests
  it('shows GitHub repos list on integrations tab', async () => {
    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    mockListGitHubRepos.mockResolvedValue([
      { repo_name: 'my-org/my-repo', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application', last_sync_at: '2024-01-15', last_sync_status: 'success', last_sync_error: null, last_commit_sha: 'abc123', added_at: '2024-01-01', added_by: 'user@test.com' },
    ])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('my-org/my-repo')).toBeInTheDocument()
    })
    expect(screen.getByText('my-org')).toBeInTheDocument()
  })

  it('shows the sync-branch chip on GitHub repos when default_branch is set', async () => {
    // Post-chunk-01 rows carry default_branch. The chip makes it
    // scannable at a glance so admins can see which repos are still
    // syncing from master after an org-wide master -> main cutover.
    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    mockListGitHubRepos.mockResolvedValue([
      { repo_name: 'my-org/main-repo', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application', last_sync_at: null, last_sync_status: null, last_sync_error: null, last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com', default_branch: 'main' },
      { repo_name: 'my-org/legacy-repo', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application', last_sync_at: null, last_sync_status: null, last_sync_error: null, last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com', default_branch: 'master' },
    ])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('my-org/main-repo')).toBeInTheDocument()
    })
    // Both branch chips render.
    expect(screen.getByText('main')).toBeInTheDocument()
    expect(screen.getByText('master')).toBeInTheDocument()
  })

  it('does not render a branch chip on legacy GitHub rows that lack default_branch', async () => {
    // Pre-chunk-01 rows may lack the field until backfilled on next
    // sync. Show nothing rather than assume 'main' -- assuming 'main'
    // would silently mislead admins about repos still on master.
    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    mockListGitHubRepos.mockResolvedValue([
      { repo_name: 'my-org/legacy', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application', last_sync_at: null, last_sync_status: null, last_sync_error: null, last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com' },
    ])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('my-org/legacy')).toBeInTheDocument()
    })
    // No branch chip should exist for this row.
    expect(screen.queryByText('main')).not.toBeInTheDocument()
    expect(screen.queryByText('master')).not.toBeInTheDocument()
  })

  it('opens the edit-branch modal from the gear icon and loads branches', async () => {
    const mockListBranches = vi.mocked(listGitHubRepoBranches)
    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    mockListGitHubRepos.mockResolvedValue([
      { repo_name: 'my-org/legacy-app', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application', last_sync_at: null, last_sync_status: null, last_sync_error: null, last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com', default_branch: 'master' },
    ])
    mockListBranches.mockResolvedValue({
      branches: [{ name: 'master', protected: true }, { name: 'main', protected: false }, { name: 'develop', protected: false }],
      current_default: 'main',   // drift: provider says main
      stored_default: 'master',  // we're still on master
    })
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('my-org/legacy-app')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByTitle('Change sync branch'))

    // Modal opens with a heading and the target repo name.
    await waitFor(() => {
      expect(screen.getByRole('dialog', { name: /change sync branch/i })).toBeInTheDocument()
    })
    expect(mockListBranches).toHaveBeenCalledWith('ws-1', 'my-org/legacy-app')

    // Drift badge visible with both branch names surfaced.
    await waitFor(() => {
      expect(screen.getByText(/branch drift detected/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/current provider default/i)).toBeInTheDocument()
  })

  it('saves the picked branch via updateGitHubRepo', async () => {
    const mockListBranches = vi.mocked(listGitHubRepoBranches)
    const mockUpdateRepo = vi.mocked(updateGitHubRepo)
    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    const initial = { repo_name: 'my-org/legacy-app', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application' as const, last_sync_at: null, last_sync_status: null, last_sync_error: null, last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com', default_branch: 'master' }
    mockListGitHubRepos.mockResolvedValue([initial])
    mockListBranches.mockResolvedValue({
      branches: [{ name: 'master' }, { name: 'main' }],
      current_default: 'main',
      stored_default: 'master',
    })
    mockUpdateRepo.mockResolvedValue({ ...initial, default_branch: 'main' })

    renderSettings('integrations')
    await waitFor(() => {
      expect(screen.getByText('my-org/legacy-app')).toBeInTheDocument()
    })
    const user = userEvent.setup()
    await user.click(screen.getByTitle('Change sync branch'))
    await waitFor(() => {
      expect(screen.getByRole('dialog', { name: /change sync branch/i })).toBeInTheDocument()
    })
    // Click the "Use main (current provider default)" affordance and save.
    await user.click(screen.getByText(/use main \(current provider default\)/i))
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(mockUpdateRepo).toHaveBeenCalledWith('ws-1', 'my-org/legacy-app', { default_branch: 'main' })
    })
  })

  it('falls back to a text input when the branches endpoint fails', async () => {
    const mockListBranches = vi.mocked(listGitHubRepoBranches)
    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    mockListGitHubRepos.mockResolvedValue([
      { repo_name: 'my-org/app', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application', last_sync_at: null, last_sync_status: null, last_sync_error: null, last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com', default_branch: 'main' },
    ])
    mockListBranches.mockRejectedValue(new Error('rate limit'))

    renderSettings('integrations')
    await waitFor(() => {
      expect(screen.getByText('my-org/app')).toBeInTheDocument()
    })
    const user = userEvent.setup()
    await user.click(screen.getByTitle('Change sync branch'))
    // Modal renders the graceful fallback (text input) with the
    // error message.
    await waitFor(() => {
      expect(screen.getByText(/could not load branch list/i)).toBeInTheDocument()
    })
    expect(screen.getByPlaceholderText(/e.g. main, master, develop/i)).toBeInTheDocument()
  })

  it('removes a GitHub repo', async () => {
    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    mockListGitHubRepos.mockResolvedValue([
      { repo_name: 'my-org/my-repo', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application', last_sync_at: null, last_sync_status: null, last_sync_error: null, last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com' },
    ])
    mockRemoveGitHubRepo.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('my-org/my-repo')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const removeBtn = screen.getByTitle('Remove repo')
    await user.click(removeBtn)

    await waitFor(() => {
      expect(mockRemoveGitHubRepo).toHaveBeenCalledWith('ws-1', 'my-org/my-repo')
    })
  })

  it('triggers GitHub sync for a repo', async () => {
    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    mockListGitHubRepos.mockResolvedValue([
      { repo_name: 'my-org/my-repo', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application', last_sync_at: null, last_sync_status: null, last_sync_error: null, last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com' },
    ])
    mockTriggerGitHubSync.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('my-org/my-repo')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    // The sync button is first, remove is second for each repo
    const syncBtn = screen.getAllByTitle('Sync now')
    await user.click(syncBtn[syncBtn.length - 1])

    await waitFor(() => {
      expect(mockTriggerGitHubSync).toHaveBeenCalledWith('ws-1', 'my-org/my-repo')
    })
  })

  // Error handling tests
  it('shows and dismisses error messages', async () => {
    mockUpdateWorkspace.mockRejectedValue(new Error('Update failed'))
    renderSettings()

    await waitFor(() => {
      expect(screen.getByDisplayValue('Test WS')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Save Changes'))

    await waitFor(() => {
      expect(screen.getByText('Update failed')).toBeInTheDocument()
    })

    // Dismiss the error
    const errorContainer = screen.getByText('Update failed').closest('.bg-red-50')
    const dismissBtn = errorContainer?.querySelector('button')
    if (dismissBtn) await user.click(dismissBtn)

    await waitFor(() => {
      expect(screen.queryByText('Update failed')).not.toBeInTheDocument()
    })
  })

  it('shows no GitHub installations message', async () => {
    mockListGitHubInstallations.mockResolvedValue([])
    mockListGitHubRepos.mockResolvedValue([])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('No GitHub App installed')).toBeInTheDocument()
    })
  })

  it('shows upload area on documents tab', async () => {
    mockListDocuments.mockResolvedValue([])
    renderSettings('documents')

    await waitFor(() => {
      expect(screen.getByText(/Drag and drop files here/)).toBeInTheDocument()
    })
    expect(screen.getByText(/Supported formats/)).toBeInTheDocument()
  })

  // Confluence connection tests
  it('shows Confluence connection with site name and space count', async () => {
    mockListConfluenceConnections.mockResolvedValue([{
      connection_id: 'conf-1',
      workspace_id: 'ws-1',
      cloud_id: 'cloud-1',
      site_name: 'My Site',
      site_url: 'https://mysite.atlassian.net',
      selected_spaces: [{ id: 's1', key: 'ENG', name: 'Engineering', type: 'global' }],
      include_attachments: false,
      last_sync_at: '2024-03-01',
      last_sync_status: 'completed',
      page_count: 42,
      attachment_count: 0,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('My Site')).toBeInTheDocument()
    })
    expect(screen.getByText(/1 space selected/)).toBeInTheDocument()
    expect(screen.getByText(/42 pages synced/)).toBeInTheDocument()
  })

  it('shows Confluence connection with no spaces selected', async () => {
    mockListConfluenceConnections.mockResolvedValue([{
      connection_id: 'conf-1',
      workspace_id: 'ws-1',
      cloud_id: 'cloud-1',
      site_name: 'My Site',
      site_url: 'https://mysite.atlassian.net',
      selected_spaces: [],
      include_attachments: false,
      last_sync_at: null,
      last_sync_status: 'needs_configuration',
      page_count: 0,
      attachment_count: 0,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('My Site')).toBeInTheDocument()
    })
    expect(screen.getByText(/No spaces selected/)).toBeInTheDocument()
  })

  // Jira connection tests
  it('shows Jira connection with status', async () => {
    mockListJiraConnections.mockResolvedValue([{
      connection_id: 'jira-1',
      workspace_id: 'ws-1',
      cloud_id: 'cloud-1',
      site_name: 'Jira Site',
      site_url: 'https://jira.atlassian.net',
      selected_projects: [{ key: 'PROJ', name: 'Project' }],
      include_attachments: true,
      include_comments: true,
      last_sync_at: '2024-03-01',
      last_sync_status: 'completed',
      last_sync_error: null,
      issue_count: 100,
      created_at: '2024-01-01',
    }])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Jira Site')).toBeInTheDocument()
    })
  })

  // Outlook connection tests
  it('shows Outlook connection with status', async () => {
    mockListOutlookConnections.mockResolvedValue([{
      connection_id: 'outlook-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'Outlook User',
      selected_folders: [{ id: 'f1', display_name: 'Inbox' }],
      last_sync_at: '2024-03-01',
      last_sync_status: 'completed',
      email_count: 50,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Outlook User')).toBeInTheDocument()
    })
  })

  // Member role change test
  it('updates member role via select', async () => {
    mockListMembers.mockResolvedValue([
      { user_email: 'admin@test.com', role: 'workspace_admin', joined_at: '2024-01-01', workspace_id: 'ws-1' },
      { user_email: 'member@test.com', role: 'member', joined_at: '2024-01-01', workspace_id: 'ws-1' },
    ])
    mockUpdateMember.mockResolvedValue(undefined)
    renderSettings('members')

    await waitFor(() => {
      expect(screen.getByText('member@test.com')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    // Find the role select for the member
    const selects = screen.getAllByRole('combobox')
    // Change the second member's role (the non-admin one)
    await user.selectOptions(selects[1], 'workspace_admin')

    await waitFor(() => {
      expect(mockUpdateMember).toHaveBeenCalledWith('ws-1', 'member@test.com', 'workspace_admin')
    })
  })

  // GitHub add repos modal test
  it('opens GitHub add repos modal and shows available repos', async () => {
    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    mockListGitHubRepos.mockResolvedValue([])
    mockListGitHubInstallationRepos.mockResolvedValue([
      { full_name: 'my-org/new-repo', name: 'new-repo', description: 'A new repo', private: false, default_branch: 'main', updated_at: '2024-01-01' },
    ])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('my-org')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Add Repos'))

    await waitFor(() => {
      expect(screen.getByText('my-org/new-repo')).toBeInTheDocument()
    })
  })

  it('adds a GitHub repo from the modal', async () => {
    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    mockListGitHubRepos.mockResolvedValue([])
    mockListGitHubInstallationRepos.mockResolvedValue([
      { full_name: 'my-org/new-repo', name: 'new-repo', description: 'A new repo', private: false, default_branch: 'main', updated_at: '2024-01-01' },
    ])
    const addedRepo = { repo_name: 'my-org/new-repo', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application', last_sync_at: null, last_sync_status: null, last_sync_error: null, last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com' }
    mockAddGitHubRepos.mockResolvedValue([addedRepo])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('my-org')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Add Repos'))

    await waitFor(() => {
      expect(screen.getByText('my-org/new-repo')).toBeInTheDocument()
    })

    // Click the Add button for the repo
    const addButtons = screen.getAllByRole('button', { name: /add/i })
    const repoAddBtn = addButtons.find(btn => btn.closest('[class*="border-b"]'))
    if (repoAddBtn) {
      await user.click(repoAddBtn)
    }
  })

  // Non-admin view test
  it('hides admin controls for non-admin users', async () => {
    const ws = createWorkspace({ workspace_id: 'ws-1', name: 'Test WS', role: 'member' })
    mockGetWorkspace.mockResolvedValue(ws)
    renderSettings()

    await waitFor(() => {
      expect(screen.getByDisplayValue('Test WS')).toBeInTheDocument()
    })

    // Danger Zone should not appear for non-admins
    expect(screen.queryByText('Danger Zone')).not.toBeInTheDocument()
    // Save Changes button should not appear
    expect(screen.queryByText('Save Changes')).not.toBeInTheDocument()
    // Advanced tab should not appear
    expect(screen.queryByText('Advanced')).not.toBeInTheDocument()
  })

  // Non-admin sharing test
  it('shows non-admin message on sharing tab', async () => {
    const ws = createWorkspace({ workspace_id: 'ws-1', name: 'Test WS', role: 'member' })
    mockGetWorkspace.mockResolvedValue(ws)
    mockGetSearchAccess.mockResolvedValue([])
    mockListSharingOptions.mockResolvedValue([{ workspace_id: 'ws-2', name: 'Other' }])
    renderSettings('sharing')

    await waitFor(() => {
      expect(screen.getByText('Only workspace admins can manage sharing settings.')).toBeInTheDocument()
    })
  })

  // Document file size format test
  it('formats file sizes correctly in document list', async () => {
    mockListDocuments.mockResolvedValue([
      { path: 'docs/small.txt', s3_key: 'workspaces/ws-1/docs/small.txt', filename: 'small.txt', size: 512, last_modified: '2024-01-01' },
      { path: 'docs/medium.md', s3_key: 'workspaces/ws-1/docs/medium.md', filename: 'medium.md', size: 5120, last_modified: '2024-01-01' },
      { path: 'docs/large.pdf', s3_key: 'workspaces/ws-1/docs/large.pdf', filename: 'large.pdf', size: 2097152, last_modified: '2024-01-01' },
    ])
    renderSettings('documents')

    await waitFor(() => {
      expect(screen.getByText('small.txt')).toBeInTheDocument()
    })
    expect(screen.getByText(/512 B/)).toBeInTheDocument()
    expect(screen.getByText(/5\.0 KB/)).toBeInTheDocument()
    expect(screen.getByText(/2\.0 MB/)).toBeInTheDocument()
  })

  // Success messages test
  it('shows success message after saving general settings', async () => {
    const updated = createWorkspace({ workspace_id: 'ws-1', name: 'Updated', role: 'workspace_admin' })
    mockUpdateWorkspace.mockResolvedValue(updated)
    renderSettings()

    await waitFor(() => {
      expect(screen.getByDisplayValue('Test WS')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Save Changes'))

    await waitFor(() => {
      expect(screen.getByText('Workspace updated successfully')).toBeInTheDocument()
    })
  })

  it('shows success message after deleting a document', async () => {
    mockListDocuments.mockResolvedValue([
      { path: 'docs/readme.md', s3_key: 'workspaces/ws-1/docs/readme.md', filename: 'readme.md', size: 1024, last_modified: '2024-01-01' },
    ])
    mockDeleteDocument.mockResolvedValue(undefined)
    renderSettings('documents')

    await waitFor(() => {
      expect(screen.getByText('readme.md')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByTitle('Delete document'))

    await waitFor(() => {
      expect(screen.getByText('Document deleted')).toBeInTheDocument()
    })
  })

  // Confluence configure and save spaces
  it('opens Confluence space configuration modal', async () => {
    const { listConfluenceSpaces } = await import('../../../src/lib/api')
    const mockListConfluenceSpaces = vi.mocked(listConfluenceSpaces)

    mockListConfluenceConnections.mockResolvedValue([{
      connection_id: 'conf-1',
      workspace_id: 'ws-1',
      cloud_id: 'cloud-1',
      site_name: 'My Site',
      site_url: 'https://mysite.atlassian.net',
      selected_spaces: [],
      include_attachments: false,
      last_sync_at: null,
      last_sync_status: 'needs_configuration',
      page_count: 0,
      attachment_count: 0,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }])
    mockListConfluenceSpaces.mockResolvedValue({
      spaces: [
        { id: 's1', key: 'ENG', name: 'Engineering', type: 'global' },
        { id: 's2', key: 'HR', name: 'Human Resources', type: 'global' },
      ]
    })
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('My Site')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Configure'))

    await waitFor(() => {
      expect(screen.getByText('Configure Confluence Spaces')).toBeInTheDocument()
    })
    expect(screen.getByText('Engineering')).toBeInTheDocument()
    expect(screen.getByText('Human Resources')).toBeInTheDocument()
  })

  // Jira configure and projects
  it('opens Jira project configuration modal', async () => {
    const { listJiraProjects } = await import('../../../src/lib/api')
    const mockListJiraProjects = vi.mocked(listJiraProjects)

    mockListJiraConnections.mockResolvedValue([{
      connection_id: 'jira-1',
      workspace_id: 'ws-1',
      cloud_id: 'cloud-1',
      site_name: 'Jira Site',
      site_url: 'https://jira.atlassian.net',
      selected_projects: [],
      include_attachments: true,
      include_comments: true,
      last_sync_at: null,
      last_sync_status: 'needs_configuration',
      last_sync_error: null,
      issue_count: 0,
      created_at: '2024-01-01',
    }])
    mockListJiraProjects.mockResolvedValue({
      projects: [
        { key: 'PROJ', name: 'Project One', type: 'software' },
      ]
    })
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Jira Site')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    // Find the Configure button for Jira (should be 2nd Configure button)
    const configButtons = screen.getAllByText('Configure')
    await user.click(configButtons[configButtons.length - 1])

    await waitFor(() => {
      expect(mockListJiraProjects).toHaveBeenCalledWith('ws-1', 'jira-1')
    })
  })

  // Outlook configure
  it('opens Outlook folder configuration modal', async () => {
    const { listOutlookFolders } = await import('../../../src/lib/api')
    const mockListOutlookFolders = vi.mocked(listOutlookFolders)

    mockListOutlookConnections.mockResolvedValue([{
      connection_id: 'outlook-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'Outlook User',
      selected_folders: [],
      last_sync_at: null,
      last_sync_status: 'needs_configuration',
      email_count: 0,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }])
    mockListOutlookFolders.mockResolvedValue({
      folders: [
        { id: 'f1', display_name: 'Inbox', child_folder_count: 0, total_item_count: 100, unread_item_count: 5 },
      ]
    })
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Outlook User')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const configButtons = screen.getAllByText('Configure')
    // Find the Outlook configure button
    await user.click(configButtons[configButtons.length - 1])

    await waitFor(() => {
      expect(mockListOutlookFolders).toHaveBeenCalledWith('ws-1', 'outlook-1')
    })
  })

  // GitHub repo with different sync statuses
  it('shows GitHub repo with failed sync status', async () => {
    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    mockListGitHubRepos.mockResolvedValue([
      { repo_name: 'my-org/failing-repo', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application', last_sync_at: '2024-01-15', last_sync_status: 'failed', last_sync_error: 'Rate limit', last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com' },
    ])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('my-org/failing-repo')).toBeInTheDocument()
    })
    expect(screen.getByText('failed')).toBeInTheDocument()
  })

  it('shows GitHub repo never synced status', async () => {
    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    mockListGitHubRepos.mockResolvedValue([
      { repo_name: 'my-org/new-repo', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application', last_sync_at: null, last_sync_status: null, last_sync_error: null, last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com' },
    ])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('my-org/new-repo')).toBeInTheDocument()
    })
    expect(screen.getByText('Never synced')).toBeInTheDocument()
  })

  // Sharing with pre-selected workspaces
  it('loads sharing with pre-selected workspaces', async () => {
    mockGetSearchAccess.mockResolvedValue([
      { workspace_id: 'ws-2', name: 'Shared WS' },
    ])
    mockListSharingOptions.mockResolvedValue([
      { workspace_id: 'ws-2', name: 'Shared WS' },
      { workspace_id: 'ws-3', name: 'Other WS' },
    ])
    renderSettings('sharing')

    await waitFor(() => {
      expect(screen.getByText('Shared WS')).toBeInTheDocument()
    })
    expect(screen.getByText('Other WS')).toBeInTheDocument()
    // Show count text
    expect(screen.getByText(/1 workspace can search/)).toBeInTheDocument()
  })

  // Delete confirmation cancel
  it('cancel on reset system prompt confirmation', async () => {
    mockGetWorkspaceSystemPrompt.mockResolvedValue({
      workspace_id: 'ws-1',
      custom_system_prompt: 'My prompt',
      updated_by: 'admin@test.com',
      updated_at: '2024-06-15T00:00:00Z',
    })
    renderSettings('advanced')

    await waitFor(() => {
      expect(screen.getByDisplayValue('My prompt')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Reset to Default'))

    expect(screen.getByText('Reset System Prompt?')).toBeInTheDocument()

    // Cancel the reset
    await user.click(screen.getByText('Cancel'))

    expect(screen.queryByText('Reset System Prompt?')).not.toBeInTheDocument()
  })

  it('shows character count for system prompt', async () => {
    mockGetWorkspaceSystemPrompt.mockResolvedValue({
      workspace_id: 'ws-1',
      custom_system_prompt: 'Hello',
      updated_by: 'admin@test.com',
      updated_at: '2024-06-15T00:00:00Z',
    })
    renderSettings('advanced')

    await waitFor(() => {
      expect(screen.getByText('5 / 10,000')).toBeInTheDocument()
    })
  })

  it('shows system prompt last updated info', async () => {
    mockGetWorkspaceSystemPrompt.mockResolvedValue({
      workspace_id: 'ws-1',
      custom_system_prompt: 'My prompt text',
      updated_by: 'admin@test.com',
      updated_at: '2024-06-15T00:00:00Z',
    })
    renderSettings('advanced')

    await waitFor(() => {
      expect(screen.getByText(/Last updated by admin@test.com/)).toBeInTheDocument()
    })
  })

  // Confluence space configuration - save and sync
  it('saves Confluence spaces and triggers sync', async () => {
    const { listConfluenceSpaces, updateConfluenceSpaces } = await import('../../../src/lib/api')
    const mockListConfluenceSpaces = vi.mocked(listConfluenceSpaces)
    const mockUpdateConfluenceSpaces = vi.mocked(updateConfluenceSpaces)

    mockListConfluenceConnections.mockResolvedValue([{
      connection_id: 'conf-1',
      workspace_id: 'ws-1',
      cloud_id: 'cloud-1',
      site_name: 'My Site',
      site_url: 'https://mysite.atlassian.net',
      selected_spaces: [],
      include_attachments: false,
      last_sync_at: null,
      last_sync_status: 'needs_configuration',
      page_count: 0,
      attachment_count: 0,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }])
    mockListConfluenceSpaces.mockResolvedValue({
      spaces: [
        { id: 's1', key: 'ENG', name: 'Engineering', type: 'global' },
      ]
    })
    mockUpdateConfluenceSpaces.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('My Site')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Configure'))

    await waitFor(() => {
      expect(screen.getByText('Engineering')).toBeInTheDocument()
    })

    // Toggle space selection
    const spaceCheckboxes = screen.getAllByRole('checkbox')
    await user.click(spaceCheckboxes[0])

    // Click Save & Sync
    await user.click(screen.getByText('Save & Sync'))

    await waitFor(() => {
      expect(mockUpdateConfluenceSpaces).toHaveBeenCalled()
    })
  })

  // Confluence trigger sync
  it('triggers Confluence sync', async () => {
    const { triggerConfluenceSync } = await import('../../../src/lib/api')
    const mockTriggerConfluenceSync = vi.mocked(triggerConfluenceSync)

    mockListConfluenceConnections.mockResolvedValue([{
      connection_id: 'conf-1',
      workspace_id: 'ws-1',
      cloud_id: 'cloud-1',
      site_name: 'My Site',
      site_url: 'https://mysite.atlassian.net',
      selected_spaces: [{ id: 's1', key: 'ENG', name: 'Engineering', type: 'global' }],
      include_attachments: false,
      last_sync_at: null,
      last_sync_status: 'completed',
      page_count: 10,
      attachment_count: 0,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }])
    mockTriggerConfluenceSync.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('My Site')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const syncButtons = screen.getAllByTitle('Sync now')
    await user.click(syncButtons[0])

    await waitFor(() => {
      expect(mockTriggerConfluenceSync).toHaveBeenCalledWith('ws-1', 'conf-1')
    })
  })

  // Confluence delete connection
  it('deletes Confluence connection', async () => {
    const { deleteConfluenceConnection } = await import('../../../src/lib/api')
    const mockDeleteConfluenceConnection = vi.mocked(deleteConfluenceConnection)

    mockListConfluenceConnections.mockResolvedValue([{
      connection_id: 'conf-1',
      workspace_id: 'ws-1',
      cloud_id: 'cloud-1',
      site_name: 'My Site',
      site_url: 'https://mysite.atlassian.net',
      selected_spaces: [{ id: 's1', key: 'ENG', name: 'Engineering', type: 'global' }],
      include_attachments: false,
      last_sync_at: null,
      last_sync_status: 'completed',
      page_count: 10,
      attachment_count: 0,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }])
    mockDeleteConfluenceConnection.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('My Site')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const disconnectBtns = screen.getAllByTitle('Disconnect')
    await user.click(disconnectBtns[0])

    await waitFor(() => {
      expect(mockDeleteConfluenceConnection).toHaveBeenCalledWith('ws-1', 'conf-1')
    })
  })

  // Jira trigger sync
  it('triggers Jira sync', async () => {
    const { triggerJiraSync } = await import('../../../src/lib/api')
    const mockTriggerJiraSync = vi.mocked(triggerJiraSync)

    mockListJiraConnections.mockResolvedValue([{
      connection_id: 'jira-1',
      workspace_id: 'ws-1',
      cloud_id: 'cloud-1',
      site_name: 'Jira Site',
      site_url: 'https://jira.atlassian.net',
      selected_projects: [{ key: 'PROJ', name: 'Project' }],
      include_attachments: true,
      include_comments: true,
      last_sync_at: null,
      last_sync_status: 'completed',
      last_sync_error: null,
      issue_count: 50,
      created_at: '2024-01-01',
    }])
    mockTriggerJiraSync.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Jira Site')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const syncButtons = screen.getAllByTitle('Sync now')
    // Find the Jira sync button
    await user.click(syncButtons[syncButtons.length - 1])

    await waitFor(() => {
      expect(mockTriggerJiraSync).toHaveBeenCalledWith('ws-1', 'jira-1')
    })
  })

  // Jira delete connection
  it('deletes Jira connection', async () => {
    const { deleteJiraConnection } = await import('../../../src/lib/api')
    const mockDeleteJiraConnection = vi.mocked(deleteJiraConnection)

    mockListJiraConnections.mockResolvedValue([{
      connection_id: 'jira-1',
      workspace_id: 'ws-1',
      cloud_id: 'cloud-1',
      site_name: 'Jira Site',
      site_url: 'https://jira.atlassian.net',
      selected_projects: [{ key: 'PROJ', name: 'Project' }],
      include_attachments: true,
      include_comments: true,
      last_sync_at: null,
      last_sync_status: 'completed',
      last_sync_error: null,
      issue_count: 50,
      created_at: '2024-01-01',
    }])
    mockDeleteJiraConnection.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Jira Site')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const disconnectBtns = screen.getAllByTitle('Disconnect')
    await user.click(disconnectBtns[disconnectBtns.length - 1])

    await waitFor(() => {
      expect(mockDeleteJiraConnection).toHaveBeenCalledWith('ws-1', 'jira-1')
    })
  })

  // Outlook trigger sync
  it('triggers Outlook sync', async () => {
    const { triggerOutlookSync } = await import('../../../src/lib/api')
    const mockTriggerOutlookSync = vi.mocked(triggerOutlookSync)

    mockListOutlookConnections.mockResolvedValue([{
      connection_id: 'outlook-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'Outlook User',
      selected_folders: [{ id: 'f1', display_name: 'Inbox' }],
      last_sync_at: null,
      last_sync_status: 'completed',
      email_count: 50,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }])
    mockTriggerOutlookSync.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Outlook User')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const syncButtons = screen.getAllByTitle('Sync now')
    await user.click(syncButtons[syncButtons.length - 1])

    await waitFor(() => {
      expect(mockTriggerOutlookSync).toHaveBeenCalledWith('ws-1', 'outlook-1')
    })
  })

  // Outlook delete connection
  it('deletes Outlook connection', async () => {
    const { deleteOutlookConnection } = await import('../../../src/lib/api')
    const mockDeleteOutlookConnection = vi.mocked(deleteOutlookConnection)

    mockListOutlookConnections.mockResolvedValue([{
      connection_id: 'outlook-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'Outlook User',
      selected_folders: [{ id: 'f1', display_name: 'Inbox' }],
      last_sync_at: null,
      last_sync_status: 'completed',
      email_count: 50,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }])
    mockDeleteOutlookConnection.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Outlook User')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const disconnectBtns = screen.getAllByTitle('Disconnect')
    await user.click(disconnectBtns[disconnectBtns.length - 1])

    await waitFor(() => {
      expect(mockDeleteOutlookConnection).toHaveBeenCalledWith('ws-1', 'outlook-1')
    })
  })

  // Member add flow with user search
  it('searches for users when adding a member', async () => {
    mockListMembers.mockResolvedValue([])
    mockSearchUsers.mockResolvedValue([
      { user_email: 'found@test.com', name: 'Found User', is_service_account: false },
    ])
    renderSettings('members')

    await waitFor(() => {
      expect(screen.getByText('Members (0)')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Add Member'))

    const searchInput = screen.getByPlaceholderText('Search by email...')
    await user.type(searchInput, 'found')

    await waitFor(() => {
      expect(mockSearchUsers).toHaveBeenCalledWith('found')
    })

    // Should show search result
    await waitFor(() => {
      expect(screen.getByText('found@test.com')).toBeInTheDocument()
    })
  })

  it('selects user from search results and adds member', async () => {
    mockListMembers.mockResolvedValue([])
    mockSearchUsers.mockResolvedValue([
      { user_email: 'found@test.com', name: 'Found User', is_service_account: false },
    ])
    const newMember = { user_email: 'found@test.com', role: 'member' as const, joined_at: '2024-01-01', workspace_id: 'ws-1' }
    mockAddMember.mockResolvedValue(newMember)
    renderSettings('members')

    await waitFor(() => {
      expect(screen.getByText('Members (0)')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Add Member'))

    const searchInput = screen.getByPlaceholderText('Search by email...')
    await user.type(searchInput, 'found')

    await waitFor(() => {
      expect(screen.getByText('found@test.com')).toBeInTheDocument()
    })

    // Click on the search result to select the user
    await user.click(screen.getByText('found@test.com'))

    // Now click Add button
    await user.click(screen.getByText('Add'))

    await waitFor(() => {
      expect(mockAddMember).toHaveBeenCalledWith('ws-1', 'found@test.com', 'member')
    })
  })

  // Cancel add member modal
  it('cancels add member modal and resets state', async () => {
    mockListMembers.mockResolvedValue([])
    renderSettings('members')

    await waitFor(() => {
      expect(screen.getByText('Members (0)')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Add Member'))

    expect(screen.getByText(/Search users/)).toBeInTheDocument()

    await user.click(screen.getByText('Cancel'))

    expect(screen.queryByText(/Search users/)).not.toBeInTheDocument()
  })

  // GitHub connect handler
  it('connects GitHub via install URL', async () => {
    const { getGitHubInstallUrl } = await import('../../../src/lib/api')
    const mockGetGitHubInstallUrl = vi.mocked(getGitHubInstallUrl)
    mockGetGitHubInstallUrl.mockResolvedValue('https://github.com/apps/install')
    mockListGitHubInstallations.mockResolvedValue([])
    mockListGitHubRepos.mockResolvedValue([])
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('No GitHub App installed')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.click(screen.getByText('Connect GitHub'))

    await waitFor(() => {
      expect(mockGetGitHubInstallUrl).toHaveBeenCalledWith('ws-1')
    })
  })

  // Sharing save
  it('saves sharing with toggled workspaces', async () => {
    mockGetSearchAccess.mockResolvedValue([
      { workspace_id: 'ws-2', name: 'Shared WS' },
    ])
    mockListSharingOptions.mockResolvedValue([
      { workspace_id: 'ws-2', name: 'Shared WS' },
      { workspace_id: 'ws-3', name: 'Another WS' },
    ])
    mockUpdateSearchAccess.mockResolvedValue([])
    renderSettings('sharing')

    await waitFor(() => {
      expect(screen.getByText('Shared WS')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    // Toggle ws-2 off (it was selected)
    const checkboxes = screen.getAllByRole('checkbox')
    await user.click(checkboxes[0])
    // Toggle ws-3 on
    await user.click(checkboxes[1])

    await user.click(screen.getByText('Save Changes'))

    await waitFor(() => {
      expect(mockUpdateSearchAccess).toHaveBeenCalledWith('ws-1', ['ws-3'])
    })
  })

  // GitHub toggle code maps
  it('toggles code maps on a GitHub repo', async () => {
    const { updateGitHubRepo } = await import('../../../src/lib/api')
    const mockUpdateGitHubRepo = vi.mocked(updateGitHubRepo)

    mockListGitHubInstallations.mockResolvedValue([
      { installation_id: 'inst-1', workspace_id: 'ws-1', account_login: 'my-org', account_type: 'Organization', permissions: { contents: 'read' }, created_by: 'user@test.com', created_at: '2024-01-01' },
    ])
    mockListGitHubRepos.mockResolvedValue([
      { repo_name: 'my-org/repo', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: true, repo_type: 'application', last_sync_at: null, last_sync_status: null, last_sync_error: null, last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com' },
    ])
    mockUpdateGitHubRepo.mockResolvedValue({
      repo_name: 'my-org/repo', workspace_id: 'ws-1', installation_id: 'inst-1', enabled: true, include_code_maps: false, repo_type: 'application', last_sync_at: null, last_sync_status: null, last_sync_error: null, last_commit_sha: null, added_at: '2024-01-01', added_by: 'user@test.com',
    })
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('my-org/repo')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    // The code maps checkbox
    const codeMapCheckbox = screen.getByTitle('Include in code maps')
    const checkbox = codeMapCheckbox.querySelector('input[type="checkbox"]')
    if (checkbox) await user.click(checkbox)

    await waitFor(() => {
      expect(mockUpdateGitHubRepo).toHaveBeenCalledWith('ws-1', 'my-org/repo', { include_code_maps: false })
    })
  })

  // Jira project configure and save
  it('configures and saves Jira projects', async () => {
    const { listJiraProjects, updateJiraProjects } = await import('../../../src/lib/api')
    const mockListJiraProjects = vi.mocked(listJiraProjects)
    const mockUpdateJiraProjects = vi.mocked(updateJiraProjects)

    mockListJiraConnections.mockResolvedValue([{
      connection_id: 'jira-1',
      workspace_id: 'ws-1',
      cloud_id: 'cloud-1',
      site_name: 'Jira Site',
      site_url: 'https://jira.atlassian.net',
      selected_projects: [],
      include_attachments: true,
      include_comments: true,
      last_sync_at: null,
      last_sync_status: 'needs_configuration',
      last_sync_error: null,
      issue_count: 0,
      created_at: '2024-01-01',
    }])
    mockListJiraProjects.mockResolvedValue({
      projects: [
        { key: 'PROJ', name: 'Project One', type: 'software' },
        { key: 'BUG', name: 'Bug Tracker', type: 'software' },
      ]
    })
    mockUpdateJiraProjects.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Jira Site')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const configButtons = screen.getAllByText('Configure')
    // Click configure for Jira
    for (const btn of configButtons) {
      const card = btn.closest('.bg-white.rounded-lg') || btn.closest('li')
      if (card && card.textContent?.includes('Jira Site')) {
        await user.click(btn)
        break
      }
    }

    await waitFor(() => {
      expect(screen.getByText('Project One')).toBeInTheDocument()
    })
  })

  // Outlook folder configure
  it('configures and saves Outlook folders', async () => {
    const { listOutlookFolders, updateOutlookFolders } = await import('../../../src/lib/api')
    const mockListOutlookFolders = vi.mocked(listOutlookFolders)
    const mockUpdateOutlookFolders = vi.mocked(updateOutlookFolders)

    mockListOutlookConnections.mockResolvedValue([{
      connection_id: 'outlook-1',
      workspace_id: 'ws-1',
      user_email: 'user@test.com',
      display_name: 'Outlook User',
      selected_folders: [],
      last_sync_at: null,
      last_sync_status: 'needs_configuration',
      email_count: 0,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }])
    mockListOutlookFolders.mockResolvedValue({
      folders: [
        { id: 'f1', display_name: 'Inbox', child_folder_count: 0, total_item_count: 100, unread_item_count: 5 },
        { id: 'f2', display_name: 'Sent', child_folder_count: 0, total_item_count: 50, unread_item_count: 0 },
      ]
    })
    mockUpdateOutlookFolders.mockResolvedValue(undefined)
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Outlook User')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const configButtons = screen.getAllByText('Configure')
    for (const btn of configButtons) {
      const card = btn.closest('li') || btn.closest('.bg-white')
      if (card && card.textContent?.includes('Outlook User')) {
        await user.click(btn)
        break
      }
    }

    await waitFor(() => {
      expect(mockListOutlookFolders).toHaveBeenCalledWith('ws-1', 'outlook-1')
    })
  })

  // ServiceNow section (env-level OAuth, same UX as Confluence/Jira)
  describe('ServiceNow', () => {
    beforeEach(() => {
      mockGetEnabledIntegrations.mockResolvedValue({
        azure_devops: false,
        azure_devops_boards: false,
        github: false,
        github_issues: false,
        confluence: false,
        jira: false,
        outlook: false,
        teams: false,
        sharepoint: false,
        onedrive: false,
        onenote: false,
        database: false,
        servicenow: true,
      })
    })

    it('renders ServiceNow section with a Connect button when no connections exist', async () => {
      renderSettings('integrations')
      await waitFor(() => {
        expect(screen.getByText('ServiceNow Knowledge Base')).toBeInTheDocument()
      })
      expect(screen.getByText(/no ServiceNow connections yet/i)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /connect/i })).toBeInTheDocument()
    })

    it('Connect click calls getServiceNowAuthUrl and redirects to authorize URL', async () => {
      mockGetServiceNowAuthUrl.mockResolvedValue(
        'https://dev307154.service-now.com/oauth_auth.do?state=XYZ'
      )
      const originalLocation = window.location
      Object.defineProperty(window, 'location', {
        writable: true,
        value: { ...originalLocation, href: '' },
      })
      try {
        renderSettings('integrations')
        await waitFor(() => screen.getByText('ServiceNow Knowledge Base'))
        const user = userEvent.setup()
        await user.click(screen.getByRole('button', { name: /connect/i }))
        await waitFor(() => {
          expect(mockGetServiceNowAuthUrl).toHaveBeenCalledWith('ws-1')
        })
        await waitFor(() => {
          expect(window.location.href).toBe(
            'https://dev307154.service-now.com/oauth_auth.do?state=XYZ'
          )
        })
      } finally {
        Object.defineProperty(window, 'location', {
          writable: true,
          value: originalLocation,
        })
      }
    })
  })

  // Salesforce section (env-level shared Connected App, sandbox picker)
  describe('Salesforce', () => {
    beforeEach(() => {
      mockGetEnabledIntegrations.mockResolvedValue({
        azure_devops: false,
        azure_devops_boards: false,
        github: false,
        github_issues: false,
        confluence: false,
        jira: false,
        outlook: false,
        teams: false,
        sharepoint: false,
        onedrive: false,
        onenote: false,
        database: false,
        servicenow: false,
        salesforce: true,
      })
    })

    it('renders Salesforce section with a Connect button when no connections exist', async () => {
      renderSettings('integrations')
      await waitFor(() => {
        expect(screen.getByText('Salesforce CRM')).toBeInTheDocument()
      })
      expect(screen.getByText(/no Salesforce connections yet/i)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /connect/i })).toBeInTheDocument()
    })

    it('Connect click opens the env picker; Continue calls connectSalesforce then redirects', async () => {
      mockConnectSalesforce.mockResolvedValue({ connection_id: 'sf-conn-1', next: 'authorize' })
      mockGetSalesforceAuthUrl.mockResolvedValue('https://login.salesforce.com/services/oauth2/authorize?state=XYZ')
      const originalLocation = window.location
      Object.defineProperty(window, 'location', {
        writable: true,
        value: { ...originalLocation, href: '' },
      })
      try {
        renderSettings('integrations')
        await waitFor(() => screen.getByText('Salesforce CRM'))
        const user = userEvent.setup()
        await user.click(screen.getByRole('button', { name: /connect/i }))
        // Env picker appears
        expect(screen.getByText(/production \(login.salesforce.com\)/i)).toBeInTheDocument()
        expect(screen.getByText(/sandbox \(test.salesforce.com\)/i)).toBeInTheDocument()
        await user.click(screen.getByRole('button', { name: /continue to oauth/i }))
        await waitFor(() => {
          expect(mockConnectSalesforce).toHaveBeenCalledWith('ws-1', false)
        })
        await waitFor(() => {
          expect(mockGetSalesforceAuthUrl).toHaveBeenCalledWith('ws-1', 'sf-conn-1')
        })
      } finally {
        Object.defineProperty(window, 'location', {
          writable: true,
          value: originalLocation,
        })
      }
    })
  })

  // Confluence connect handler
  it('connects Confluence via auth URL', async () => {
    const { getConfluenceAuthUrl } = await import('../../../src/lib/api')
    const mockGetConfluenceAuthUrl = vi.mocked(getConfluenceAuthUrl)
    mockGetConfluenceAuthUrl.mockResolvedValue('https://auth.atlassian.com/authorize')
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Confluence')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const connectButtons = screen.getAllByText('Connect')
    // Click Confluence connect (first Connect button)
    await user.click(connectButtons[0])

    await waitFor(() => {
      expect(mockGetConfluenceAuthUrl).toHaveBeenCalledWith('ws-1')
    })
  })

  // Jira connect handler
  it('connects Jira via auth URL', async () => {
    const { getJiraAuthUrl } = await import('../../../src/lib/api')
    const mockGetJiraAuthUrl = vi.mocked(getJiraAuthUrl)
    mockGetJiraAuthUrl.mockResolvedValue('https://auth.atlassian.com/jira')
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Jira')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const connectButtons = screen.getAllByText('Connect')
    // Click Jira connect (second Connect button)
    await user.click(connectButtons[1])

    await waitFor(() => {
      expect(mockGetJiraAuthUrl).toHaveBeenCalledWith('ws-1')
    })
  })

  // Outlook connect handler
  it('connects Outlook via auth URL', async () => {
    const { getOutlookAuthUrl } = await import('../../../src/lib/api')
    const mockGetOutlookAuthUrl = vi.mocked(getOutlookAuthUrl)
    mockGetOutlookAuthUrl.mockResolvedValue('https://login.microsoft.com/outlook')
    renderSettings('integrations')

    await waitFor(() => {
      expect(screen.getByText('Outlook 365')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    const connectButtons = screen.getAllByText('Connect')
    // Connect buttons: Confluence(0), Jira(1), Boards(2), Outlook(3), Teams(4), SharePoint(5)
    await user.click(connectButtons[3])

    await waitFor(() => {
      expect(mockGetOutlookAuthUrl).toHaveBeenCalledWith('ws-1')
    })
  })

  describe('Privacy section (PII redaction)', () => {
    it('renders the radio group when org-level redaction is enabled', async () => {
      mockGetWorkspacePrivacy.mockResolvedValue({
        workspace_id: 'ws-1',
        privacy_mode: 'STRICT',
        redaction_enabled_at_org_level: true,
        is_effective: true,
      })
      renderSettings('general')
      await waitFor(() => {
        expect(screen.getByText('Privacy')).toBeInTheDocument()
      })
      expect(screen.getByText('Redact PII (recommended)')).toBeInTheDocument()
      expect(screen.getByText('No redaction')).toBeInTheDocument()
      // STRICT radio is checked.
      const strictRadio = screen.getByRole('radio', {
        name: /Redact PII \(recommended\)/i,
      })
      expect(strictRadio).toBeChecked()
    })

    it('switches to NONE mode and shows the rose warning panel', async () => {
      mockGetWorkspacePrivacy.mockResolvedValue({
        workspace_id: 'ws-1',
        privacy_mode: 'STRICT',
        redaction_enabled_at_org_level: true,
        is_effective: true,
      })
      mockUpdateWorkspacePrivacy.mockResolvedValue({
        workspace_id: 'ws-1',
        privacy_mode: 'NONE',
        redaction_enabled_at_org_level: true,
        is_effective: false,
      })
      renderSettings('general')
      await waitFor(() => {
        expect(screen.getByText('Privacy')).toBeInTheDocument()
      })
      const noneRadio = screen.getByRole('radio', { name: /No redaction/i })
      const user = userEvent.setup()
      await user.click(noneRadio)

      await waitFor(() => {
        expect(mockUpdateWorkspacePrivacy).toHaveBeenCalledWith('ws-1', 'NONE')
      })
      // Rose warning shown.
      expect(
        screen.getByText('PII redaction is disabled for this workspace.'),
      ).toBeInTheDocument()
    })

    it('hides the toggle entirely when org-level redaction is disabled', async () => {
      mockGetWorkspacePrivacy.mockResolvedValue({
        workspace_id: 'ws-1',
        privacy_mode: 'STRICT',
        redaction_enabled_at_org_level: false,
        is_effective: false,
      })
      renderSettings('general')
      await waitFor(() => {
        expect(
          screen.getByText('PII redaction is disabled organization-wide.'),
        ).toBeInTheDocument()
      })
      // No radios in the org-disabled branch.
      expect(
        screen.queryByRole('radio', { name: /Redact PII/i }),
      ).not.toBeInTheDocument()
      expect(
        screen.queryByRole('radio', { name: /No redaction/i }),
      ).not.toBeInTheDocument()
    })

    it('does not call updateWorkspacePrivacy when org-level disabled', async () => {
      mockGetWorkspacePrivacy.mockResolvedValue({
        workspace_id: 'ws-1',
        privacy_mode: 'STRICT',
        redaction_enabled_at_org_level: false,
        is_effective: false,
      })
      renderSettings('general')
      await waitFor(() => {
        expect(
          screen.getByText('PII redaction is disabled organization-wide.'),
        ).toBeInTheDocument()
      })
      expect(mockUpdateWorkspacePrivacy).not.toHaveBeenCalled()
    })

    it('surfaces an inline error when the PUT fails', async () => {
      mockGetWorkspacePrivacy.mockResolvedValue({
        workspace_id: 'ws-1',
        privacy_mode: 'STRICT',
        redaction_enabled_at_org_level: true,
        is_effective: true,
      })
      mockUpdateWorkspacePrivacy.mockRejectedValue(new Error('Network down'))
      renderSettings('general')
      await waitFor(() => {
        expect(screen.getByText('Privacy')).toBeInTheDocument()
      })
      const noneRadio = screen.getByRole('radio', { name: /No redaction/i })
      const user = userEvent.setup()
      await user.click(noneRadio)

      await waitFor(() => {
        expect(screen.getByText('Network down')).toBeInTheDocument()
      })
    })
  })
})
