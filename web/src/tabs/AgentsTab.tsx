import { useCallback, useEffect, useState } from 'react'
import { Pencil, Trash2, Copy } from 'lucide-react'
import { api } from '../api'
import { ProjectRoles, RoleJSON, RoleScope } from '../types'
import { Spinner } from '../components/Spinner'
import { ConfirmModal } from '../components/ConfirmModal'
import { Modal, ModalHead } from '../components/Modal'
import { EditableMarkdown } from '../components/EditableMarkdown'
import { useToast, ToastContainer } from '../components/Toast'
import { useOnRunEnd, useFocusRefresh } from '../hooks/useProjectActivity'
import { t } from '../i18n'

// spec-091 Phase 1 — cockpit UI for the declarative agent-role registry.
// Contract: docs/internal/specs/spec-091-agent-roles/IMPLEMENTATION.md §4-6.
// Mirrors MemoryTab.tsx (list → modal editor → save/delete) — same components,
// same "edit the file" mental model, just with role-specific metadata rows.
//
// FX5 (tab-merge): the standalone `main.md` role concept is gone — CLAUDE.md was already
// the main agent's own instruction file, so this tab's "main agent" block is now CLAUDE.md
// itself (EditableMarkdown, same load/save the old ClaudeMdTab used), not a second prompt
// appended to it. The backend no longer returns `main`/`is_main` on this contract at all.

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

// ── Editor modal (new / edit — every role opens editable, FX5 decision 2) ─────

type EditorMode = 'new' | 'edit'

interface EditorState {
  mode: EditorMode
  name: string
  scope: RoleScope   // 'new': the operator's chosen scope. 'edit': the role's OWN (source) scope.
  content: string
  loading: boolean
  saving: boolean
  error: string
}

/** Where a Save actually lands. Editing a builtin role can never write the builtin file
 * (the backend refuses `scope=builtin` writes) — FX5 decision 2 forks it to the project
 * scope instead. Global/project edits stay in place. 'new' always targets the operator's
 * explicit scope choice. Single source of truth shared by the modal's own display and
 * AgentsTab's actual save call, so the two can never disagree about where a write goes. */
function saveTargetScope(editor: Pick<EditorState, 'mode' | 'scope'>): RoleScope {
  return editor.mode === 'edit' && editor.scope === 'builtin' ? 'project' : editor.scope
}

function RoleEditorModal({ editor, onChange, onSave, onClose }: {
  editor: EditorState
  onChange: (patch: Partial<EditorState>) => void
  onSave: () => void
  onClose: () => void
}) {
  const isNew = editor.mode === 'new'
  const isFork = !isNew && editor.scope === 'builtin'
  const targetScope = saveTargetScope(editor)
  const title = isNew ? t['agents.new_btn'] : `${t['agents.edit_title']}: ${editor.name}`

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
        {/* FX5 decision 2: "before saving, where the save will land" — shown for every
            edit of an already-existing role (never for 'new', whose scope selector above
            already IS that answer). A builtin source always forks to project; global/
            project edits stay on their own scope, shown here too for at-a-glance parity. */}
        {!isNew && (
          <div className="agents-editor-target-row">
            <span className="agents-hint">{t['agents.save_target_label']}</span>
            <span className={`agents-role-scope-badge agents-role-scope-badge--${targetScope}`}>
              {t[`agents.scope_${targetScope}` as const]}
            </span>
          </div>
        )}
        {isFork && <p className="agents-hint">{t['agents.builtin_note']}</p>}
        {targetScope === 'global' && (
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
              value={editor.content}
              onChange={ev => onChange({ content: ev.target.value })}
              onKeyDown={ev => {
                if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') { ev.preventDefault(); onSave() }
                else if (ev.key === 'Escape') { ev.preventDefault(); onClose() }
              }}
            />
          )}
        </label>
        {editor.error && <div className="error-state" style={{ fontSize: 12 }}>⚠ {editor.error}</div>}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="doc-btn ghost" onClick={onClose} disabled={editor.saving}>
            {t['common.cancel']}
          </button>
          <button className="doc-btn primary" onClick={onSave} disabled={editor.saving || editor.loading}>
            {editor.saving ? t['agents.saving'] : t['agents.save_btn']}
          </button>
        </div>
      </div>
    </Modal>
  )
}

// ── One sub-agent role row ─────────────────────────────────────────────────

function RoleRow({ role, busy, onToggle, onEdit, onCopy, onDelete }: {
  role: RoleJSON
  busy: boolean
  onToggle: (role: RoleJSON) => void
  onEdit: (role: RoleJSON) => void
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
        {/* FX5 decision 2: every role opens a real, always-editable editor now — a builtin
            row's Save forks to the project scope (see RoleEditorModal / saveTargetScope)
            instead of opening a dead-end read-only view. */}
        <button
          className="memory-action-btn"
          title={t['agents.edit_btn_aria']}
          aria-label={t['agents.edit_btn_aria']}
          onClick={() => onEdit(role)}
        ><Pencil size={13} /></button>
        {/* FX5: "Copy to project" is now redundant for a BUILTIN row (Edit → Save already
            forks it) — keep it ONLY for a GLOBAL row, the one case edit-in-place can't
            reach: editing a global role saves it back to global, so copying it into this
            project's own override still needs a dedicated one-click action. */}
        {role.scope === 'global' && (
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

  // FX4/N5 + I1k + FX5: "+ New role", "Copy to project" and now an edit-to-fork of a
  // builtin role can all hit a 409 from the backend's overwrite guard (an existing file at
  // the target name/scope) instead of a client-side pre-check, which could read a stale
  // snapshot (a second cockpit window, an old tab). Holds what to retry with
  // `overwrite:true` once the operator agrees. `kind` only decides which open editor (if
  // any) closes on success — 'copy' has none, 'new'/'fork' both do.
  const [overwriteConfirm, setOverwriteConfirm] = useState<
    { kind: 'new' | 'copy' | 'fork'; name: string; scope: RoleScope; content: string; filePath: string } | null
  >(null)
  const [overwriteSaving, setOverwriteSaving] = useState(false)

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
      // 'new' and 'fork' both came from an open editor; 'copy' never opens one.
      if (overwriteConfirm.kind === 'new' || overwriteConfirm.kind === 'fork') setEditor(null)
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

  // FX5: every role — builtin included — now opens the same editable editor; there is no
  // more read-only 'view' mode. F14: GET /roles/{name} returns {role:null, content, error}
  // for a file that exists but fails to parse; surface that `error` as the editor's own
  // error instead of silently loading a blank-error round-trip (the raw content still
  // loads, matching webapp.py's own "the UI still needs the raw text to fix it").
  async function openEditor(role: RoleJSON) {
    setEditor({ mode: 'edit', name: role.name, scope: role.scope, content: '', loading: true, saving: false, error: '' })
    try {
      const file = await api.role(projectId, role.name, role.scope)
      setEditor(prev => (prev && prev.mode === 'edit' && prev.name === role.name && prev.scope === role.scope)
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
    // (FIXES.md F-2) for 'new', or the role's own (already-existing) SOURCE scope for
    // 'edit' — saveTargetScope() below is what decides where the write actually lands.
    if (editor.mode === 'new' && !ROLE_NAME_RE.test(name)) {
      setEditor(e => e ? { ...e, error: t['agents.name_invalid'] } : e)
      return
    }
    const targetScope = saveTargetScope(editor)
    // A project/global 'edit' is always a self-overwrite of the exact file the operator
    // opened (same "self-edit is always intentional" reasoning throughout this tab).
    // Editing a BUILTIN role is never a self-overwrite — it forks into the project scope,
    // which may or may not already hold a file for this name, so it goes through the same
    // 409-driven confirm as 'new' below instead of assuming either way.
    const isSelfEdit = editor.mode === 'edit' && editor.scope !== 'builtin'
    setEditor(e => e ? { ...e, saving: true, error: '' } : e)
    try {
      await api.saveRole(projectId, name, targetScope, editor.content, isSelfEdit)
      setEditor(null)
      reload()
    } catch (e) {
      const canFork = editor.mode === 'new' || (editor.mode === 'edit' && editor.scope === 'builtin')
      if (canFork && errStatus(e) === 409) {
        setEditor(prev => prev ? { ...prev, saving: false } : prev)
        setOverwriteConfirm({
          kind: editor.mode === 'new' ? 'new' : 'fork',
          name, scope: targetScope, content: editor.content,
          filePath: roleFilePath(targetScope, name),
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

  const roles = data.roles || []
  const sortedRoles = [...roles].sort((a, b) => {
    const activeA = (!a.shadowed_by && a.enabled) ? 0 : 1
    const activeB = (!b.shadowed_by && b.enabled) ? 0 : 1
    if (activeA !== activeB) return activeA - activeB
    return a.name.localeCompare(b.name)
  })
  const errors = data.errors || []

  return (
    <div className="agents-tab">
      {/* ── Main agent — CLAUDE.md itself, visually distinct, sits at the top ──
          FX5: the main agent's instructions ARE CLAUDE.md — there is no second file any
          more. Reuses the exact editor + api calls the old standalone CLAUDE.md tab used. */}
      <section className="agents-main-block agents-claude-md-block">
        <div className="agents-main-head">
          <h3 className="agents-main-title">{t['agents.main_title']}</h3>
        </div>
        <p className="agents-hint">{t['agents.main_hint']}</p>
        <EditableMarkdown
          projectId={projectId}
          load={api.claudeMd}
          save={api.saveClaudeMd}
          spinnerLabel={t['claude_md.loading']}
          emptyLabel={t['claude_md.empty']}
        />
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
                onEdit={openEditor}
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

      {/* FX4/N5 + FX5, fix items 1+3: the backend's overwrite guard (409) is what actually
          knows whether the target file exists — not a client snapshot. Names the file; when
          the target scope is global, appends the same cockpit-wide warning as above. */}
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
