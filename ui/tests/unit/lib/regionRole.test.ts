import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'

const mockFetch = vi.fn()
vi.stubGlobal('fetch', mockFetch)

import {
  useRegionRole,
  getRegionRole,
  _resetRegionRoleCacheForTests,
} from '../../../src/lib/regionRole'

describe('regionRole', () => {
  beforeEach(() => {
    _resetRegionRoleCacheForTests()
    mockFetch.mockReset()
  })

  it('returns active role when API reports active', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        role: 'active',
        region: 'us-west-2',
        primary_region: 'us-west-2',
        secondary_region: 'us-east-2',
      }),
    })

    const result = await getRegionRole()

    expect(result.role).toBe('active')
    expect(result.region).toBe('us-west-2')
    expect(result.primaryRegion).toBe('us-west-2')
    expect(result.secondaryRegion).toBe('us-east-2')
    expect(result.unknown).toBe(false)
    expect(result.loading).toBe(false)
  })

  it('returns passive role when API reports passive', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        role: 'passive',
        region: 'us-east-2',
        primary_region: 'us-west-2',
        secondary_region: 'us-east-2',
      }),
    })

    const result = await getRegionRole()

    expect(result.role).toBe('passive')
    expect(result.region).toBe('us-east-2')
  })

  it('falls back to active+unknown on fetch error', async () => {
    mockFetch.mockRejectedValueOnce(new Error('network'))

    const result = await getRegionRole()

    expect(result.role).toBe('active')
    expect(result.unknown).toBe(true)
    expect(result.loading).toBe(false)
  })

  it('falls back to active+unknown on non-ok response', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 503,
      json: async () => ({}),
    })

    const result = await getRegionRole()

    expect(result.role).toBe('active')
    expect(result.unknown).toBe(true)
  })

  it('caches the result and only fetches once', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ role: 'passive', region: 'us-east-2' }),
    })

    await getRegionRole()
    await getRegionRole()
    await getRegionRole()

    expect(mockFetch).toHaveBeenCalledTimes(1)
  })

  it('coalesces in-flight requests', async () => {
    mockFetch.mockImplementation(
      () =>
        new Promise(resolve =>
          setTimeout(
            () =>
              resolve({
                ok: true,
                json: async () => ({ role: 'active', region: 'us-west-2' }),
              }),
            10,
          ),
        ),
    )

    const [a, b, c] = await Promise.all([
      getRegionRole(),
      getRegionRole(),
      getRegionRole(),
    ])

    expect(mockFetch).toHaveBeenCalledTimes(1)
    expect(a.role).toBe('active')
    expect(b.role).toBe('active')
    expect(c.role).toBe('active')
  })

  it('useRegionRole resolves to the fetched state', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        role: 'passive',
        region: 'us-east-2',
        primary_region: 'us-west-2',
        secondary_region: 'us-east-2',
      }),
    })

    const { result } = renderHook(() => useRegionRole())

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.role).toBe('passive')
    expect(result.current.region).toBe('us-east-2')
  })

  it('treats non-"passive" values as active', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ role: 'something-weird' }),
    })

    const result = await getRegionRole()
    expect(result.role).toBe('active')
    expect(result.unknown).toBe(false)
  })
})
