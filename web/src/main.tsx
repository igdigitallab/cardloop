import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import '@fontsource-variable/geist'
import '@fontsource-variable/geist-mono'
import './styles.css'
// Apply persisted theme before first paint to avoid flash
import { applyPersistedTheme } from './hooks/useTheme'
import { bootNative } from './native/boot'
applyPersistedTheme()

const rootEl = document.getElementById('root')!
if (bootNative(rootEl)) {
  ReactDOM.createRoot(rootEl).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  )
}
