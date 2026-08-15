import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      }
    }
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.js'],
    // PatternFly ships CSS imports inside its component modules; without
    // this every page import fails to parse under the test runner.
    css: false,
    include: ['src/**/*.test.{js,jsx}'],
  },
})
