/** localStorage keys and the URL-fragment parameter shared by the native server
 *  gate. Kept apart from the components so importing a key never drags a React
 *  component along (and so Fast Refresh stays intact in the component files). */

/** The self-hosted instance this install points at. */
export const STORAGE_KEY = 'cops.native.serverUrl'
/** The app bundle's OWN origin (Capacitor's local scheme), recorded before we
 *  navigate away: from the server's origin there is otherwise no way back to the
 *  bundle that holds the server picker. */
export const APP_ORIGIN_KEY = 'cops.native.appOrigin'
/** Set by the server-side "Change server" control and consumed by the gate — the
 *  request to show the picker instead of redirecting straight back. */
export const FORCE_SETUP_KEY = 'cops.native.forceSetup'
/** One-shot passphrase handed to the server's own origin through the URL fragment. */
export const AUTH_HANDOFF_PARAM = 'cops-auth'
