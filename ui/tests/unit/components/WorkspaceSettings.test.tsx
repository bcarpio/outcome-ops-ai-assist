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
}))

import { getWorkspace, listMembers, listConfluenceConnections, listJiraConnections, getSearchAccess, listSharingOptions, listDocuments, listOutlookConnections, listGitHubInstallations, listGitHubRepos } from '../../../src/lib/api'

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
})
