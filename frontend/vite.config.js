import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'PATCHLOOP_')
  const proxy = {
    '/api': {
      target: process.env.PATCHLOOP_API_TARGET || env.PATCHLOOP_API_TARGET || 'http://127.0.0.1:8000',
      changeOrigin: true,
      rewrite: (path) => path.replace(/^\/api(?=\/|$)/, ''),
    },
  }
  return {
    plugins: [react(), tailwindcss()],
    server: { port: 5173, strictPort: true, proxy },
    preview: { port: 4173, strictPort: true, proxy },
    test: { environment: 'jsdom', setupFiles: './src/test/setup.js', clearMocks: true },
  }
})
