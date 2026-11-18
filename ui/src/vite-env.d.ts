// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_STREAMING_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
