import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '0.0.0.0',
    port: parseInt(process.env.FRONTEND_PORT || '5300'),
    // Suppress the white error overlay on HMR reconnect-on-idle: the Vite
    // HMR socket drops after ~2 min of idle (no file changes), the client
    // polls, finds the server back, and does a full location.reload().
    // The white flash is the error overlay rendering during that reload.
    hmr: {
      overlay: false,
    },
    watch: {
      // Exclude directories that change frequently (agent transcripts,
      // worktree checkouts, designs) which would otherwise fire the
      // chokidar watcher and cause spurious full-reload HMR events.
      ignored: [
        '**/.hephaestus/**',
        '**/.worktrees/**',
        '**/data/**',
        '**/.kilo/**',
      ],
    },
    proxy: {
      '/api': {
        target: `http://localhost:${process.env.BACKEND_PORT || '8300'}`,
        changeOrigin: true,
      },
      '/ws': {
        target: `ws://localhost:${process.env.BACKEND_PORT || '8300'}`,
        ws: true,
      },
    },
  },
})