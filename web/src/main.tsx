import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles.css'
// Apply persisted theme before first paint to avoid flash
import { applyPersistedTheme } from './hooks/useTheme'
applyPersistedTheme()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
