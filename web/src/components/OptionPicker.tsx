/**
 * OptionPicker — CLI-style choice picker rendered from an ```options fenced block.
 *
 * When the agent ends a message with a fenced code block tagged `options`, this
 * component renders the choices as an interactive selectable list.
 *
 * Interaction:
 *   - Mouse: hover highlights, click selects.
 *   - Keyboard (when active): ↑/↓ navigate, Enter confirms, 1–9 select directly.
 *   - Only the picker on the LAST assistant message is interactive; older ones are
 *     rendered as static disabled lists so history stays clean.
 *
 * On select: calls onSelect(fullOptionText) — the caller (ChatTab) sends that text
 * as the user's next message.
 *
 * After selecting, the picker becomes inert (selected=true) and cannot be re-triggered.
 */

import { useEffect, useRef, useState } from 'react'
import { t } from '../i18n'

// ---------------------------------------------------------------------------
// Parser
// ---------------------------------------------------------------------------

export interface ParsedOptions {
  /** Text before the ```options block (may be empty). */
  prefix: string
  /** Parsed option lines, with leading numbering/bullet stripped for display but
   *  the full original line kept as the send value. */
  options: Array<{ label: string; value: string }>
}

/**
 * Detect and parse a trailing ```options fenced block from an assistant message.
 * Returns null when no such block is found (the message should render normally).
 *
 * Only a fenced block with the exact info-string `options` as a trailing block
 * triggers the picker. This avoids false-positives on ordinary numbered lists.
 */
export function parseOptionsBlock(text: string): ParsedOptions | null {
  // Match a ```options ... ``` block that is at the end of the text (optional trailing whitespace).
  // The block may be preceded by any content (the `prefix`).
  const match = text.match(/^([\s\S]*?)```options\n([\s\S]*?)```\s*$/m)
  if (!match) return null

  const prefix = match[1].trimEnd()
  const body = match[2]

  const options: Array<{ label: string; value: string }> = []
  for (const raw of body.split('\n')) {
    const line = raw.trim()
    if (!line) continue
    // Strip leading `N.` / `N)` / `- ` / `* ` numbering prefix for display
    const label = line.replace(/^(\d+[.)]\s*|-\s*|\*\s*)/, '').trim()
    if (!label) continue
    // Keep the original line (after leading-number strip) as the send value.
    // This matches what the user sees and what the agent's text says.
    options.push({ label, value: label })
  }

  if (options.length === 0) return null

  return { prefix, options }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface OptionPickerProps {
  options: Array<{ label: string; value: string }>
  /** When true, this picker is on the last assistant message and is interactive. */
  isActive: boolean
  /** Called with the chosen option's value when the user picks one. */
  onSelect: (value: string) => void
  /** A previously-chosen option value, recovered from chat history. Local `selectedIndex`
   *  is component state that resets on a ChatTab remount (mobile screen lock/unlock), which
   *  re-arms an already-answered picker — a second tap then double-submits. This durable
   *  signal (the answer is a real user message in history) keeps the picker inert. */
  answeredValue?: string | null
}

export function OptionPicker({ options, isActive, onSelect, answeredValue }: OptionPickerProps) {
  const [highlighted, setHighlighted] = useState<number>(0)
  // Once selected, the picker becomes inert — renders as a static confirmed choice.
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  // Effective selection: local click OR a history-recovered answer (survives remount).
  const externalIdx = answeredValue != null
    ? options.findIndex(o => o.value.trim() === answeredValue.trim())
    : -1
  const effectiveSelected = selectedIndex !== null
    ? selectedIndex
    : (externalIdx >= 0 ? externalIdx : null)

  // Auto-focus the container when this picker is the active one (last message, no run).
  // This lets arrow keys work immediately without an explicit click.
  useEffect(() => {
    if (isActive && effectiveSelected === null) {
      containerRef.current?.focus()
    }
  }, [isActive, effectiveSelected])

  function pick(idx: number) {
    if (!isActive || effectiveSelected !== null) return
    setSelectedIndex(idx)
    onSelect(options[idx].value)
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (!isActive || effectiveSelected !== null) return
    // Do not handle if the textarea is focused (guard: event bubbles up only from here)
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlighted(h => (h + 1) % options.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlighted(h => (h - 1 + options.length) % options.length)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      pick(highlighted)
    } else {
      // Number keys 1–9 for direct selection
      const n = parseInt(e.key, 10)
      if (!isNaN(n) && n >= 1 && n <= options.length) {
        e.preventDefault()
        pick(n - 1)
      }
    }
  }

  const isDisabled = !isActive || effectiveSelected !== null

  return (
    <div
      ref={containerRef}
      className={`option-picker${isDisabled ? ' option-picker--disabled' : ''}`}
      role="listbox"
      aria-label={t['chat.option_picker_label']}
      aria-disabled={isDisabled}
      tabIndex={isActive && effectiveSelected === null ? 0 : -1}
      onKeyDown={handleKeyDown}
    >
      {options.map((opt, idx) => {
        const isHighlighted = !isDisabled && highlighted === idx
        const isSelected = effectiveSelected === idx
        return (
          <div
            key={idx}
            role="option"
            aria-selected={isSelected || isHighlighted}
            className={[
              'option-picker__row',
              isHighlighted ? 'option-picker__row--highlighted' : '',
              isSelected ? 'option-picker__row--selected' : '',
              isDisabled && !isSelected ? 'option-picker__row--static' : '',
            ].filter(Boolean).join(' ')}
            onClick={() => pick(idx)}
            onMouseEnter={() => {
              if (!isDisabled) setHighlighted(idx)
            }}
          >
            <span className="option-picker__num">{idx + 1}</span>
            <span className="option-picker__label">{opt.label}</span>
            {isSelected && (
              <span className="option-picker__check" aria-hidden="true">✓</span>
            )}
          </div>
        )
      })}
      {isActive && effectiveSelected === null && (
        <div className="option-picker__hint" aria-hidden="true">
          {t['chat.option_picker_hint']}
        </div>
      )}
    </div>
  )
}
