// `npm test` — runs every src/**/*.test.ts(x) under Node's built-in runner, the recipe each
// file's header documents (bundle with esbuild, then `node --test`), for all of them at once.
// Bundling (not file-by-file tsc) because the modules under test import React, the api client
// and each other. CI runs this; a test file is picked up by its name alone.
import { build } from 'esbuild'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const webDir = fileURLToPath(new URL('..', import.meta.url))
const srcDir = join(webDir, 'src')

const entries = readdirSync(srcDir, { recursive: true })
  .filter(p => /\.test\.tsx?$/.test(p))
  .map(p => join(srcDir, p))
  .sort()
if (entries.length === 0) {
  console.error('node-tests: no src/**/*.test.ts(x) found')
  process.exit(1)
}

const outDir = mkdtempSync(join(tmpdir(), 'cardloop-web-tests-'))
let status = 1
try {
  await build({
    entryPoints: entries,
    outdir: outDir,
    outbase: srcDir,
    outExtension: { '.js': '.cjs' },
    bundle: true,
    platform: 'node',
    format: 'cjs',
    logLevel: 'warning',
  })
  const bundles = entries.map(e => join(outDir, relative(srcDir, e).replace(/\.tsx?$/, '.cjs')))
  status = spawnSync(process.execPath, ['--test', ...bundles], { stdio: 'inherit' }).status ?? 1
} finally {
  rmSync(outDir, { recursive: true, force: true })
}
process.exit(status)
