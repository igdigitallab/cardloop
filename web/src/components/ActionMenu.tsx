/**
 * ActionMenu — adaptive action menu for sidebar rows.
 *
 * Desktop (pointer:fine / width >768px):
 *   Dropdown popover anchored to the trigger element's DOMRect.
 *   Positioned below-left of the anchor; auto-flips if it would overflow.
 *
 * Mobile (width ≤768px):
 *   Bottom sheet — slides up from the bottom of the viewport, full-width,
 *   dim backdrop, tap-backdrop or swipe-down to dismiss.
 *
 * Drill-in navigation: items with a `submenu` property show a "›" chevron.
 * Clicking navigates into the submenu view; a sticky "‹ Back" row returns to
 * the root. Works identically in both the dropdown and bottom sheet.
 *
 * Ref: https://developer.mozilla.org/en-US/docs/Web/API/Element/getBoundingClientRect
 */
import { useEffect, useRef, useState, ReactNode } from 'react'
import { createPortal } from 'react-dom'

export interface ActionMenuItem {
  label: string
  icon?: string
  danger?: boolean
  disabled?: boolean
  checked?: boolean
  onClick?: () => void
  /** When set, clicking this item drills into a submenu instead of calling onClick */
  submenu?: ActionMenuSection[]
}

export interface ActionMenuSection {
  /** Optional section title rendered as a small label above the items */
  title?: string
  items: ActionMenuItem[]
}

export interface ActionMenuProps {
  /** DOMRect of the trigger button — used to anchor the desktop dropdown */
  anchorRect: DOMRect | null
  /** Flat sections; each may have an optional title */
  sections: ActionMenuSection[]
  /** Called when the menu should close (backdrop click, Escape, item click) */
  onClose: () => void
}

interface DrillState {
  title: string
  sections: ActionMenuSection[]
}

/** Returns true when the device likely has a mouse (non-touch primary input) */
function isPointerFine(): boolean {
  return window.matchMedia('(pointer: fine)').matches
}

function isNarrow(): boolean {
  return window.innerWidth <= 768
}

function useShouldUseBottomSheet(): boolean {
  // bottom sheet when either the pointer is coarse OR viewport is narrow
  return isNarrow() || !isPointerFine()
}

export function ActionMenu({ anchorRect, sections, onClose }: ActionMenuProps) {
  const bottomSheet = useShouldUseBottomSheet()
  const menuRef = useRef<HTMLDivElement>(null)

  // Drill-in state: null = root view, non-null = drill view
  const [drill, setDrill] = useState<DrillState | null>(null)

  // Reset drill when menu closes
  function handleClose() {
    setDrill(null)
    onClose()
  }

  // Close on Escape
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        if (drill) {
          // Escape from drill goes back to root
          setDrill(null)
        } else {
          handleClose()
        }
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [drill, onClose])

  // Desktop: close on outside click
  useEffect(() => {
    if (bottomSheet) return
    function onMd(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        handleClose()
      }
    }
    // Use setTimeout so the current click that opened the menu is not immediately
    // consumed by this handler.
    const id = setTimeout(() => document.addEventListener('mousedown', onMd), 0)
    return () => {
      clearTimeout(id)
      document.removeEventListener('mousedown', onMd)
    }
  }, [bottomSheet, onClose])

  function handleItemClick(item: ActionMenuItem) {
    if (item.disabled) return
    if (item.submenu) {
      // Navigate into the drill view
      setDrill({ title: item.label, sections: item.submenu })
      return
    }
    if (item.onClick) item.onClick()
    handleClose()
  }

  function renderItems(sectionList: ActionMenuSection[], isDrill: boolean) {
    return (
      <>
        {isDrill && (
          <button
            className="action-menu-back-row"
            type="button"
            onClick={() => setDrill(null)}
            aria-label="Back"
          >
            <span className="action-menu-back-chevron">‹</span>
            <span className="action-menu-back-label">{drill?.title ?? 'Back'}</span>
          </button>
        )}
        {sectionList.map((section, si) => (
          <div key={si} className="action-menu-section">
            {section.title && (
              <div className="action-menu-section-title">{section.title}</div>
            )}
            {section.items.map((item, ii) => (
              <button
                key={ii}
                className={[
                  'action-menu-item',
                  item.danger ? 'danger' : '',
                  item.disabled ? 'disabled' : '',
                  item.submenu ? 'has-submenu' : '',
                ].filter(Boolean).join(' ')}
                onClick={() => handleItemClick(item)}
                disabled={item.disabled && !item.submenu}
                type="button"
              >
                {item.icon && <span className="action-menu-item-icon">{item.icon}</span>}
                <span className="action-menu-item-label">{item.label}</span>
                {item.checked && <span className="action-menu-item-check">✓</span>}
                {item.submenu && <span className="action-menu-item-chevron">›</span>}
              </button>
            ))}
            {si < sectionList.length - 1 && <div className="action-menu-separator" />}
          </div>
        ))}
      </>
    )
  }

  const activeSections = drill ? drill.sections : sections

  if (bottomSheet) {
    return createPortal(
      <div className="action-menu-backdrop" onPointerDown={handleClose}>
        <div
          ref={menuRef}
          className="action-menu-sheet"
          onPointerDown={e => e.stopPropagation()}
        >
          <div className="action-menu-sheet-handle" />
          <div className="action-menu-sheet-body">
            {renderItems(activeSections, !!drill)}
          </div>
        </div>
      </div>,
      document.body
    )
  }

  // Desktop dropdown — anchor to trigger rect
  let style: React.CSSProperties = { position: 'fixed', zIndex: 9999 }

  if (anchorRect) {
    const menuW = 200
    // Estimate height: each item ~36px, section titles ~28px, back row ~44px, separators ~9px
    const itemCount = activeSections.reduce((acc, s) => acc + s.items.length + (s.title ? 0.78 : 0), 0)
    const menuH = Math.min(400, itemCount * 36 + (drill ? 44 : 0) + activeSections.length * 5 + 8)

    // Prefer below-left of anchor
    let left = anchorRect.right - menuW
    let top = anchorRect.bottom + 4

    // Flip left if overflow right
    if (left + menuW > window.innerWidth - 8) left = window.innerWidth - menuW - 8
    if (left < 8) left = 8

    // Flip up if overflow bottom
    if (top + menuH > window.innerHeight - 8) top = anchorRect.top - menuH - 4
    if (top < 8) top = 8

    style = { ...style, left, top }
  }

  return createPortal(
    <div
      ref={menuRef}
      className="action-menu-dropdown"
      style={style}
      onPointerDown={e => e.stopPropagation()}
    >
      {renderItems(activeSections, !!drill)}
    </div>,
    document.body
  )
}

/** Convenience: a vertical kebab "⋮" button that opens ActionMenu on click */
interface KebabButtonProps {
  /** Accessible label for the button */
  label?: string
  onClick: (rect: DOMRect) => void
  className?: string
  children?: ReactNode
}

export function KebabButton({ label = 'More actions', onClick, className, children }: KebabButtonProps) {
  function handleClick(e: React.MouseEvent<HTMLButtonElement>) {
    e.stopPropagation()
    e.preventDefault()
    const rect = e.currentTarget.getBoundingClientRect()
    onClick(rect)
  }

  return (
    <button
      type="button"
      className={['sidebar-kebab-btn', className].filter(Boolean).join(' ')}
      onPointerDown={e => e.stopPropagation()}
      onClick={handleClick}
      aria-label={label}
      title={label}
    >
      {children ?? (
        /* Vertical three-dot kebab — standard SVG for crisp rendering */
        <svg width="14" height="14" viewBox="0 0 4 16" fill="currentColor" aria-hidden="true">
          <circle cx="2" cy="2"  r="1.5" />
          <circle cx="2" cy="8"  r="1.5" />
          <circle cx="2" cy="14" r="1.5" />
        </svg>
      )}
    </button>
  )
}
