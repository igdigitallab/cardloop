/** localStorage keys and the URL-fragment parameter shared by the native server
 *  gate. Kept apart from the components so importing a key never drags a React
 *  component along (and so Fast Refresh stays intact in the component files). */

/** The self-hosted instance this install points at. */
export const STORAGE_KEY = 'cops.native.serverUrl'
/** Query flag the server-side "Change server" control appends when sending the
 *  WebView back to the bundle: "open the picker, do not redirect straight back".
 *  It rides in the URL and NOT in localStorage because localStorage is per-origin —
 *  a flag written on the server's origin is invisible on the bundle's. */
export const SETUP_QUERY_PARAM = 'cops-setup'
/** Hostname the Capacitor bundle is served from. Capacitor's Android default, and
 *  capacitor.config.ts deliberately sets no custom `server.hostname` — the test
 *  `test_bundle_host_matches_capacitor_config` fails if that ever changes.
 *  This is how we tell "we are on the app bundle" from "we are on a real server". */
export const APP_BUNDLE_HOST = 'localhost'
/** One-shot passphrase handed to the server's own origin through the URL fragment. */
export const AUTH_HANDOFF_PARAM = 'cops-auth'
