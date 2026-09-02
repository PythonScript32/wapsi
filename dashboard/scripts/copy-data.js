// Copies data/results_*.json from the repo root into public/data/ so Vite
// serves them as static assets (/data/results_{set}.json). Runs before every
// dev server start and every build (see package.json's predev/prebuild) so
// the deployed/dev copy can never go stale relative to the last batch run.
import { copyFileSync, existsSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const srcDir = join(__dirname, '..', '..', 'data')
const destDir = join(__dirname, '..', 'public', 'data')

mkdirSync(destDir, { recursive: true })

for (const name of ['results_dev.json', 'results_holdout.json']) {
  const src = join(srcDir, name)
  const dest = join(destDir, name)
  if (existsSync(src)) {
    copyFileSync(src, dest)
    console.log(`copy-data: ${name} -> public/data/`)
  } else {
    console.warn(`copy-data: WARNING ${src} not found -- run a batch first (see docs/SETUP.md)`)
  }
}
