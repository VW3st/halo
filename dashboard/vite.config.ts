import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Builds the React dashboard straight into halo/web_static so the existing
// Flask server (halo/web.py) serves it with no extra plumbing. base:'./'
// keeps asset URLs relative so they resolve under Flask's "/" route.
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: '../halo/web_static',
    emptyOutDir: true,
    assetsDir: 'assets',
    chunkSizeWarningLimit: 1500,
  },
  server: {
    // `npm run dev` proxies the API calls to the running Flask instance so
    // the dashboard can be developed with hot-reload against live Halo data.
    proxy: {
      '/api': 'http://127.0.0.1:7070',
    },
  },
})
