import { useCallback, useEffect, useRef, useState } from 'react'
import { api, apiErrorMessage } from '../api'
import type { HealthFinding, ProjectHealthCheck } from '../types'

export interface ProjectHealthCheckState {
  data: ProjectHealthCheck | null
  /** A request is in flight (the modal's Re-check shows it). */
  checking: boolean
  /** Re-run the checks now (the Tests button, the modal's Re-check). */
  recheck: () => void
  /** Acknowledge one finding; resolves to an error message, or null on success. */
  acknowledge: (f: HealthFinding) => Promise<string | null>
}

/**
 * Project health check for one project: one cheap GET on open (the server reuses a result
 * younger than ~5 min), a fresh run on demand.  Any failure leaves `data` null, which hides
 * the pill — the endpoint is absent when the module is switched off, and that is not an error.
 */
export function useProjectHealthCheck(projectId: string): ProjectHealthCheckState {
  const [data, setData] = useState<ProjectHealthCheck | null>(null)
  const [checking, setChecking] = useState(false)
  // Newest request wins: a slow response must not overwrite a fresher one.
  const seq = useRef(0)

  const load = useCallback(async (fresh: boolean) => {
    const mine = ++seq.current
    setChecking(true)
    try {
      const d = await api.projectHealthCheck(projectId, fresh)
      if (mine === seq.current) setData(d)
    } catch { /* module off (404) or request failed: keep what we had */ }
    finally { if (mine === seq.current) setChecking(false) }
  }, [projectId])

  useEffect(() => {
    setData(null)
    void load(false)
  }, [load])

  const recheck = useCallback(() => { void load(true) }, [load])

  const acknowledge = useCallback(async (f: HealthFinding): Promise<string | null> => {
    if (!f.ackable || !f.ack_sha256) return 'This finding cannot be acknowledged.'
    try {
      const d = await api.ackHealthFinding(projectId, f.id, f.ack_sha256)
      seq.current++
      setData(d)
      return null
    } catch (e) {
      // 409: the file changed since it was listed. The body is the fresh result — show it.
      const err = e as { status?: number; body?: unknown }
      const body = err.body as Partial<ProjectHealthCheck> | null | undefined
      if (err.status === 409 && body && Array.isArray(body.findings)) {
        seq.current++
        setData(body as ProjectHealthCheck)
        return 'The file changed since it was listed - review it again.'
      }
      return apiErrorMessage(e)
    }
  }, [projectId])

  return { data, checking, recheck, acknowledge }
}
