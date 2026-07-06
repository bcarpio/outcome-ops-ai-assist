import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config'

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      globals: true,
      environment: 'jsdom',
      setupFiles: ['./tests/setup.ts'],
      include: ['tests/**/*.test.{ts,tsx}'],
      coverage: {
        provider: 'v8',
        reporter: ['text', 'lcov'],
        reportsDirectory: 'coverage',
        // Chunk 13: include server/middleware.js so the extracted auth
        // gate + 404 guard + error handler count toward coverage. Keep
        // server/index.js excluded (it binds to a port + loads SSM at
        // import time; the pure middleware factory lives in middleware.js).
        include: ['src/**/*.{ts,tsx}', 'server/middleware.js'],
        exclude: ['src/main.tsx', 'src/vite-env.d.ts', 'server/index.js'],
        thresholds: {
          statements: 45,
          branches: 35,
          functions: 35,
          lines: 45,
        },
      },
    },
  })
)
