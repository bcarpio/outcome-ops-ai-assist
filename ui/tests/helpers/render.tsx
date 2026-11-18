// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { render, type RenderOptions } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router'
import type { ReactElement } from 'react'

interface RouterOptions extends RenderOptions {
  initialEntries?: string[]
  path?: string
}

export function renderWithRouter(
  ui: ReactElement,
  { initialEntries = ['/'], path, ...options }: RouterOptions = {}
) {
  const content = path ? (
    <Routes>
      <Route path={path} element={ui} />
    </Routes>
  ) : ui

  return render(
    <MemoryRouter initialEntries={initialEntries}>{content}</MemoryRouter>,
    options
  )
}
