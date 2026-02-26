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
})
