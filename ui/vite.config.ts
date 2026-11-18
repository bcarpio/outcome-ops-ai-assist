// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // Listen on all interfaces (0.0.0.0) so the dev server is reachable over
    // the LAN / SSH host (e.g. http://192.168.86.15:5173), not just localhost.
    // Dev-server only -- has no effect on the production build or CI.
    host: true,
    port: 5173,
    proxy: {
      // Proxy API requests to the Express server
      '/api': {
        target: 'http://localhost:3000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
