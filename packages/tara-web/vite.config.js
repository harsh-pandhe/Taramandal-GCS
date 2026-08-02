import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import cesium from 'vite-plugin-cesium'

// https://vite.dev/config/
export default defineConfig({
  // vite-plugin-cesium copies Cesium's Workers/Assets/Widgets/ThirdParty
  // static assets to /cesium/ (dev server middleware + a build-time copy) and
  // sets window.CESIUM_BASE_URL for us — Cesium loads those at runtime via
  // plain <script>/fetch, not through Vite's module graph, so they can't be
  // bundled the normal way.
  plugins: [react(), cesium()],
  build: {
    rollupOptions: {
      output: {
        // Split heavy vendor libs (three.js + its react renderers, react,
        // cesium) into their own chunks so app code stays small and vendor
        // bundles cache across deploys. Also resolves the >500kB single-chunk
        // warning.
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('cesium')) return 'cesium'
            if (id.includes('three')) return 'three'
            if (id.includes('react')) return 'react'
            return 'vendor'
          }
        },
      },
    },
  },
})
