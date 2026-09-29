import { defineConfig, configDefaults } from 'vitest/config'

// Vitest 3 runs on its own Vite 7, where @vitejs/plugin-react v6 (a Vite 8
// plugin) does not transform JSX; esbuild does, and defaults to the classic
// runtime ("React is not defined"). Component tests need the automatic
// runtime the app builds with. vite.config.js still drives the real build.
export default defineConfig({
  esbuild: { jsx: 'automatic' },
  test: {
    exclude: [...configDefaults.exclude, 'e2e/**'],
  },
})
