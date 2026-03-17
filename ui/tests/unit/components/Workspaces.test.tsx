import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Workspaces from '../../../src/pages/Workspaces'
import { renderWithRouter } from '../../helpers/render'
import { createWorkspace } from '../../helpers/mocks'

vi.mock('../../../src/lib/api', () => ({
  listWorkspaces: vi.fn(),
  createWorkspace: vi.fn(),
}))

import { listWorkspaces, createWorkspace as createWorkspaceApi } from '../../../src/lib/api'

const mockListWorkspaces = vi.mocked(listWorkspaces)
const mockCreateWorkspace = vi.mocked(createWorkspaceApi)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Workspaces', () => {
  it('renders workspace grid', async () => {
    const workspaces = [
      createWorkspace({ workspace_id: 'ws-1', name: 'Frontend', description: 'Frontend services' }),
      createWorkspace({ workspace_id: 'ws-2', name: 'Backend', description: 'Backend services' }),
    ]
    mockListWorkspaces.mockResolvedValue(workspaces)

    renderWithRouter(<Workspaces />)

    await waitFor(() => {
      expect(screen.getByText('Frontend')).toBeInTheDocument()
    })
    expect(screen.getByText('Backend')).toBeInTheDocument()
    expect(screen.getByText('Frontend services')).toBeInTheDocument()
    expect(screen.getByText('Backend services')).toBeInTheDocument()
  })

  it('shows empty state when no workspaces (non-admin)', async () => {
    mockListWorkspaces.mockResolvedValue([])

    renderWithRouter(<Workspaces />)

    await waitFor(() => {
      expect(screen.getByText('No workspaces yet')).toBeInTheDocument()
    })
    expect(screen.getByText('Ask an org admin to add you to a workspace')).toBeInTheDocument()
    expect(screen.queryByText('Create Your First Workspace')).not.toBeInTheDocument()
  })

  it('shows loading state', () => {
    mockListWorkspaces.mockReturnValue(new Promise(() => {})) // never resolves

    renderWithRouter(<Workspaces />)

    expect(screen.getByText('Workspaces')).toBeInTheDocument()
  })

  it('hides create button for non-org-admins', async () => {
    mockListWorkspaces.mockResolvedValue([
      createWorkspace({ workspace_id: 'ws-1', name: 'Test', role: 'member' }),
    ])

    renderWithRouter(<Workspaces />)
    await waitFor(() => expect(screen.getByText('Test')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /new workspace/i })).not.toBeInTheDocument()
  })

  it('opens create modal', async () => {
    mockListWorkspaces.mockResolvedValue([
      createWorkspace({ role: 'org_admin' }),
    ])

    renderWithRouter(<Workspaces />)
    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /new workspace/i }))

    expect(screen.getByText('Create Workspace')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('My Workspace')).toBeInTheDocument()
  })

  it('creates a workspace through the modal', async () => {
    mockListWorkspaces.mockResolvedValue([
      createWorkspace({ role: 'org_admin' }),
    ])
    const newWs = createWorkspace({ workspace_id: 'ws-new', name: 'My WS' })
    mockCreateWorkspace.mockResolvedValue(newWs)

    renderWithRouter(<Workspaces />)
    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /new workspace/i }))

    await user.type(screen.getByPlaceholderText('My Workspace'), 'My WS')
    await user.type(
      screen.getByPlaceholderText('A brief description of this workspace'),
      'Description'
    )
    await user.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() => {
      expect(mockCreateWorkspace).toHaveBeenCalledWith('My WS', 'Description')
    })
  })

  it('closes create modal on cancel', async () => {
    mockListWorkspaces.mockResolvedValue([
      createWorkspace({ role: 'org_admin' }),
    ])

    renderWithRouter(<Workspaces />)
    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /new workspace/i }))
    expect(screen.getByText('Create Workspace')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /cancel/i }))
    expect(screen.queryByText('Create Workspace')).not.toBeInTheDocument()
  })

  it('displays workspace roles', async () => {
    const workspaces = [
      createWorkspace({ workspace_id: 'ws-1', name: 'Test', role: 'workspace_admin' }),
    ]
    mockListWorkspaces.mockResolvedValue(workspaces)

    renderWithRouter(<Workspaces />)
    await waitFor(() => expect(screen.getByText('Test')).toBeInTheDocument())
    expect(screen.getByText('workspace admin')).toBeInTheDocument()
  })

  it('shows create error', async () => {
    mockListWorkspaces.mockResolvedValue([
      createWorkspace({ role: 'org_admin' }),
    ])
    mockCreateWorkspace.mockRejectedValue(new Error('Duplicate name'))

    renderWithRouter(<Workspaces />)
    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /new workspace/i }))
    await user.type(screen.getByPlaceholderText('My Workspace'), 'Dup')
    await user.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() => {
      expect(screen.getByText('Duplicate name')).toBeInTheDocument()
    })
  })
})
