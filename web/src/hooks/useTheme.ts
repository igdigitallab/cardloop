/**
 * useTheme — manual theme switcher.
 *
 * Theme values: "light" | "dark" | "auto"
 *   - "light" / "dark" → sets data-theme attribute on <html>
 *   - "auto"           → removes data-theme attribute (OS prefers-color-scheme takes over)
 *
 * Persisted in localStorage under the key "cops.theme".
 */
import { useState, useCallback, useEffect } from 'react'

export type ThemeValue = 'light' | 'dark' | 'auto'

const LS_KEY = 'cops.theme'

/** Apply the theme to the document root without triggering a React render. */
function applyTheme(theme: ThemeValue): void {
  if (theme === 'auto') {
    document.documentElement.removeAttribute('data-theme')
  } else {
    document.documentElement.setAttribute('data-theme', theme)
  }
}

/** Read the persisted theme or fall back to 'auto'. */
function readTheme(): ThemeValue {
  try {
    const v = localStorage.getItem(LS_KEY)
    if (v === 'light' || v === 'dark' || v === 'auto') return v
  } catch { /* ignore */ }
  return 'auto'
}

export function useTheme(): [ThemeValue, (t: ThemeValue) => void] {
  const [theme, setThemeState] = useState<ThemeValue>(() => {
    const v = readTheme()
    // Apply immediately on first render (before paint if called from module scope)
    applyTheme(v)
    return v
  })

  const setTheme = useCallback((next: ThemeValue) => {
    try { localStorage.setItem(LS_KEY, next) } catch { /* ignore */ }
    applyTheme(next)
    setThemeState(next)
  }, [])

  // Guard: if another tab changes localStorage, sync here too
  useEffect(() => {
    function onStorage(e: StorageEvent) {
      if (e.key !== LS_KEY) return
      const v = e.newValue
      if (v === 'light' || v === 'dark' || v === 'auto') {
        applyTheme(v)
        setThemeState(v)
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  return [theme, setTheme]
}

/**
 * Apply the persisted theme immediately before the React tree mounts,
 * called from main.tsx to avoid a flash of unstyled dark theme on light devices.
 */
export function applyPersistedTheme(): void {
  applyTheme(readTheme())
}
