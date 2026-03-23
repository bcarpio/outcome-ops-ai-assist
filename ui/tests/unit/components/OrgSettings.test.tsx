import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import OrgSettings from '../../../src/pages/OrgSettings'
import { renderWithRouter } from '../../helpers/render'
import { createOrgMember } from '../../helpers/mocks'

vi.mock('../../../src/lib/api', () => ({
  listOrgMembers: vi.fn(),
  addOrgMember: vi.fn(),
  updateOrgMember: vi.fn(),
  removeOrgMember: vi.fn(),
}))

import { listOrgMembers, addOrgMember, updateOrgMember, removeOrgMember } from '../../../src/lib/api'

const mockListOrgMembers = vi.mocked(listOrgMembers)
const mockAddOrgMember = vi.mocked(addOrgMember)
const mockUpdateOrgMember = vi.mocked(updateOrgMember)
const mockRemoveOrgMember = vi.mocked(removeOrgMember)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('OrgSettings', () => {
  it('renders loading state then member list', async () => {
    const members = [
      createOrgMember({ user_email: 'admin@example.com', org_role: 'org_admin' }),
      createOrgMember({ user_email: 'user@example.com', org_role: 'member' }),
    ]
    mockListOrgMembers.mockResolvedValue(members)

    renderWithRouter(<OrgSettings />)

    // Title renders immediately
    expect(screen.getByText('Organization Settings')).toBeInTheDocument()

    // Members load
    await waitFor(() => {
      expect(screen.getByText('admin@example.com')).toBeInTheDocument()
    })
    expect(screen.getByText('user@example.com')).toBeInTheDocument()
    expect(screen.getByText('Organization Members (2)')).toBeInTheDocument()
  })

  it('filters members by search', async () => {
    const members = [
      createOrgMember({ user_email: 'alice@example.com', org_role: 'org_admin' }),
      createOrgMember({ user_email: 'bob@example.com', org_role: 'member' }),
    ]
    mockListOrgMembers.mockResolvedValue(members)

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())

    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText('Search by email...'), 'alice')

    expect(screen.getByText('alice@example.com')).toBeInTheDocument()
    expect(screen.queryByText('bob@example.com')).not.toBeInTheDocument()
  })

  it('shows empty state when no members match search', async () => {
    mockListOrgMembers.mockResolvedValue([
      createOrgMember({ user_email: 'alice@example.com' }),
    ])

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('alice@example.com')).toBeInTheDocument())

    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText('Search by email...'), 'zzz')

    expect(screen.getByText('No matching members')).toBeInTheDocument()
  })

  it('shows add member modal and adds a member', async () => {
    mockListOrgMembers.mockResolvedValue([])
    const newMember = createOrgMember({ user_email: 'new@example.com', org_role: 'member' })
    mockAddOrgMember.mockResolvedValue(newMember)

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(mockListOrgMembers).toHaveBeenCalled())

    const user = userEvent.setup()
    // Click "Add Member" button in header
    await user.click(screen.getByRole('button', { name: /add member/i }))

    // Modal should appear
    expect(screen.getByText('Add Organization Member')).toBeInTheDocument()

    // Fill in email
    await user.type(screen.getByPlaceholderText('user@example.com'), 'new@example.com')

    // Click Add Member submit button
    const addButtons = screen.getAllByRole('button', { name: /add member/i })
    await user.click(addButtons[addButtons.length - 1])

    await waitFor(() => {
      expect(mockAddOrgMember).toHaveBeenCalledWith('new@example.com', 'member')
    })
  })

  it('displays role badges correctly', async () => {
    const members = [
      createOrgMember({ user_email: 'admin@example.com', org_role: 'org_admin' }),
      createOrgMember({ user_email: 'user@example.com', org_role: 'member' }),
    ]
    mockListOrgMembers.mockResolvedValue(members)

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('admin@example.com')).toBeInTheDocument())

    expect(screen.getByText('Org Admin')).toBeInTheDocument()
    expect(screen.getByText('Member')).toBeInTheDocument()
  })

  it('shows error message on load failure', async () => {
    mockListOrgMembers.mockRejectedValue(new Error('Server error'))

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('Server error')).toBeInTheDocument())
  })

  it('shows about organization roles info', async () => {
    mockListOrgMembers.mockResolvedValue([])

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('About Organization Roles')).toBeInTheDocument())
  })

  it('opens action menu and promotes member to org admin', async () => {
    const members = [
      createOrgMember({ user_email: 'admin@example.com', org_role: 'org_admin' }),
      createOrgMember({ user_email: 'regular@example.com', org_role: 'member' }),
    ]
    mockListOrgMembers.mockResolvedValue(members)
    mockUpdateOrgMember.mockResolvedValue(undefined)

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('regular@example.com')).toBeInTheDocument())

    const user = userEvent.setup()
    // Click the action menu (MoreVertical) for member - there should be 2 menus
    const menuButtons = screen.getAllByRole('button').filter(b => !b.textContent)
    // Click the second menu button (for regular@example.com)
    await user.click(menuButtons[menuButtons.length - 1])

    // Should see "Make Org Admin" for a member
    await waitFor(() => {
      expect(screen.getByText('Make Org Admin')).toBeInTheDocument()
    })
    await user.click(screen.getByText('Make Org Admin'))

    await waitFor(() => {
      expect(mockUpdateOrgMember).toHaveBeenCalledWith('regular@example.com', 'org_admin')
    })
  })

  it('opens action menu and demotes admin to member', async () => {
    const members = [
      createOrgMember({ user_email: 'admin1@example.com', org_role: 'org_admin' }),
      createOrgMember({ user_email: 'admin2@example.com', org_role: 'org_admin' }),
    ]
    mockListOrgMembers.mockResolvedValue(members)
    mockUpdateOrgMember.mockResolvedValue(undefined)

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('admin2@example.com')).toBeInTheDocument())

    const user = userEvent.setup()
    // Click the action menu for admin2
    const menuButtons = screen.getAllByRole('button').filter(b => !b.textContent)
    await user.click(menuButtons[menuButtons.length - 1])

    await waitFor(() => {
      expect(screen.getByText('Make Member')).toBeInTheDocument()
    })
    await user.click(screen.getByText('Make Member'))

    await waitFor(() => {
      expect(mockUpdateOrgMember).toHaveBeenCalledWith('admin2@example.com', 'member')
    })
  })

  it('removes a member from org', async () => {
    const members = [
      createOrgMember({ user_email: 'admin@example.com', org_role: 'org_admin' }),
      createOrgMember({ user_email: 'user@example.com', org_role: 'member' }),
    ]
    mockListOrgMembers.mockResolvedValue(members)
    mockRemoveOrgMember.mockResolvedValue(undefined)
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('user@example.com')).toBeInTheDocument())

    const user = userEvent.setup()
    // Open action menu for user
    const menuButtons = screen.getAllByRole('button').filter(b => !b.textContent)
    await user.click(menuButtons[menuButtons.length - 1])

    await waitFor(() => {
      expect(screen.getByText('Remove from Org')).toBeInTheDocument()
    })
    await user.click(screen.getByText('Remove from Org'))

    await waitFor(() => {
      expect(mockRemoveOrgMember).toHaveBeenCalledWith('user@example.com')
    })
  })

  it('cancels remove when confirm is rejected', async () => {
    const members = [
      createOrgMember({ user_email: 'admin@example.com', org_role: 'org_admin' }),
      createOrgMember({ user_email: 'user@example.com', org_role: 'member' }),
    ]
    mockListOrgMembers.mockResolvedValue(members)
    vi.spyOn(window, 'confirm').mockReturnValue(false)

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('user@example.com')).toBeInTheDocument())

    const user = userEvent.setup()
    const menuButtons = screen.getAllByRole('button').filter(b => !b.textContent)
    await user.click(menuButtons[menuButtons.length - 1])

    await waitFor(() => {
      expect(screen.getByText('Remove from Org')).toBeInTheDocument()
    })
    await user.click(screen.getByText('Remove from Org'))

    expect(mockRemoveOrgMember).not.toHaveBeenCalled()
  })

  it('cancels add member modal', async () => {
    mockListOrgMembers.mockResolvedValue([])

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(mockListOrgMembers).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /add member/i }))

    expect(screen.getByText('Add Organization Member')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /cancel/i }))

    expect(screen.queryByText('Add Organization Member')).not.toBeInTheDocument()
  })

  it('adds member as org_admin role', async () => {
    mockListOrgMembers.mockResolvedValue([])
    const newMember = createOrgMember({ user_email: 'newadmin@example.com', org_role: 'org_admin' })
    mockAddOrgMember.mockResolvedValue(newMember)

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(mockListOrgMembers).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /add member/i }))

    await user.type(screen.getByPlaceholderText('user@example.com'), 'newadmin@example.com')

    // Select Org Admin radio
    await user.click(screen.getByLabelText('Org Admin'))

    const addButtons = screen.getAllByRole('button', { name: /add member/i })
    await user.click(addButtons[addButtons.length - 1])

    await waitFor(() => {
      expect(mockAddOrgMember).toHaveBeenCalledWith('newadmin@example.com', 'org_admin')
    })
  })

  it('shows error when adding member fails', async () => {
    mockListOrgMembers.mockResolvedValue([])
    mockAddOrgMember.mockRejectedValue(new Error('Email already exists'))

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(mockListOrgMembers).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /add member/i }))
    await user.type(screen.getByPlaceholderText('user@example.com'), 'dup@example.com')

    const addButtons = screen.getAllByRole('button', { name: /add member/i })
    await user.click(addButtons[addButtons.length - 1])

    await waitFor(() => {
      expect(mockAddOrgMember).toHaveBeenCalled()
    })
    // The error is displayed on the page (may appear in both main area and modal)
    await waitFor(() => {
      expect(screen.getAllByText(/Email already exists/).length).toBeGreaterThanOrEqual(1)
    })
  })

  it('dismisses error message', async () => {
    mockListOrgMembers.mockRejectedValue(new Error('Network error'))

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('Network error')).toBeInTheDocument())

    const user = userEvent.setup()
    // Click the X button to dismiss
    const dismissButtons = screen.getAllByRole('button').filter(
      b => b.closest('.bg-red-50')
    )
    await user.click(dismissButtons[0])

    await waitFor(() => {
      expect(screen.queryByText('Network error')).not.toBeInTheDocument()
    })
  })

  it('shows empty state when no members exist', async () => {
    mockListOrgMembers.mockResolvedValue([])

    renderWithRouter(<OrgSettings />)
    await waitFor(() => {
      expect(screen.getByText('No members yet')).toBeInTheDocument()
    })
    expect(screen.getByText(/Members will appear here after they sign in/)).toBeInTheDocument()
  })

  it('shows service account badge', async () => {
    const members = [
      createOrgMember({ user_email: 'svc@example.com', org_role: 'member', is_service_account: true }),
    ]
    mockListOrgMembers.mockResolvedValue(members)

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('svc@example.com')).toBeInTheDocument())
    expect(screen.getByText('Service Account')).toBeInTheDocument()
  })

  it('shows invited_by and workspace count', async () => {
    const members = [
      createOrgMember({
        user_email: 'invited@example.com',
        org_role: 'member',
        invited_by: 'admin@example.com',
        workspaces: [{ workspace_id: 'ws-1', name: 'WS1', role: 'member' }],
      }),
    ]
    mockListOrgMembers.mockResolvedValue(members)

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('invited@example.com')).toBeInTheDocument())
    expect(screen.getByText(/Invited by admin@example.com/)).toBeInTheDocument()
    expect(screen.getByText(/1 workspace/)).toBeInTheDocument()
  })

  it('shows role change error', async () => {
    const members = [
      createOrgMember({ user_email: 'admin@example.com', org_role: 'org_admin' }),
      createOrgMember({ user_email: 'user@example.com', org_role: 'member' }),
    ]
    mockListOrgMembers.mockResolvedValue(members)
    mockUpdateOrgMember.mockRejectedValue(new Error('Permission denied'))

    renderWithRouter(<OrgSettings />)
    await waitFor(() => expect(screen.getByText('user@example.com')).toBeInTheDocument())

    const user = userEvent.setup()
    const menuButtons = screen.getAllByRole('button').filter(b => !b.textContent)
    await user.click(menuButtons[menuButtons.length - 1])

    await waitFor(() => {
      expect(screen.getByText('Make Org Admin')).toBeInTheDocument()
    })
    await user.click(screen.getByText('Make Org Admin'))

    await waitFor(() => {
      expect(mockUpdateOrgMember).toHaveBeenCalled()
    })
    await waitFor(() => {
      expect(screen.getByText(/Permission denied|Failed to update role/)).toBeInTheDocument()
    })
  })

  it('navigates to MCP Servers page', async () => {
    mockListOrgMembers.mockResolvedValue([])

    renderWithRouter(<OrgSettings />, {
      initialEntries: ['/org/settings'],
      path: '/org/settings',
    })
    await waitFor(() => expect(screen.getByText('MCP Servers')).toBeInTheDocument())
  })
})
