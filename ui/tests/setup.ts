// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { afterEach, vi } from 'vitest'

// allow node-environment tests (server/ suites) to opt out
// of the DOM setup. A per-file `// @vitest-environment node` directive
// disables `window`; the DOM shims below then throw at load time.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const hasDom = typeof (globalThis as any).window !== 'undefined'

if (hasDom) {
  // Dynamic import so jest-dom's window-touching side effects don't
  // execute in node-env suites.
  await import('@testing-library/jest-dom/vitest')
  const { cleanup } = await import('@testing-library/react')

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  // Mock localStorage
  const localStorageMock = (() => {
    let store: Record<string, string> = {}
    return {
      getItem: (key: string) => store[key] ?? null,
      setItem: (key: string, value: string) => { store[key] = value },
      removeItem: (key: string) => { delete store[key] },
      clear: () => { store = {} },
      get length() { return Object.keys(store).length },
      key: (index: number) => Object.keys(store)[index] ?? null,
    }
  })()
  Object.defineProperty(window, 'localStorage', { value: localStorageMock })

  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })

  Element.prototype.scrollIntoView = vi.fn()
} else {
  // Node-env suites still need vi.restoreAllMocks between tests.
  afterEach(() => {
    vi.restoreAllMocks()
  })
}
