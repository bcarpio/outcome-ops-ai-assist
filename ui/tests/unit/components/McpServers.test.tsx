import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import McpServers from '../../../src/pages/McpServers'
import { renderWithRouter } from '../../helpers/render'
import { createMcpServer, createWorkspace } from '../../helpers/mocks'

vi.mock('../../../src/lib/api', () => ({
  listMcpServers: vi.fn(),
  createMcpServer: vi.fn(),
  updateMcpServer: vi.fn(),
  deleteMcpServer: vi.fn(),
  refreshMcpTools: vi.fn(),
  listWorkspaces: vi.fn(),
  getMcpServerWorkspaces: vi.fn(),
  assignMcpToWorkspace: vi.fn(),
  removeMcpFromWorkspace: vi.fn(),
}))

import {
  listMcpServers,
  createMcpServer as apiCreateMcpServer,
  updateMcpServer,
  deleteMcpServer,
  refreshMcpTools,
  listWorkspaces,
  getMcpServerWorkspaces,
  assignMcpToWorkspace,
  removeMcpFromWorkspace,
} from '../../../src/lib/api'

const mockListMcpServers = vi.mocked(listMcpServers)
const mockCreateMcpServer = vi.mocked(apiCreateMcpServer)
const mockUpdateMcpServer = vi.mocked(updateMcpServer)
const mockDeleteMcpServer = vi.mocked(deleteMcpServer)
const mockRefreshMcpTools = vi.mocked(refreshMcpTools)
const mockListWorkspaces = vi.mocked(listWorkspaces)
const mockGetMcpServerWorkspaces = vi.mocked(getMcpServerWorkspaces)
const mockAssignMcpToWorkspace = vi.mocked(assignMcpToWorkspace)
const mockRemoveMcpFromWorkspace = vi.mocked(removeMcpFromWorkspace)

beforeEach(() => {
  vi.clearAllMocks()
  mockListMcpServers.mockResolvedValue([])
  mockListWorkspaces.mockResolvedValue([])
  mockGetMcpServerWorkspaces.mockResolvedValue([])
})

function renderMcpServers() {
  return renderWithRouter(<McpServers />, {
    initialEntries: ['/org/mcp-servers'],
    path: '/org/mcp-servers',
  })
}

describe('McpServers', () => {
  it('renders empty state', async () => {
    renderMcpServers()

    await waitFor(() => {
      expect(screen.getByText('No MCP servers configured')).toBeInTheDocument()
    })
  })

  it('renders server list', async () => {
    const servers = [
      createMcpServer({ mcp_id: 'mcp-1', name: 'SonarQube', is_global: true, tool_count: 18 }),
      createMcpServer({ mcp_id: 'mcp-2', name: 'Custom MCP', is_global: false, tool_count: 3 }),
    ]
    mockListMcpServers.mockResolvedValue(servers)

    renderMcpServers()

    await waitFor(() => {
      expect(screen.getByText('SonarQube')).toBeInTheDocument()
    })
    expect(screen.getByText('Custom MCP')).toBeInTheDocument()
    expect(screen.getByText('MCP Servers (2)')).toBeInTheDocument()
  })

  it('shows global badge for global servers', async () => {
    mockListMcpServers.mockResolvedValue([
      createMcpServer({ is_global: true }),
    ])

    renderMcpServers()

    await waitFor(() => {
      expect(screen.getByText('Global')).toBeInTheDocument()
    })
  })

  it('shows active/error status badges', async () => {
    mockListMcpServers.mockResolvedValue([
      createMcpServer({ mcp_id: 'mcp-1', name: 'Active Server', status: 'active' }),
      createMcpServer({ mcp_id: 'mcp-2', name: 'Error Server', status: 'error' }),
    ])

    renderMcpServers()

    await waitFor(() => {
      expect(screen.getByText('Active')).toBeInTheDocument()
    })
    expect(screen.getByText('Error')).toBeInTheDocument()
  })

  it('opens create modal', async () => {
    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('Add MCP Server')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Add MCP Server'))

    expect(screen.getByText('Add MCP Server')).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toBeInTheDocument()
    expect(screen.getByLabelText('URL')).toBeInTheDocument()
  })

  it('creates a new server through the modal', async () => {
    const newServer = createMcpServer({ mcp_id: 'mcp-new', name: 'New Server' })
    mockCreateMcpServer.mockResolvedValue(newServer)

    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('No MCP servers configured')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Add MCP Server'))
    await user.type(screen.getByLabelText('Name'), 'New Server')
    await user.type(screen.getByLabelText('URL'), 'https://mcp.example.com')

    const addBtn = screen.getAllByText('Add Server').find(el => el.tagName === 'BUTTON')
    await user.click(addBtn!)

    await waitFor(() => {
      expect(mockCreateMcpServer).toHaveBeenCalledWith(
        expect.objectContaining({ name: 'New Server', url: 'https://mcp.example.com' })
      )
    })
  })

  it('opens edit modal with server data', async () => {
    const server = createMcpServer({ name: 'SonarQube', url: 'https://mcp.example.com/mcp', is_global: true })
    mockListMcpServers.mockResolvedValue([server])

    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('SonarQube')).toBeInTheDocument()
    })

    // Click the gear (settings) button
    const settingsBtn = screen.getByTitle('Configure server')
    await user.click(settingsBtn)

    expect(screen.getByText('Configure MCP Server')).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveValue('SonarQube')
    expect(screen.getByLabelText('URL')).toHaveValue('https://mcp.example.com/mcp')
  })

  it('shows workspace assignments when not global in edit modal', async () => {
    const server = createMcpServer({ name: 'Custom MCP', is_global: false })
    const ws = createWorkspace({ workspace_id: 'ws-1', name: 'Engineering' })
    mockListMcpServers.mockResolvedValue([server])
    mockListWorkspaces.mockResolvedValue([ws])
    mockGetMcpServerWorkspaces.mockResolvedValue(['ws-1'])

    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('Custom MCP')).toBeInTheDocument()
    })

    await user.click(screen.getByTitle('Configure server'))

    await waitFor(() => {
      expect(screen.getByText('Engineering')).toBeInTheDocument()
    })
    expect(mockGetMcpServerWorkspaces).toHaveBeenCalledWith('mcp-1')
  })

  it('refreshes server tools', async () => {
    const server = createMcpServer({ mcp_id: 'mcp-1', name: 'SonarQube', tool_count: 18 })
    const updated = { ...server, tool_count: 20 }
    mockListMcpServers.mockResolvedValue([server])
    mockRefreshMcpTools.mockResolvedValue(updated)

    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('SonarQube')).toBeInTheDocument()
    })

    await user.click(screen.getByTitle('Refresh tools'))

    await waitFor(() => {
      expect(mockRefreshMcpTools).toHaveBeenCalledWith('mcp-1')
    })
  })

  it('deletes a server with confirmation', async () => {
    const server = createMcpServer({ mcp_id: 'mcp-1', name: 'Old Server' })
    mockListMcpServers.mockResolvedValue([server])
    mockDeleteMcpServer.mockResolvedValue()
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('Old Server')).toBeInTheDocument()
    })

    await user.click(screen.getByTitle('Delete server'))

    await waitFor(() => {
      expect(mockDeleteMcpServer).toHaveBeenCalledWith('mcp-1')
    })
  })

  it('shows info banner about MCP servers', async () => {
    renderMcpServers()

    await waitFor(() => {
      expect(screen.getByText('About MCP Servers')).toBeInTheDocument()
    })
  })
})
