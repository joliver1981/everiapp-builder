import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'
import { version } from './package.json'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Injected UI version (package.json) — the sidebar shows it next to the API
  // version so a stale bundle or backend is visible at a glance.
  define: {
    __APP_VERSION__: JSON.stringify(version),
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8800',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
