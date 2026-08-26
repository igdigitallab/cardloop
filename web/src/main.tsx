import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import '@fontsource-variable/geist'
import '@fontsource-variable/geist-mono'
import './styles.css'
// Apply persisted theme before first paint to avoid flash
import { applyPersistedTheme } from './hooks/useTheme'
import { bootNative } from './native/boot'
import { consumeAuthHandoff } from './native/handoff'
applyPersistedTheme()

const rootEl = document.getElementById('root')!

async function start() {
  if (!bootNative(rootEl)) return
  // A passphrase handed over from the native server picker (URL fragment) is spent
  // here, BEFORE <App/> mounts — otherwise the login screen flashes on every fresh
  // install even though we already hold the credentials.
  await consumeAuthHandoff()
  ReactDOM.createRoot(rootEl).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  )
}

void start()
