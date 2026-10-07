import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import fs from 'fs'

// When TLS_CERT_FILE/TLS_KEY_FILE are set (see scripts/dev.sh and
// scripts/generate-self-signed-cert.sh), serve the Vite dev server itself
// over HTTPS too -- browsers only expose features like microphone access
// (getUserMedia) in a "secure context" (HTTPS or http://localhost), so a
// bare-metal box reached over http://<lan-ip> needs both the frontend dev
// server and the Express API server on HTTPS.
const tlsCertFile = process.env.TLS_CERT_FILE
const tlsKeyFile = process.env.TLS_KEY_FILE
const httpsOptions =
  tlsCertFile && tlsKeyFile
    ? { cert: fs.readFileSync(tlsCertFile), key: fs.readFileSync(tlsKeyFile) }
    : undefined
// The Express server (server/server.js) is started with the same cert, so
// proxied requests need https:// targets too when TLS is enabled.
const proxyScheme = httpsOptions ? 'https' : 'http'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  envPrefix: ['VITE_', 'REACT_APP_'],
  server: {
    host: process.env.VITE_HOST || '0.0.0.0',
    port: Number(process.env.VITE_PORT) || 5173,
    strictPort: true,
    https: httpsOptions,
    watch: {
      ignored: ['**/.venv/**', '**/node_modules/**', '**/dist/**', '**/.git/**'],
    },
    proxy: {
      '/api/cluster-overview': {
        target: process.env.VITE_AUTH_PROXY_TARGET || `${proxyScheme}://localhost:3000`,
        changeOrigin: true,
        secure: false,
      },
      '/api': {
        target: process.env.VITE_API_PROXY_TARGET || `${proxyScheme}://localhost:3000`,
        changeOrigin: true,
        secure: false,
      }
    }
  }
})
