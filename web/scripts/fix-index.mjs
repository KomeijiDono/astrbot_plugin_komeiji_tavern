import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'

const path = new URL('../dist/index.html', import.meta.url)
const html = await readFile(path, 'utf8')
const packageData = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))
const fixed = html
  .replace(/\s+type="module"/g, '')
  .replace(/\s+crossorigin/g, '')
  .replace('<script src=', '<script defer src=')
  .replace('assets/app.js', `assets/app.js?v=${packageData.version}`)
await writeFile(path, fixed, 'utf8')

const dashboard = new URL('../../pages/dashboard/', import.meta.url)
await mkdir(dashboard, { recursive: true })
await rm(new URL('assets/', dashboard), { recursive: true, force: true })
await cp(new URL('../dist/', import.meta.url), dashboard, { recursive: true })
