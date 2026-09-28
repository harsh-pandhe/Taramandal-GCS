import { defineConfig } from 'vite'
import { configDefaults } from 'vitest/config'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // e2e/ holds Playwright specs, which run under `npm run test:e2e`, not Vitest.
  test: {
    exclude: [...configDefaults.exclude, 'e2e/**'],
  },
  build: {
    rollupOptions: {
      output: {
        // Split heavy vendor libs (three.js + its react renderers, react) into
        // their own chunks so app code stays small and vendor bundles cache
        // across deploys. Also resolves the >500kB single-chunk warning.
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('three')) return 'three'
            if (id.includes('react')) return 'react'
            return 'vendor'
          }
        },
      },
    },
  },
})
