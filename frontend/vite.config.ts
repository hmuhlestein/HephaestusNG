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
    port: parseInt(process.env.FRONTEND_PORT || '5300'),
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