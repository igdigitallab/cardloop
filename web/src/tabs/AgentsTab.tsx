import { useCallback, useEffect, useState } from 'react'
import { Pencil, Trash2, Copy, Eye } from 'lucide-react'
import { api } from '../api'
import { ProjectRoles, RoleJSON, RoleScope } from '../types'
import { Spinner } from '../components/Spinner'
import { ConfirmModal } from '../components/ConfirmModal'
import { Modal, ModalHead } from '../components/Modal'
import { useToast, ToastContainer } from '../components/Toast'
import { useOnRunEnd, useFocusRefresh } from '../hooks/useProjectActivity'
import { t } from '../i18n'

// spec-091 Phase 1 — cockpit UI for the declarative agent-role registry.
// Contract: docs/internal/specs/spec-091-agent-roles/IMPLEMENTATION.md §4-6.
// Mirrors MemoryTab.tsx (list → modal editor → save/delete) — same components,
// same "edit the file" mental model, just with role-specific metadata rows.

interface Props {
  projectId: string
}

const ROLE_NAME_RE = /^[a-z0-9][a-z0-9-]{1,31}$/

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function errStatus(e: unknown): number | undefined {
  return (e as { status?: number } | undefined)?.status
}

function rowKey(role: Pick<RoleJSON, 'name' | 'scope'>): string {
  return `${role.scope}:${role.name}`
}

function newRoleTemplate(name: string): string {
  const slug = name || 'my-role'
  return `---
name: ${slug}
description: Use this role when ...
enabled: true
tools: [Read, Grep, Glob, Bash]
model: claude-sonnet-5
effort: medium
maxTurns: 20
---
You are ${slug}. Describe what this role does and how it should behave.
`
}

const MAIN_ROLE_TEMPLATE = `---
name: main
description: Project-specific instructions for the main agent (appended to CLAUDE.md).
enabled: true
---
Add project-specific instructions for the main agent here.
`

// ── Editor modal (new / edit / view-only for builtin) ─────────────────────────

type EditorMode = 'new' | 'edit' | 'view'

interface EditorState {
  mode: EditorMode
  name: string
  scope: RoleScope
  content: string
  loading: boolean
  saving: boolean
  error: string
}

function RoleEditorModal({ editor, onChange, onSave, onClose }: {
  editor: EditorState
  onChange: (patch: Partial<EditorState>) => void
  onSave: () => void
  onClose: () => void
}) {
  const isNew = editor.mode === 'new'
  const isView = editor.mode === 'view'
  const title = isNew
    ? t['agents.new_btn']
    : `${isView ? t['agents.view_title'] : t['agents.edit_title']}: ${editor.name}`

  return (
    <Modal onClose={onClose} className="memory-edit-modal">
      <ModalHead title={title} onClose={onClose} />
      <div className="run-modal-body" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {isNew && (
          <label style={{ fontSize: 13 }}>
            <span style={{ display: 'block', marginBottom: 4, color: 'var(--text2)' }}>
              {t['agents.new_name_label']}
            </span>
            <input
              type="text"
              className="doc-textarea"
              style={{ height: 'auto', padding: '6px 8px', fontSize: 13 }}
              placeholder={t['agents.new_name_placeholder']}
              value={editor.name}
              autoFocus
              onChange={ev => {
                const nextName = ev.target.value
                // Keep the frontmatter `name:` in lockstep with the field ONLY while the
                // body is still the pristine template — once the operator edits the body
                // directly, stop touching it (don't clobber their own text).
                const pristine = editor.content === newRoleTemplate(editor.name)
                onChange({ name: nextName, content: pristine ? newRoleTemplate(nextName) : editor.content })
              }}
            />
          </label>
        )}
        {/* FIXES.md F-2: "+ New role" was hard-locked to project scope — there was no
            cockpit path to create a role shared by every project. Project stays
            preselected; choosing global is explicit and labelled, never silent. */}
        {isNew && (
          <div className="agents-scope-select" role="radiogroup" aria-label={t['agents.new_scope_label']}>
            <span>{t['agents.new_scope_label']}</span>
            <label className="agents-scope-option">
              <input
                type="radio"
                name="agents-new-role-scope"
                checked={editor.scope === 'project'}
                onChange={() => onChange({ scope: 'project' })}
              />
              {t['agents.scope_project']}
            </label>
            <label className="agents-scope-option">
              <input
                type="radio"
                name="agents-new-role-scope"
                checked={editor.scope === 'global'}
                onChange={() => onChange({ scope: 'global' })}
              />
              {t['agents.scope_global']}
            </label>
          </div>
        )}
        {editor.scope === 'global' && (
          <p className="agents-scope-global-warning">⚠ {t['agents.scope_global_warning']}</p>
        )}
        <label style={{ fontSize: 13 }}>
          <span style={{ display: 'block', marginBottom: 4, color: 'var(--text2)' }}>
            {t['agents.content_label']}
          </span>
          {editor.loading ? (
            <Spinner label={t['agents.loading']} />
          ) : (
            <textarea
              className="doc-textarea"
              style={{ minHeight: 320 }}
              spellCheck={false}
              readOnly={isView}
              value={editor.content}
              onChange={ev => onChange({ content: ev.target.value })}
              onKeyDown={ev => {
                if (!isView && (ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') { ev.preventDefault(); onSave() }
                else if (ev.key === 'Escape') { ev.preventDefault(); onClose() }
              }}
            />
          )}
        </label>
        {editor.error && <div className="error-state" style={{ fontSize: 12 }}>⚠ {editor.error}</div>}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="doc-btn ghost" onClick={onClose} disabled={editor.saving}>
            {isView ? t['common.close'] : t['common.cancel']}
          </button>
          {!isView && (
            <button className="doc-btn primary" onClick={onSave} disabled={editor.saving || editor.loading}>
              {editor.saving ? t['agents.saving'] : t['agents.save_btn']}
            </button>
          )}
        </div>
        {isView && <p className="agents-hint">{t['agents.builtin_note']}</p>}
      </div>
    </Modal>
  )
}

// ── One sub-agent role row ─────────────────────────────────────────────────

function RoleRow({ role, busy, onToggle, onEdit, onView, onCopy, onDelete }: {
  role: RoleJSON
  busy: boolean
  onToggle: (role: RoleJSON) => void
  onEdit: (role: RoleJSON) => void
  onView: (role: RoleJSON) => void
  onCopy: (role: RoleJSON) => void
  onDelete: (role: RoleJSON) => void
}) {
  const isBuiltin = role.scope === 'builtin'
  const isShadowed = !!role.shadowed_by
  const toolCount = role.tools === null ? null : role.tools.length
  const toolsLabel = toolCount === null
    ? t['agents.tools_all']
    : (toolCount === 0 ? t['agents.tools_none'] : `${toolCount} ${t['agents.tools_suffix']}`)

  // FX4/N5 (A2-fix-audit.md): the previous round hard-disabled both controls on EVERY
  // shadowed row, including a builtin row shadowed only by a GLOBAL role — whose own
  // checkbox/copy were NOT disabled and whose write hits a cockpit-wide file. That traded a
  // one-project hazard for a cockpit-wide one. Neither control here is destructive to a
  // SHADOWING project file any more: a builtin-row toggle is redirected by `set_enabled` to
  // the project copy and only flips the `enabled:` key, preserving the rest byte-for-byte
  // (roles.py `_toggle_enabled_text`) — never the shadowed file's content. Copy's real
  // content-overwrite risk (builtin/global → an EXISTING project file) is now gated by the
  // server's 409 + an explicit ConfirmModal naming the file (see `handleCopy` below), not by
  // disabling the button on a stale client snapshot. The row stays visually muted when
  // shadowed (CSS-only, informational) so the operator still sees which file is effective.
  const toggleTitle = t['agents.enabled_aria']
  const copyTitle = t['agents.copy_btn_aria']

  return (
    <div className={`agents-role-row${(isShadowed || !role.enabled) ? ' agents-role-row--muted' : ''}`}>
      <label className="agents-role-toggle">
        <input
          type="checkbox"
          checked={role.enabled}
          disabled={busy}
          onChange={() => onToggle(role)}
          aria-label={toggleTitle}
          title={toggleTitle}
        />
      </label>
      <div className="agents-role-main">
        <div className="agents-role-name-row">
          <span className="agents-role-name">{role.name}</span>
          <span className={`agents-role-scope-badge agents-role-scope-badge--${role.scope}`}>
            {t[`agents.scope_${role.scope}` as const]}
          </span>
          {isShadowed && role.shadowed_by && (
            <span className="agents-role-shadowed">
              {t['agents.shadowed_by']} {t[`agents.scope_${role.shadowed_by}` as const]}
            </span>
          )}
        </div>
        {role.description && (
          <div className="agents-role-desc" title={role.description}>{role.description}</div>
        )}
        <div className="agents-role-meta">
          <span>{role.model || t['agents.model_none']}</span>
          <span>·</span>
          <span>{role.effort || t['agents.model_none']}</span>
          <span>·</span>
          <span>{toolsLabel}</span>
        </div>
        {/* F11: parse_role's name/filename-mismatch warnings — shipped but never rendered.
            Yellow, not red: a warning is not a parse failure (those are in .agents-errors). */}
        {role.warnings.length > 0 && (
          <div className="agents-role-warnings">
            {role.warnings.map((w, i) => (
              <div key={i} className="agents-role-warning-msg">⚠ {w}</div>
            ))}
          </div>
        )}
      </div>
      <div className="agents-role-actions">
        {isBuiltin ? (
          <button
            className="memory-action-btn"
            title={t['agents.view_btn_aria']}
            aria-label={t['agents.view_btn_aria']}
            onClick={() => onView(role)}
          ><Eye size={13} /></button>
        ) : (
          <button
            className="memory-action-btn"
            title={t['agents.edit_btn_aria']}
            aria-label={t['agents.edit_btn_aria']}
            onClick={() => onEdit(role)}
          ><Pencil size={13} /></button>
        )}
        {role.scope !== 'project' && (
          <button
            className="memory-action-btn"
            title={copyTitle}
            aria-label={copyTitle}
            disabled={busy}
            onClick={() => onCopy(role)}
          ><Copy size={13} /></button>
        )}
        {!isBuiltin && (
          <button
            className="memory-action-btn memory-action-btn--danger"
            title={t['agents.delete_btn_aria']}
            aria-label={t['agents.delete_btn_aria']}
            onClick={() => onDelete(role)}
          ><Trash2 size={13} /></button>
        )}
      </div>
    </div>
  )
}

// ── Main tab ─────────────────────────────────────────────────────────────────

export function AgentsTab({ projectId }: Props) {
  const [data, setData] = useState<ProjectRoles | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<{ message: string; status?: number } | null>(null)

  const [editor, setEditor] = useState<EditorState | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<{ name: string; scope: RoleScope } | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [busyRole, setBusyRole] = useState<string | null>(null)

  // FX4/N5: toggling a GLOBAL-scope row writes `global_dir/<name>.md` directly — every
  // project on this cockpit is affected, not just this one. Holds the row pending the
  // operator's confirmation; null means no such confirm is open.
  const [globalToggleTarget, setGlobalToggleTarget] = useState<RoleJSON | null>(null)

  // FX4/N5 + I1k: "+ New role" and "Copy to project" can now hit a 409 from the backend's
  // overwrite guard (an existing file at the target name/scope) instead of the removed
  // client-side `data.roles` pre-check, which could read a stale snapshot (a second cockpit
  // window, an old tab). Holds what to retry with `overwrite:true` once the operator agrees.
  const [overwriteConfirm, setOverwriteConfirm] = useState<
    { kind: 'new' | 'copy'; name: string; scope: RoleScope; content: string; filePath: string } | null
  >(null)
  const [overwriteSaving, setOverwriteSaving] = useState(false)

  const [mainEditing, setMainEditing] = useState(false)
  const [mainDraft, setMainDraft] = useState('')
  const [mainLoadingDraft, setMainLoadingDraft] = useState(false)
  const [mainSaving, setMainSaving] = useState(false)
  const [mainError, setMainError] = useState('')

  const { toasts, showToast, dismiss } = useToast()

  const reload = useCallback(() => {
    api.roles(projectId).then(d => { setData(d); setLoadError(null) })
      .catch(e => setLoadError({ message: errMsg(e), status: errStatus(e) }))
  }, [projectId])

  useEffect(() => {
    let cancelled = false
    setLoading(true); setLoadError(null); setData(null)
    api.roles(projectId).then(d => {
      if (!cancelled) { setData(d); setLoading(false) }
    }).catch(e => {
      if (!cancelled) { setLoadError({ message: errMsg(e), status: errStatus(e) }); setLoading(false) }
    })
    return () => { cancelled = true }
  }, [projectId])

  useOnRunEnd(reload)
  useFocusRefresh(reload)

  // ── Main-agent block ──────────────────────────────────────────────────────

  async function openMainEditor() {
    setMainError('')
    setMainEditing(true)
    setMainLoadingDraft(true)
    try {
      if (data?.main) {
        const file = await api.role(projectId, 'main', data.main.scope)
        setMainDraft(file.content)
      } else {
        setMainDraft(MAIN_ROLE_TEMPLATE)
      }
    } catch (e) {
      setMainDraft(MAIN_ROLE_TEMPLATE)
      setMainError(errMsg(e))
    } finally {
      setMainLoadingDraft(false)
    }
  }

  async function saveMain() {
    setMainSaving(true); setMainError('')
    try {
      // Always writes the PROJECT's own main.md — editing from inside a project never
      // touches a global default (D2: project overrides global, this tab only owns the
      // project side of that override). `overwrite:true`: this IS the project's own file
      // the operator just opened and edited (the draft was loaded from it, or it is the
      // first save of a fresh template) — the same "self-edit is always intentional"
      // reasoning as `saveEditor`'s 'edit' mode below. Without it, every re-save of an
      // already-existing main.md would hit the backend's new overwrite-guard 409.
      await api.saveRole(projectId, 'main', 'project', mainDraft, true)
      setMainEditing(false)
      reload()
    } catch (e) {
      setMainError(errMsg(e))
    } finally {
      setMainSaving(false)
    }
  }

  // ── Sub-agent role actions ────────────────────────────────────────────────

  function roleFilePath(scope: RoleScope, name: string): string {
    const dir = scope === 'global' ? data?.global_dir : data?.project_dir
    return dir ? `${dir}/${name}.md` : `${name}.md`
  }

  // The actual write. `handleToggle` below is the gate in front of it — a builtin row
  // redirects here unconditionally (safe: see RoleRow's comment), a global row only
  // reaches this after the operator confirms the cockpit-wide warning, a project row was
  // always self-contained.
  async function doToggle(role: RoleJSON) {
    const key = rowKey(role)
    setBusyRole(key)
    try {
      await api.setRoleEnabled(projectId, role.name, role.scope, !role.enabled)
      reload()
    } catch (e) {
      showToast(errMsg(e), 'error')
    } finally {
      setBusyRole(prev => prev === key ? null : prev)
    }
  }

  function handleToggle(role: RoleJSON) {
    // FX4/N5: `set_enabled` writes the GLOBAL file directly for scope="global" (unlike
    // "builtin", which it always redirects to a project copy) — so this is the one toggle
    // whose blast radius is every project on the cockpit, and it fires with no confirm
    // today. Gate it; builtin/project toggles proceed immediately (see RoleRow's comment
    // for why those two are non-destructive as-is).
    if (role.scope === 'global') { setGlobalToggleTarget(role); return }
    doToggle(role)
  }

  async function handleCopy(role: RoleJSON) {
    const key = rowKey(role)
    setBusyRole(key)
    try {
      const file = await api.role(projectId, role.name, role.scope)
      try {
        await api.saveRole(projectId, role.name, 'project', file.content)
        reload()
      } catch (e) {
        if (errStatus(e) === 409) {
          // A project-scope file for this name already exists (a prior override, possibly
          // created after this tab's last load) — name it and let the operator decide,
          // instead of the stale `role.shadowed_by` snapshot the previous round trusted.
          setOverwriteConfirm({
            kind: 'copy', name: role.name, scope: 'project', content: file.content,
            filePath: roleFilePath('project', role.name),
          })
        } else {
          throw e
        }
      }
    } catch (e) {
      showToast(errMsg(e), 'error')
    } finally {
      setBusyRole(prev => prev === key ? null : prev)
    }
  }

  async function confirmOverwrite() {
    if (!overwriteConfirm) return
    setOverwriteSaving(true)
    try {
      await api.saveRole(projectId, overwriteConfirm.name, overwriteConfirm.scope, overwriteConfirm.content, true)
      if (overwriteConfirm.kind === 'new') setEditor(null)
      setOverwriteConfirm(null)
      reload()
    } catch (e) {
      showToast(errMsg(e), 'error')
      setOverwriteConfirm(null)
    } finally {
      setOverwriteSaving(false)
    }
  }

  function requestDelete(role: RoleJSON) {
    setDeleteTarget({ name: role.name, scope: role.scope })
  }

  async function confirmDelete() {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await api.deleteRole(projectId, deleteTarget.name, deleteTarget.scope)
      reload()
    } catch (e) {
      showToast(errMsg(e), 'error')
    } finally {
      setDeleting(false)
      setDeleteTarget(null)
    }
  }

  function openNewRole() {
    setEditor({ mode: 'new', name: '', scope: 'project', content: newRoleTemplate(''), loading: false, saving: false, error: '' })
  }

  // openEdit/openView were the same 10 lines twice with only the mode string differing —
  // collapsed into one. F14: GET /roles/{name} returns {role:null, content, error} for a
  // file that exists but fails to parse; surface that `error` as the editor's own error
  // instead of silently loading a blank-error round-trip (the raw content still loads,
  // matching webapp.py's own "the UI still needs the raw text to fix it").
  async function openEditor(role: RoleJSON, mode: 'edit' | 'view') {
    setEditor({ mode, name: role.name, scope: role.scope, content: '', loading: true, saving: false, error: '' })
    try {
      const file = await api.role(projectId, role.name, role.scope)
      setEditor(prev => (prev && prev.mode === mode && prev.name === role.name && prev.scope === role.scope)
        ? { ...prev, content: file.content, loading: false, error: file.role === null ? (file.error || '') : '' }
        : prev)
    } catch (e) {
      setEditor(prev => prev ? { ...prev, loading: false, error: errMsg(e) } : prev)
    }
  }

  async function saveEditor() {
    if (!editor) return
    const name = editor.name.trim()
    // editor.scope is the operator's explicit choice from the scope selector above
    // (FIXES.md F-2) for 'new', or the role's own (already-existing) scope for 'edit' —
    // either way it is never silently reassigned here.
    if (editor.mode === 'new') {
      if (name === 'main') { setEditor(e => e ? { ...e, error: t['agents.name_reserved'] } : e); return }
      if (!ROLE_NAME_RE.test(name)) { setEditor(e => e ? { ...e, error: t['agents.name_invalid'] } : e); return }
      // FX4/N5 + I1k: the client-side `data.roles` collision check this replaced read a
      // possibly stale GET snapshot (a second cockpit window or tab defeats it). The
      // backend's write endpoint is the single source of truth for "does this file already
      // exist" — a 409 below opens the overwrite confirm instead.
    }
    setEditor(e => e ? { ...e, saving: true, error: '' } : e)
    try {
      // 'edit' mode always targets the role's OWN already-known file — the operator opened
      // exactly this file, so the save is an intentional self-overwrite every time (same
      // reasoning as `saveMain`). Only 'new' mode can collide with a file the operator
      // never saw, so only 'new' omits `overwrite` and lets a 409 drive the confirm below.
      await api.saveRole(projectId, name, editor.scope, editor.content, editor.mode === 'edit')
      setEditor(null)
      reload()
    } catch (e) {
      if (editor.mode === 'new' && errStatus(e) === 409) {
        setEditor(prev => prev ? { ...prev, saving: false } : prev)
        setOverwriteConfirm({
          kind: 'new', name, scope: editor.scope, content: editor.content,
          filePath: roleFilePath(editor.scope, name),
        })
        return
      }
      setEditor(prev => prev ? { ...prev, saving: false, error: errMsg(e) } : prev)
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  if (loading) return <Spinner label={t['agents.loading']} />

  if (loadError) {
    return (
      <div className="agents-tab-error">
        <div className="error-state">
          ⚠ {t['agents.unavailable']}{loadError.status ? ` (HTTP ${loadError.status})` : ''}
        </div>
        <button className="doc-btn ghost" onClick={reload}>{t['agents.retry']}</button>
      </div>
    )
  }

  if (!data) return null

  const roles = (data.roles || []).filter(r => !r.is_main)
  const sortedRoles = [...roles].sort((a, b) => {
    const activeA = (!a.shadowed_by && a.enabled) ? 0 : 1
    const activeB = (!b.shadowed_by && b.enabled) ? 0 : 1
    if (activeA !== activeB) return activeA - activeB
    return a.name.localeCompare(b.name)
  })
  const errors = data.errors || []

  return (
    <div className="agents-tab">
      {/* ── Main agent — visually distinct, sits at the top ── */}
      <section className="agents-main-block">
        <div className="agents-main-head">
          <h3 className="agents-main-title">{t['agents.main_title']}</h3>
          {!mainEditing && (
            <button
              className="doc-btn ghost"
              style={{ padding: '2px 10px', fontSize: 12 }}
              onClick={openMainEditor}
              aria-label={t['agents.main_edit_aria']}
            >{t['agents.main_edit_btn']}</button>
          )}
        </div>
        <p className="agents-hint">{t['agents.main_hint']}</p>
        {!mainEditing && data.main && data.main.scope !== 'project' && (
          <p className="agents-main-inherited">{t['agents.main_inherited']}</p>
        )}
        {mainError && <div className="error-state" style={{ fontSize: 12 }}>⚠ {mainError}</div>}
        {mainEditing ? (
          <div className="agents-main-editor">
            {mainLoadingDraft ? (
              <Spinner label={t['agents.loading']} />
            ) : (
              <textarea
                className="doc-textarea"
                spellCheck={false}
                autoFocus
                value={mainDraft}
                onChange={e => setMainDraft(e.target.value)}
                onKeyDown={e => {
                  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); saveMain() }
                  else if (e.key === 'Escape') { e.preventDefault(); setMainEditing(false); setMainError('') }
                }}
              />
            )}
            <div className="agents-main-editor-actions">
              <button
                className="doc-btn ghost"
                onClick={() => { setMainEditing(false); setMainError('') }}
                disabled={mainSaving}
              >{t['common.cancel']}</button>
              <button
                className="doc-btn primary"
                onClick={saveMain}
                disabled={mainSaving || mainLoadingDraft}
              >{mainSaving ? t['agents.saving'] : t['agents.save_btn']}</button>
            </div>
          </div>
        ) : (
          data.main
            ? <pre className="agents-main-preview">{data.main.prompt}</pre>
            : <div className="no-content">{t['agents.main_empty']}</div>
        )}
      </section>

      {/* ── Parse errors ── */}
      {errors.length > 0 && (
        <section className="agents-errors">
          <div className="agents-errors-title">⚠ {t['agents.errors_title']}</div>
          {errors.map(e => (
            <div key={`${e.scope}:${e.name}`} className="agents-error-row">
              <span className="agents-error-name" title={e.path}>{e.path || e.name}</span>
              <span className="agents-error-scope">{t[`agents.scope_${e.scope}` as const]}</span>
              <span className="agents-error-msg">{e.error}</span>
            </div>
          ))}
        </section>
      )}

      {/* ── Sub-agent roles ── */}
      <section className="agents-roles-block">
        <div className="agents-roles-head">
          <h3 className="agents-roles-title">{t['agents.roles_title']}</h3>
          <button
            className="doc-btn primary"
            style={{ padding: '2px 10px', fontSize: 12 }}
            onClick={openNewRole}
            aria-label={t['agents.new_btn_aria']}
          >{t['agents.new_btn']}</button>
        </div>
        {sortedRoles.length === 0 ? (
          <div className="no-content">{t['agents.roles_empty']}</div>
        ) : (
          <div className="agents-role-list">
            {sortedRoles.map(r => (
              <RoleRow
                key={rowKey(r)}
                role={r}
                busy={busyRole === rowKey(r)}
                onToggle={handleToggle}
                onEdit={role => openEditor(role, 'edit')}
                onView={role => openEditor(role, 'view')}
                onCopy={handleCopy}
                onDelete={requestDelete}
              />
            ))}
          </div>
        )}
      </section>

      {editor && (
        <RoleEditorModal
          editor={editor}
          onChange={patch => setEditor(prev => prev ? { ...prev, ...patch } : prev)}
          onSave={saveEditor}
          onClose={() => setEditor(null)}
        />
      )}

      {deleteTarget && (
        <ConfirmModal
          title={t['agents.confirm_delete_title']}
          message={deleteTarget.scope === 'global' ? t['agents.confirm_delete_global_body'] : t['agents.confirm_delete_body']}
          confirmLabel={deleting ? '…' : t['agents.confirm_delete_yes']}
          onConfirm={confirmDelete}
          onCancel={() => setDeleteTarget(null)}
          danger
        />
      )}

      {/* FX4/N5, fix item 2: a global-scope toggle affects every project on this cockpit —
          reuses the SAME warning string as the editor/delete dialogs, not a new one. */}
      {globalToggleTarget && (
        <ConfirmModal
          title={t['agents.confirm_global_toggle_title']}
          message={t['agents.scope_global_warning']}
          onConfirm={() => { const role = globalToggleTarget; setGlobalToggleTarget(null); doToggle(role) }}
          onCancel={() => setGlobalToggleTarget(null)}
        />
      )}

      {/* FX4/N5, fix items 1+3: the backend's overwrite guard (409) is what actually knows
          whether the target file exists — not a client snapshot. Names the file; when the
          target scope is global, appends the same cockpit-wide warning as above. */}
      {overwriteConfirm && (
        <ConfirmModal
          title={t['agents.confirm_overwrite_title']}
          message={
            overwriteConfirm.scope === 'global'
              ? `${t['agents.confirm_overwrite_body'].replace('{file}', overwriteConfirm.filePath)} ${t['agents.scope_global_warning']}`
              : t['agents.confirm_overwrite_body'].replace('{file}', overwriteConfirm.filePath)
          }
          confirmLabel={overwriteSaving ? '…' : t['agents.confirm_overwrite_yes']}
          onConfirm={confirmOverwrite}
          onCancel={() => setOverwriteConfirm(null)}
          danger
        />
      )}

      <ToastContainer toasts={toasts} onDismiss={dismiss} />
    </div>
  )
}
