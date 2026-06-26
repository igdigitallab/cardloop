/**
 * ThemeToggle — compact three-way theme switcher (Light / Dark / Auto).
 * Rendered fixed top-right in App.tsx so it's reachable from any screen.
 * Styled in base.css under .theme-toggle.
 */
import { ThemeValue } from '../hooks/useTheme'

interface ThemeToggleProps {
  theme: ThemeValue
  onChange: (t: ThemeValue) => void
}

const OPTIONS: { value: ThemeValue; label: string; title: string }[] = [
  { value: 'light', label: '☀', title: 'Light (daylight)' },
  { value: 'auto',  label: 'A',   title: 'Auto (follows OS)' },
  { value: 'dark',  label: '🌙', title: 'Dark' },
]

export function ThemeToggle({ theme, onChange }: ThemeToggleProps) {
  return (
    <div className="theme-toggle" role="group" aria-label="Color theme">
      {OPTIONS.map(opt => (
        <button
          key={opt.value}
          className={`theme-toggle-btn${theme === opt.value ? ' active' : ''}`}
          onClick={() => onChange(opt.value)}
          title={opt.title}
          aria-pressed={theme === opt.value}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}
