import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

const mockFetch = vi.fn()
vi.stubGlobal('fetch', mockFetch)

import RegionBanner from '../../../src/components/RegionBanner'
import MutationButton from '../../../src/components/MutationButton'
import { _resetRegionRoleCacheForTests } from '../../../src/lib/regionRole'

describe('RegionBanner', () => {
  beforeEach(() => {
    _resetRegionRoleCacheForTests()
    mockFetch.mockReset()
  })

  it('renders nothing when active', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ role: 'active', region: 'us-west-2' }),
    })

    const { container } = render(<RegionBanner />)
    await waitFor(() => {
      // After the fetch resolves the banner stays empty.
      expect(container.querySelector('[role="status"]')).toBeNull()
    })
  })

  it('renders banner when passive', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        role: 'passive',
        region: 'us-east-2',
        primary_region: 'us-west-2',
        secondary_region: 'us-east-2',
      }),
    })

    render(<RegionBanner />)

    await waitFor(() => {
      expect(screen.getByRole('status')).toBeTruthy()
    })
    expect(screen.getByText(/Read-only mode/i)).toBeTruthy()
    expect(screen.getByText(/us-east-2/)).toBeTruthy()
    expect(screen.getByText(/us-west-2/)).toBeTruthy()
  })

  it('renders nothing when fetch fails (unknown role)', async () => {
    mockFetch.mockRejectedValueOnce(new Error('network'))

    const { container } = render(<RegionBanner />)
    // Banner should not appear because fallback role is 'active'.
    await waitFor(() => {
      // Component re-renders on resolve, but banner remains hidden.
      expect(container.querySelector('[role="status"]')).toBeNull()
    })
  })
})

describe('MutationButton', () => {
  beforeEach(() => {
    _resetRegionRoleCacheForTests()
    mockFetch.mockReset()
  })

  it('is enabled when region is active', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ role: 'active', region: 'us-west-2' }),
    })

    render(<MutationButton>Save</MutationButton>)
    const button = screen.getByRole('button', { name: /save/i }) as HTMLButtonElement

    await waitFor(() => {
      expect(button.disabled).toBe(false)
    })
  })

  it('is disabled when region is passive', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ role: 'passive', region: 'us-east-2' }),
    })

    render(<MutationButton>Save</MutationButton>)
    const button = screen.getByRole('button', { name: /save/i }) as HTMLButtonElement

    await waitFor(() => {
      expect(button.disabled).toBe(true)
    })
    expect(button.title).toMatch(/passive region/i)
  })

  it('preserves disabled=true from props when region is active', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ role: 'active', region: 'us-west-2' }),
    })

    render(<MutationButton disabled>Save</MutationButton>)
    const button = screen.getByRole('button', { name: /save/i }) as HTMLButtonElement

    await waitFor(() => {
      // Even after region resolves, the explicit prop disable wins.
      expect(button.disabled).toBe(true)
    })
  })

  it('uses custom passiveTitle when provided', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ role: 'passive', region: 'us-east-2' }),
    })

    render(
      <MutationButton passiveTitle="Custom blocked message">Sync</MutationButton>,
    )
    const button = screen.getByRole('button', { name: /sync/i }) as HTMLButtonElement

    await waitFor(() => {
      expect(button.title).toBe('Custom blocked message')
    })
  })
})
