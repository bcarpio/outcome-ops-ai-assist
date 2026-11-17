import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Workspaces from '../../../src/pages/Workspaces'
import { renderWithRouter } from '../../helpers/render'
import { createWorkspace } from '../../helpers/mocks'

vi.mock('../../../src/lib/api', () => ({
  listWorkspaces: vi.fn(),
  createWorkspace: vi.fn(),
  listOrgMembers: vi.fn(),
  getCurrentUser: vi.fn(),
}))

import {
  listWorkspaces,
  createWorkspace as createWorkspaceApi,
  listOrgMembers,
  getCurrentUser,
} from '../../../src/lib/api'

const mockListWorkspaces = vi.mocked(listWorkspaces)
const mockCreateWorkspace = vi.mocked(createWorkspaceApi)
const mockListOrgMembers = vi.mocked(listOrgMembers)
const mockGetCurrentUser = vi.mocked(getCurrentUser)

/** Mark the test user as an org admin via the new isOrgAdmin source
 * of truth (listOrgMembers + getCurrentUser matching by email). */
function asOrgAdmin(email = 'admin@example.com') {
  mockGetCurrentUser.mockResolvedValue({ email })
  mockListOrgMembers.mockResolvedValue([
    {
      user_email: email,
      org_role: 'org_admin',
      joined_at: '2026-06-03T00:00:00Z',
    },
  ])
}

/** Mark the test user as a non-admin: listOrgMembers 403s (the
 * server-side guard), simulated as a rejected promise. */
function asNonAdmin(email = 'member@example.com') {
  mockGetCurrentUser.mockResolvedValue({ email })
  mockListOrgMembers.mockRejectedValue(new Error('403'))
}

beforeEach(() => {
  vi.clearAllMocks()
  // Default: non-admin. Individual tests override via asOrgAdmin().
  asNonAdmin()
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
    asNonAdmin()
    mockListWorkspaces.mockResolvedValue([])

    renderWithRouter(<Workspaces />)

    await waitFor(() => {
      expect(screen.getByText('No workspaces yet')).toBeInTheDocument()
    })
    expect(screen.getByText('Ask an org admin to add you to a workspace')).toBeInTheDocument()
    expect(screen.queryByText('Create Your First Workspace')).not.toBeInTheDocument()
  })

  it('shows create CTA when org_admin has zero workspaces (regression: 2026-06-04)', async () => {
    // Org admin on a freshly-bootstrapped deployment has zero
    // workspaces yet. The empty state used to derive isOrgAdmin from
    // `workspaces.some(w => w.role === 'org_admin')` -- empty array
    // ->  false -> the empty state routed the org admin into the
    // "Ask an org admin to add you to a workspace" copy (telling
    // them to ask themselves). Surfaced on the openai workspace's
    // first login. Fix: isOrgAdmin now derives from listOrgMembers +
    // getCurrentUser, independent of the workspace list.
    asOrgAdmin('admin@example.com')
    mockListWorkspaces.mockResolvedValue([])

    renderWithRouter(<Workspaces />)

    await waitFor(() => {
      expect(screen.getByText('No workspaces yet')).toBeInTheDocument()
    })
    expect(screen.getByText('Create Your First Workspace')).toBeInTheDocument()
    expect(
      screen.queryByText('Ask an org admin to add you to a workspace')
    ).not.toBeInTheDocument()
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
    asOrgAdmin()
    mockListWorkspaces.mockResolvedValue([
      createWorkspace({ role: 'member' }),
    ])

    renderWithRouter(<Workspaces />)
    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /new workspace/i }))

    expect(screen.getByText('Create Workspace')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('My Workspace')).toBeInTheDocument()
  })

  it('creates a workspace through the modal', async () => {
    asOrgAdmin()
    mockListWorkspaces.mockResolvedValue([
      createWorkspace({ role: 'member' }),
    ])
    const newWs = createWorkspace({ workspace_id: 'ws-new', name: 'My WS' })
    mockCreateWorkspace.mockResolvedValue(newWs)

    renderWithRouter(<Workspaces />)
    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /new workspace/i }))

    await user.type(screen.getByPlaceholderText('My Workspace'), 'My WS')
    await user.type(
      screen.getByPlaceholderText('A brief description of this workspace'),
      'Description'
    )
    await user.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() => {
      expect(mockCreateWorkspace).toHaveBeenCalledWith(
        'My WS', 'Description', { is_global: false },
      )
    })
  })

  it('shows a Global badge on workspaces flagged is_global', async () => {
    // of the workspace-scope plan. Global workspaces (Security,
    // Enterprise Architecture, etc.) are visually distinguishable in the
    // list so admins can scan the deployment's read-scope at a glance.
    mockListWorkspaces.mockResolvedValue([
      createWorkspace({ workspace_id: 'ws-1', name: 'Team Eng' }),
      createWorkspace({
        workspace_id: 'ws-2', name: 'Security Standards',
        is_global: true, role: 'global_reader',
      }),
    ])
    renderWithRouter(<Workspaces />)
    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalled())

    // Both workspaces render, but only the Security one has a Global badge.
    expect(screen.getByText('Team Eng')).toBeInTheDocument()
    expect(screen.getByText('Security Standards')).toBeInTheDocument()
    const badges = screen.getAllByText('Global')
    expect(badges).toHaveLength(1)
  })

  it('requires a two-step confirm when creating a Global workspace', async () => {
    // Ticking is_global at create time triggers the provisioning
    // warning modal so admins see the bidirectional-read consequence
    // before submitting.
    asOrgAdmin()
    mockListWorkspaces.mockResolvedValue([])
    const newWs = createWorkspace({
      workspace_id: 'ws-sec', name: 'Sec', is_global: true,
    })
    mockCreateWorkspace.mockResolvedValue(newWs)
    renderWithRouter(<Workspaces />)
    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalled())

    const user = userEvent.setup()
    // Open the empty-state CTA (Create Your First Workspace).
    await user.click(screen.getByRole('button', { name: /create your first workspace/i }))
    await user.type(screen.getByPlaceholderText('My Workspace'), 'Sec')
    // Tick is_global.
    await user.click(screen.getByRole('checkbox', { name: /global workspace/i }))
    // Clicking Create should NOT fire the API yet -- it should show
    // the warning modal first.
    await user.click(screen.getByRole('button', { name: /^create$/i }))
    expect(mockCreateWorkspace).not.toHaveBeenCalled()
    // Warning modal is visible.
    expect(screen.getByText(/confirm global workspace/i)).toBeInTheDocument()
    // Avoid apostrophe encoding differences (rsquo vs straight quote).
    expect(screen.getByText(/every user/i)).toBeInTheDocument()
    // Confirm -> API fires with is_global=true.
    await user.click(screen.getByRole('button', { name: /confirm — create as global/i }))
    await waitFor(() => {
      expect(mockCreateWorkspace).toHaveBeenCalledWith(
        'Sec', '', { is_global: true },
      )
    })
  })

  it('closes create modal on cancel', async () => {
    asOrgAdmin()
    mockListWorkspaces.mockResolvedValue([
      createWorkspace({ role: 'member' }),
    ])

    renderWithRouter(<Workspaces />)
    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /new workspace/i }))
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
    asOrgAdmin()
    mockListWorkspaces.mockResolvedValue([
      createWorkspace({ role: 'member' }),
    ])
    mockCreateWorkspace.mockRejectedValue(new Error('Duplicate name'))

    renderWithRouter(<Workspaces />)
    await waitFor(() => expect(mockListWorkspaces).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /new workspace/i }))
    await user.type(screen.getByPlaceholderText('My Workspace'), 'Dup')
    await user.click(screen.getByRole('button', { name: /^create$/i }))

    await waitFor(() => {
      expect(screen.getByText('Duplicate name')).toBeInTheDocument()
    })
  })
})
