import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// The HMR websocket drops after ~2 min of idle (no HMR traffic), and any
// non-clean close makes Vite's client assume the server restarted -- once
// it can reach the server again it force-reloads the page (see
// vite/dist/client/client.mjs: close handler always calls location.reload()
// unless wasClean). Sending a no-op "custom" event periodically keeps the
// socket active so it never goes idle long enough to be dropped.
function hmrKeepAlive(): Plugin {
  return {
    name: 'hmr-keepalive',
    configureServer(server) {
      const interval = setInterval(() => {
        server.ws.send({ type: 'custom', event: 'hmr-keepalive', data: {} })
      }, 30000)
      server.httpServer?.once('close', () => clearInterval(interval))
    },
  }
}

export default defineConfig({
  plugins: [react(), hmrKeepAlive()],
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