// https://vitejs.dev/config/ | https://vite-pwa-org.netlify.app/guide/inject-manifest.html
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const backendUrl = env.VITE_BACKEND_URL || 'http://localhost:8787'

  return {
    plugins: [
      react(),
      VitePWA({
        // Use injectManifest so we can ship a custom SW with Web-Push handlers.
        // The plugin compiles src/sw.ts, injects the precache manifest, and emits
        // dist/sw.js. A later agent only needs to add the push subscription server.
        strategies: 'injectManifest',
        srcDir: 'src',
        filename: 'sw.ts',

        // Plugin auto-registers the SW (inserts a small script in the HTML output).
        registerType: 'autoUpdate',
        injectRegister: 'auto',

        // Override PWA scope to '/' so the SW controls the whole origin, regardless
        // of Vite's base: './' which is only for JS/CSS asset paths.
        // The plugin's own `scope` and `base` options are independent of vite.base.
        scope: '/',
        base: '/',

        // The plugin generates manifest.webmanifest and injects its <link> into
        // index.html automatically. The static public/manifest.json has been
        // removed to prevent a duplicate manifest link.
        manifest: {
          name: 'Cardloop',
          short_name: 'Cardloop',
          description: 'Cardloop — project management cockpit powered by the Claude Agent SDK',
          start_url: '/',
          scope: '/',
          display: 'standalone',
          background_color: '#0e0e13',
          theme_color: '#0e0e13',
          icons: [
            {
              src: 'icons/icon-192.png',
              sizes: '192x192',
              type: 'image/png',
              purpose: 'any maskable',
            },
            {
              src: 'icons/icon-512.png',
              sizes: '512x512',
              type: 'image/png',
              purpose: 'any maskable',
            },
          ],
        },

        // injectManifest: workbox-build processes src/sw.ts and injects the precache
        // manifest at self.__WB_MANIFEST. Increase the size limit so large assets
        // are not silently skipped from precaching.
        injectManifest: {
          maximumFileSizeToCacheInBytes: 5 * 1024 * 1024,
        },

        devOptions: {
          enabled: false,
          type: 'module',
        },
      }),
    ],
    base: './',
    build: {
      outDir: 'dist',
    },
    server: {
      proxy: {
        '/api': {
          target: backendUrl,
          changeOrigin: true,
        },
      },
    },
  }
})
