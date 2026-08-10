import type { CapacitorConfig } from '@capacitor/cli'

// Cardloop ships self-hosted: each install points at its own server, chosen
// at first run by the user (see src/native/ServerSetup.tsx), not baked in here.
// allowNavigation: '*' lets the WebView follow that redirect — the app then
// runs as an ordinary page served BY that instance, same as the browser PWA.
const config: CapacitorConfig = {
  appId: 'com.igdigitallab.cardloop',
  appName: 'Cardloop',
  webDir: 'dist',
  server: {
    androidScheme: 'https',
    allowNavigation: ['*'],
  },
}

export default config
