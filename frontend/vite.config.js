import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: true,
    // 开发时把 /api 代到后端；生产由 FastAPI 直接托管 dist
    proxy: { '/api': 'http://127.0.0.1:8756' },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
