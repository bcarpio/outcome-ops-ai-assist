// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AiDisclosureModal from '../../../src/components/AiDisclosureModal'

const STORAGE_KEY = 'ai-disclosure-accepted'

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  // Reset import.meta.env
  vi.stubEnv('VITE_AI_DISCLOSURE_ENABLED', 'true')
})

describe('AiDisclosureModal', () => {
  it('renders the modal when not previously accepted', () => {
    render(<AiDisclosureModal />)

    expect(screen.getByText('AI-Powered Assistant')).toBeInTheDocument()
    expect(screen.getByText('Important notice before you begin')).toBeInTheDocument()
    expect(screen.getByText('I Accept')).toBeInTheDocument()
    expect(screen.getByText('Not Now')).toBeInTheDocument()
  })

  it('shows all disclosure items', () => {
    render(<AiDisclosureModal />)

    expect(screen.getByText(/AI-generated content may contain errors/)).toBeInTheDocument()
    expect(screen.getByText(/Do not share sensitive personal information/)).toBeInTheDocument()
    expect(screen.getByText(/Interactions are logged for audit/)).toBeInTheDocument()
    expect(screen.getByText(/AI outputs do not constitute professional advice/)).toBeInTheDocument()
    expect(screen.getByText(/Your organization's data governance/)).toBeInTheDocument()
  })

  it('renders nothing when already accepted', () => {
    localStorage.setItem(STORAGE_KEY, 'true')

    const { container } = render(<AiDisclosureModal />)
    expect(container.innerHTML).toBe('')
  })

  it('sets localStorage and hides modal when accepted', async () => {
    render(<AiDisclosureModal />)

    expect(screen.getByText('AI-Powered Assistant')).toBeInTheDocument()

    const user = userEvent.setup()
    await user.click(screen.getByText('I Accept'))

    expect(localStorage.getItem(STORAGE_KEY)).toBe('true')
    expect(screen.queryByText('AI-Powered Assistant')).not.toBeInTheDocument()
  })

  it('signs the user out via /api/logout when "Not Now" is clicked', async () => {
    const original = window.location
    const hrefSetter = vi.fn()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: {
        set href(v: string) {
          hrefSetter(v)
        },
      },
    })

    render(<AiDisclosureModal />)

    const user = userEvent.setup()
    await user.click(screen.getByText('Not Now'))

    expect(hrefSetter).toHaveBeenCalledWith('/api/logout')
    // Declining must NOT record acceptance
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()

    Object.defineProperty(window, 'location', {
      configurable: true,
      value: original,
    })
  })

  it('renders nothing when feature is disabled', () => {
    vi.stubEnv('VITE_AI_DISCLOSURE_ENABLED', 'false')

    const { container } = render(<AiDisclosureModal />)
    expect(container.innerHTML).toBe('')
  })

  it('renders modal when feature env var is not set (default enabled)', () => {
    vi.stubEnv('VITE_AI_DISCLOSURE_ENABLED', '')

    render(<AiDisclosureModal />)
    expect(screen.getByText('AI-Powered Assistant')).toBeInTheDocument()
  })

  it('shows the artificial intelligence text in body', () => {
    render(<AiDisclosureModal />)

    expect(screen.getByText(/artificial intelligence/)).toBeInTheDocument()
    expect(screen.getByText(/not a human/)).toBeInTheDocument()
  })
})
