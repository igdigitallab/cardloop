/** The body of a project-settings save.
 *
 *  The server validates `context_pack_enabled` as a strict boolean and has no stored "inherit"
 *  (null) — yet `GET .../settings` returns null for every project that never chose, and the
 *  Settings tab posts the whole record back. An untouched project therefore 400'd the WHOLE save
 *  ("context_pack_enabled: expected bool"), so nothing on the page could be saved on a default
 *  project — including the Grok privacy opt-in (`grok_allowed`).
 *
 *  A null the operator did not touch is left out. A null they DID choose (On/Off -> Inherit) is
 *  still sent, so the server's answer tells them it cannot be stored rather than the choice being
 *  dropped silently. `saved` = the settings as the server last returned them. */
import type { ProjectSettings } from '../types'

export function projectSettingsPayload(
  next: ProjectSettings,
  saved: Readonly<Pick<ProjectSettings, 'context_pack_enabled'>> | null,
): ProjectSettings {
  const out = { ...next }
  const savedNull = saved == null || saved.context_pack_enabled == null
  if (out.context_pack_enabled == null && savedNull) delete out.context_pack_enabled
  return out
}
