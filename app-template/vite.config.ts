import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // The AI imports platform hooks from '@aihub/app-sdk' (useDataset, useAppDB,
  // AI-Toggle hooks, …). The SDK is vendored into src/sdk so each generated app
  // is self-contained — this alias makes that package specifier resolve for the
  // build (tsconfig "paths" does the same for the type-checker), here and on a
  // deploy target where the repo's app-sdk/ doesn't exist.
  resolve: {
    alias: {
      '@aihub/app-sdk': fileURLToPath(new URL('./src/sdk/index.ts', import.meta.url)),
    },
  },
  base: process.env.VITE_BASE || '/',
  // VITE_AIHUB_BASE_URL is baked in by the platform's builder. The SDK reads it
  // via import.meta.env to know where to call back for config + auth when the
  // app is deployed off-platform. Empty = same-origin (local preview mode).
  define: {
    'import.meta.env.VITE_AIHUB_BASE_URL': JSON.stringify(
      process.env.VITE_AIHUB_BASE_URL || ''
    ),
  },
  server: {
    port: 0,
  },
})
