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

  it('shows tool count for servers', async () => {
    mockListMcpServers.mockResolvedValue([
      createMcpServer({ mcp_id: 'mcp-1', name: 'SonarQube', tool_count: 18 }),
    ])

    renderMcpServers()

    await waitFor(() => {
      expect(screen.getByText(/18 tools/)).toBeInTheDocument()
    })
  })

  it('shows singular tool text for 1 tool', async () => {
    mockListMcpServers.mockResolvedValue([
      createMcpServer({ mcp_id: 'mcp-1', name: 'Simple MCP', tool_count: 1 }),
    ])

    renderMcpServers()

    await waitFor(() => {
      expect(screen.getByText(/1 tool --/)).toBeInTheDocument()
    })
  })

  it('shows truncated URL for long URLs', async () => {
    mockListMcpServers.mockResolvedValue([
      createMcpServer({ mcp_id: 'mcp-1', name: 'Long URL', url: 'https://very-long-mcp-server-hostname.example.com/api/v1/mcp-endpoint' }),
    ])

    renderMcpServers()

    await waitFor(() => {
      expect(screen.getByText('Long URL')).toBeInTheDocument()
    })
    // URL should be truncated (40 chars + ...)
    expect(screen.getByText(/\.\.\.$/)).toBeInTheDocument()
  })

  it('shows last refreshed date', async () => {
    mockListMcpServers.mockResolvedValue([
      createMcpServer({ mcp_id: 'mcp-1', last_refreshed_at: null }),
    ])

    renderMcpServers()

    await waitFor(() => {
      expect(screen.getByText(/Never/)).toBeInTheDocument()
    })
  })

  it('cancels delete when confirm is declined', async () => {
    const server = createMcpServer({ mcp_id: 'mcp-1', name: 'Keep Me' })
    mockListMcpServers.mockResolvedValue([server])
    vi.spyOn(window, 'confirm').mockReturnValue(false)

    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('Keep Me')).toBeInTheDocument()
    })

    await user.click(screen.getByTitle('Delete server'))

    expect(mockDeleteMcpServer).not.toHaveBeenCalled()
  })

  it('handles load data error', async () => {
    mockListMcpServers.mockRejectedValue(new Error('Server error'))

    renderMcpServers()

    await waitFor(() => {
      expect(screen.getByText('Server error')).toBeInTheDocument()
    })
  })

  it('dismisses error message', async () => {
    mockListMcpServers.mockRejectedValue(new Error('Server error'))

    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('Server error')).toBeInTheDocument()
    })

    // Click the dismiss button (X) on the error
    const errorContainer = screen.getByText('Server error').closest('div')!
    const dismissBtn = errorContainer.querySelector('button')!
    await user.click(dismissBtn)

    await waitFor(() => {
      expect(screen.queryByText('Server error')).not.toBeInTheDocument()
    })
  })

  it('closes the create modal via Cancel', async () => {
    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('Add MCP Server')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Add MCP Server'))
    expect(screen.getByLabelText('Name')).toBeInTheDocument()

    await user.click(screen.getByText('Cancel'))

    await waitFor(() => {
      expect(screen.queryByLabelText('Name')).not.toBeInTheDocument()
    })
  })

  it('handles save error in create modal', async () => {
    mockCreateMcpServer.mockRejectedValue(new Error('Duplicate name'))

    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('No MCP servers configured')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Add MCP Server'))
    await user.type(screen.getByLabelText('Name'), 'Dup Server')
    await user.type(screen.getByLabelText('URL'), 'https://mcp.example.com')

    const addBtn = screen.getAllByText('Add Server').find(el => el.tagName === 'BUTTON')
    await user.click(addBtn!)

    await waitFor(() => {
      expect(screen.getByText('Duplicate name')).toBeInTheDocument()
    })
  })

  it('updates server through edit modal', async () => {
    const server = createMcpServer({ name: 'Old Name', url: 'https://old.com', is_global: true })
    const updated = { ...server, name: 'New Name' }
    mockListMcpServers.mockResolvedValue([server])
    mockUpdateMcpServer.mockResolvedValue(updated)

    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('Old Name')).toBeInTheDocument()
    })

    await user.click(screen.getByTitle('Configure server'))

    await waitFor(() => {
      expect(screen.getByText('Configure MCP Server')).toBeInTheDocument()
    })

    const nameInput = screen.getByLabelText('Name')
    await user.clear(nameInput)
    await user.type(nameInput, 'New Name')

    await user.click(screen.getByText('Save Changes'))

    await waitFor(() => {
      expect(mockUpdateMcpServer).toHaveBeenCalledWith(
        'mcp-1',
        expect.objectContaining({ name: 'New Name' })
      )
    })
  })

  it('shows auth type selector and credentials field', async () => {
    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('No MCP servers configured')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Add MCP Server'))

    // Auth type should default to None
    expect(screen.getByLabelText('Auth Type')).toHaveValue('none')

    // Change to API Key
    await user.selectOptions(screen.getByLabelText('Auth Type'), 'api_key')

    // Now credentials field should appear
    await waitFor(() => {
      expect(screen.getByLabelText('API Key')).toBeInTheDocument()
    })

    // Change to Bearer
    await user.selectOptions(screen.getByLabelText('Auth Type'), 'bearer')

    await waitFor(() => {
      expect(screen.getByLabelText('Bearer Token')).toBeInTheDocument()
    })
  })

  it('toggles workspace assignment in edit modal', async () => {
    const server = createMcpServer({ name: 'Custom MCP', is_global: false })
    const ws1 = createWorkspace({ workspace_id: 'ws-1', name: 'Engineering' })
    const ws2 = createWorkspace({ workspace_id: 'ws-2', name: 'Design' })
    mockListMcpServers.mockResolvedValue([server])
    mockListWorkspaces.mockResolvedValue([ws1, ws2])
    mockGetMcpServerWorkspaces.mockResolvedValue(['ws-1'])
    mockAssignMcpToWorkspace.mockResolvedValue()
    mockRemoveMcpFromWorkspace.mockResolvedValue()

    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('Custom MCP')).toBeInTheDocument()
    })

    await user.click(screen.getByTitle('Configure server'))

    await waitFor(() => {
      expect(screen.getByText('Engineering')).toBeInTheDocument()
      expect(screen.getByText('Design')).toBeInTheDocument()
    })

    // Click the Design checkbox to assign it
    const designLabel = screen.getByText('Design').closest('label')!
    const checkbox = designLabel.querySelector('input[type="checkbox"]')!
    await user.click(checkbox)

    await waitFor(() => {
      expect(mockAssignMcpToWorkspace).toHaveBeenCalledWith('ws-2', 'mcp-1')
    })
  })

  it('handles refresh error', async () => {
    const server = createMcpServer({ mcp_id: 'mcp-1', name: 'SonarQube' })
    mockListMcpServers.mockResolvedValue([server])
    mockRefreshMcpTools.mockRejectedValue(new Error('Timeout'))

    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('SonarQube')).toBeInTheDocument()
    })

    await user.click(screen.getByTitle('Refresh tools'))

    await waitFor(() => {
      expect(screen.getByText('Timeout')).toBeInTheDocument()
    })
  })

  it('handles delete error', async () => {
    const server = createMcpServer({ mcp_id: 'mcp-1', name: 'Protected Server' })
    mockListMcpServers.mockResolvedValue([server])
    mockDeleteMcpServer.mockRejectedValue(new Error('Cannot delete'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('Protected Server')).toBeInTheDocument()
    })

    await user.click(screen.getByTitle('Delete server'))

    await waitFor(() => {
      expect(screen.getByText('Cannot delete')).toBeInTheDocument()
    })
  })

  it('shows "Save first" message for new non-global servers', async () => {
    renderMcpServers()
    const user = userEvent.setup()

    await waitFor(() => {
      expect(screen.getByText('No MCP servers configured')).toBeInTheDocument()
    })

    await user.click(screen.getByText('Add MCP Server'))

    // The form defaults to is_global: false, so the "save first" message should show immediately
    await waitFor(() => {
      expect(screen.getByText('Save the server first, then configure workspace assignments.')).toBeInTheDocument()
    })

    // Checking the global checkbox should hide the message
    const globalCheckbox = screen.getByLabelText(/Available to all workspaces/)
    await user.click(globalCheckbox)

    await waitFor(() => {
      expect(screen.queryByText('Save the server first, then configure workspace assignments.')).not.toBeInTheDocument()
    })
  })
})
