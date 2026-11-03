import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// In development the dev server forwards /api to the Titan API and adds the session token itself,
// so the browser page never holds it. (The built app is served by the API, which puts the token in the page.)
function sessionToken(): string {
  try {
    return readFileSync(new URL('../data/api_token.txt', import.meta.url), 'utf-8').trim()
  } catch {
    return ''
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8000', headers: { 'X-Titan-Token': sessionToken() } } },
  },
})
