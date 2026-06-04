import { useEffect, useState, type ReactNode } from 'react'
import { api } from '../api'
import { ProjectSettings, GlobalSettings, GlobalSettingsEffective } from '../types'
import { Spinner } from '../components/Spinner'
import { SecretsTab } from './SecretsTab'
import { MODELS } from '../lib/models'

interface Props { projectId: string }

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

// Строка «лейбл + контрол» с подсказкой
function Row({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16, padding: '10px 0', borderTop: '1px solid var(--border)' }}>
      <span>
        <b style={{ fontSize: 13 }}>{title}</b>
        {hint && <span style={{ display: 'block', fontSize: 11, color: 'var(--text3)', fontWeight: 400, marginTop: 2, maxWidth: 430 }}>{hint}</span>}
      </span>
      <span style={{ flexShrink: 0, paddingTop: 2 }}>{children}</span>
    </div>
  )
}

export function SettingsTab({ projectId }: Props) {
  const [proj, setProj] = useState<ProjectSettings | null>(null)
  const [glob, setGlob] = useState<GlobalSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [projMsg, setProjMsg] = useState('')
  const [globMsg, setGlobMsg] = useState('')
  const [savingProj, setSavingProj] = useState(false)
  const [savingGlob, setSavingGlob] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true); setError(''); setProj(null); setProjMsg(''); setGlobMsg('')
    Promise.all([api.projectSettings(projectId), api.settings()])
      .then(([p, g]) => { if (!cancelled) { setProj(p); setGlob(g); setLoading(false) } })
      .catch(e => { if (!cancelled) { setError(errMsg(e)); setLoading(false) } })
    return () => { cancelled = true }
  }, [projectId])

  async function saveProj() {
    if (!proj) return
    setSavingProj(true); setProjMsg('')
    try {
      const r = await api.saveProjectSettings(projectId, proj)
      setProj(r.settings); setProjMsg('Сохранено ✓')
    } catch (e) { setProjMsg('⚠ ' + errMsg(e)) }
    finally { setSavingProj(false) }
  }

  async function saveGlob() {
    if (!glob) return
    setSavingGlob(true); setGlobMsg('')
    const ef = glob.effective
    try {
      const r = await api.saveSettings({
        self_heal_enabled: ef.self_heal_enabled,
        self_heal_max_concurrent: ef.self_heal_max_concurrent,
        scan_interval_sec: ef.scan_interval_sec,
        default_model: ef.default_model || '',
        watchdog_stall_sec: ef.watchdog_stall_sec,
        watchdog_max_sec: ef.watchdog_max_sec,
      })
      setGlobMsg(`Сохранено ✓ (${Object.keys(r.stored).length} переопределений)`)
    } catch (e) { setGlobMsg('⚠ ' + errMsg(e)) }
    finally { setSavingGlob(false) }
  }

  if (loading) return <Spinner label="Загрузка настроек…" />
  if (error) return <div className="error-state">⚠ {error}</div>
  if (!proj || !glob) return null

  const e = glob.effective
  const setE = (patch: Partial<GlobalSettingsEffective>) =>
    setGlob({ ...glob, effective: { ...e, ...patch } })

  return (
    <div style={{ maxWidth: 660, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 28, padding: '6px 4px 32px' }}>

      {/* ── Настройки проекта ── */}
      <section>
        <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>Настройки проекта</h3>
        <p style={{ margin: '0 0 6px', fontSize: 12, color: 'var(--text3)' }}>
          Только для этого проекта (хранятся в topics.json, подхватываются на лету).
        </p>

        <Row title="Git-синхронизация"
             hint="Выкл — кокпит не использует git: карточки гоняются прямо в папке (без worktree-веток), кнопка git-sync отключена, health не требует .git. Вся история диалогов сохраняется; существующий .git физически не трогаем.">
          <input type="checkbox" checked={proj.git_enabled}
                 onChange={ev => setProj({ ...proj, git_enabled: ev.target.checked })}
                 aria-label="Git-синхронизация" />
        </Row>

        <Row title="Самолечение"
             hint="Агент-чинильщик при падении тестов (доходит до Review, не авто-применяет). Глобальный master-выключатель ниже перекрывает.">
          <input type="checkbox" checked={proj.self_heal}
                 onChange={ev => setProj({ ...proj, self_heal: ev.target.checked })}
                 aria-label="Самолечение" />
        </Row>

        <Row title="TG-уведомления об ошибках" hint="Пинг в Telegram при новых инцидентах в Failed.">
          <input type="checkbox" checked={proj.notify_on_error}
                 onChange={ev => setProj({ ...proj, notify_on_error: ev.target.checked })}
                 aria-label="TG-уведомления об ошибках" />
        </Row>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '10px 0', borderTop: '1px solid var(--border)' }}>
          <b style={{ fontSize: 13 }}>log_cmd</b>
          <input type="text" className="doc-textarea"
                 style={{ height: 'auto', padding: '6px 8px', fontSize: 13, fontFamily: 'monospace' }}
                 placeholder="journalctl -u my-service -n 300 --no-pager"
                 value={proj.log_cmd} onChange={ev => setProj({ ...proj, log_cmd: ev.target.value })} />
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '10px 0', borderTop: '1px solid var(--border)' }}>
          <b style={{ fontSize: 13 }}>test_cmd</b>
          <input type="text" className="doc-textarea"
                 style={{ height: 'auto', padding: '6px 8px', fontSize: 13, fontFamily: 'monospace' }}
                 placeholder="venv/bin/python -m pytest -q"
                 value={proj.test_cmd} onChange={ev => setProj({ ...proj, test_cmd: ev.target.value })} />
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 12 }}>
          <button className="doc-btn primary" onClick={saveProj} disabled={savingProj}>
            {savingProj ? 'Сохранение…' : 'Сохранить'}
          </button>
          {projMsg && <span style={{ fontSize: 12, color: 'var(--text2)' }}>{projMsg}</span>}
        </div>
      </section>

      {/* ── Глобальные настройки ── */}
      <section>
        <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>Глобальные настройки</h3>
        <p style={{ margin: '0 0 6px', fontSize: 12, color: 'var(--text3)' }}>
          На весь кокпит (data/settings.json). Переопределяют env-дефолты в рантайме.
        </p>

        <Row title="Самолечение — master"
             hint="Выкл — самолечение отключено во ВСЕХ проектах, независимо от их персональной настройки.">
          <input type="checkbox" checked={e.self_heal_enabled}
                 onChange={ev => setE({ self_heal_enabled: ev.target.checked })}
                 aria-label="Самолечение master" />
        </Row>

        <Row title="Макс. параллельных починок" hint="1–10">
          <input type="number" min={1} max={10} style={{ width: 90, padding: '4px 8px', fontSize: 13 }}
                 value={e.self_heal_max_concurrent}
                 onChange={ev => setE({ self_heal_max_concurrent: Number(ev.target.value) })} />
        </Row>

        <Row title="Интервал сканера инцидентов, сек" hint="30–3600">
          <input type="number" min={30} max={3600} style={{ width: 90, padding: '4px 8px', fontSize: 13 }}
                 value={e.scan_interval_sec}
                 onChange={ev => setE({ scan_interval_sec: Number(ev.target.value) })} />
        </Row>

        <Row title="Дефолт-модель новых проектов"
             hint="Применяется при создании нового проекта. Существующие проекты не затрагиваются.">
          <select value={e.default_model} onChange={ev => setE({ default_model: ev.target.value })}>
            {MODELS.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </Row>

        <Row title="Watchdog: тишина, сек" hint="Нет событий дольше → прервать задачу. 30–7200">
          <input type="number" min={30} max={7200} style={{ width: 90, padding: '4px 8px', fontSize: 13 }}
                 value={e.watchdog_stall_sec}
                 onChange={ev => setE({ watchdog_stall_sec: Number(ev.target.value) })} />
        </Row>

        <Row title="Watchdog: потолок задачи, сек" hint="Общий лимит на задачу. 60–14400">
          <input type="number" min={60} max={14400} style={{ width: 90, padding: '4px 8px', fontSize: 13 }}
                 value={e.watchdog_max_sec}
                 onChange={ev => setE({ watchdog_max_sec: Number(ev.target.value) })} />
        </Row>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 12 }}>
          <button className="doc-btn primary" onClick={saveGlob} disabled={savingGlob}>
            {savingGlob ? 'Сохранение…' : 'Сохранить'}
          </button>
          {globMsg && <span style={{ fontSize: 12, color: 'var(--text2)' }}>{globMsg}</span>}
        </div>
      </section>

      {/* ── Секреты проекта ── */}
      <section>
        <h3 style={{ margin: '0 0 4px', fontSize: 15 }}>{'\u{1F511}'} Секреты проекта</h3>
        <p style={{ margin: '0 0 12px', fontSize: 12, color: 'var(--text3)' }}>
          Переменные окружения, доступные агенту при выполнении задач. Не коммитятся в git.
        </p>
        <SecretsTab projectId={projectId} />
      </section>

    </div>
  )
}
