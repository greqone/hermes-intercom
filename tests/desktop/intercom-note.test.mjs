// Catalog rule 8 regression: desktop/plugin.js must not touch Hermes Desktop's DOM.
//
// The Desktop Plugin SDK has no slot that renders an incoming agent message, so the
// compliant build leaves the native note exactly as the host drew it. These tests
// render real intercom cards (produced by store.format_intercom_message) into the
// native note fixture and assert the plugin changes nothing — no hidden notes, no
// injected cards, no observer surviving register/dispose.
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import { test } from 'node:test'

import {
  createLifecycle,
  flush,
  formatViaPython,
  loadJsdom,
  loadPlugin,
  PLUGIN_PATH,
  renderNativeMessage
} from './native-fixture.mjs'

const { JSDOM } = loadJsdom()

const base = { sender_card_name: 'Deploy checks (local-1a2b3c4d)', sender_session_id: 'local-1a2b3c4d', max_hops: 3 }
const [FYI, REPLY, FINAL, CODE, HOSTILE, OTHER, UNICODE] = formatViaPython([
  { ...base, hop: 1, expect_reply: false, message: 'Build is green on main.' },
  { ...base, hop: 1, expect_reply: true, message: 'Can you confirm the migration ran?' },
  { ...base, hop: 3, expect_reply: true, message: 'Final answer: yes.' },
  { ...base, hop: 2, expect_reply: false, message: 'Run `pytest -q` then:\n```py\nprint("ok")\n```\nDone.' },
  { ...base, hop: 1, expect_reply: false, message: '<img src=x onerror="alert(1)"><script>alert(2)</script>' },
  { sender_card_name: 'Other bot (local-99999999)', sender_session_id: 'local-99999999', max_hops: 3, hop: 2, expect_reply: false, message: 'Second message body.' },
  { ...base, hop: 1, expect_reply: false, message: 'Zażółć gęślą jaźń 🚀 \u2066left\u2069' }
])

function setup(messages) {
  const dom = new JSDOM('<!doctype html><html><body><div data-slot="thread"></div></body></html>')
  const doc = dom.window.document
  const thread = doc.querySelector('[data-slot="thread"]')
  const roots = messages.map((m, i) => {
    const root = renderNativeMessage(doc, m, { messageId: `m${i}` })
    thread.append(root)
    return root
  })
  globalThis.document = doc
  return { doc, win: dom.window, thread, roots }
}

test('register and dispose leave native intercom notes byte-identical', async () => {
  const env = setup([FYI, REPLY, FINAL, CODE, HOSTILE, UNICODE])
  const before = env.roots.map(r => r.outerHTML)
  const plugin = await loadPlugin()
  const life = createLifecycle()
  plugin.register(life.ctx)
  await flush(env.win)
  env.roots.forEach((r, i) => assert.equal(r.outerHTML, before[i], `note ${i} unchanged after register`))
  assert.equal(env.doc.querySelector('[data-hermes-intercom-card]'), null)
  assert.equal(env.doc.querySelector('[hidden]'), null)
  life.dispose()
  env.roots.forEach((r, i) => assert.equal(r.outerHTML, before[i], `note ${i} unchanged after dispose`))
})

test('a note mounted after register stays native, and dispose stops nothing that was watching', async () => {
  const env = setup([])
  const plugin = await loadPlugin()
  const life = createLifecycle()
  plugin.register(life.ctx)
  const root = renderNativeMessage(env.doc, OTHER, { messageId: 'late' })
  const before = root.outerHTML
  env.thread.append(root)
  await flush(env.win)
  assert.equal(root.outerHTML, before)
  life.dispose()
  env.thread.append(env.doc.createTextNode('later'))
  await flush(env.win)
  assert.equal(root.outerHTML, before)
  assert.equal(env.doc.querySelector('[data-hermes-intercom-card]'), null)
})

test('non-intercom notes and human bubbles stay untouched (positive control of the boundary)', async () => {
  const controls = [
    "[Message from agent 'turqoise'] ping",
    'plain human text that mentions [intercom from=x hop=1/3 mode=fyi]'
  ]
  const env = setup(controls)
  const before = env.roots.map(r => r.outerHTML)
  const plugin = await loadPlugin()
  const life = createLifecycle()
  plugin.register(life.ctx)
  await flush(env.win)
  env.roots.forEach((r, i) => assert.equal(r.outerHTML, before[i]))
  life.dispose()
})

test('source stays inside the SDK surface: no markup queries, no body observer, no HTML sinks', () => {
  assert.ok(existsSync(PLUGIN_PATH), 'desktop/plugin.js ships')
  const src = readFileSync(PLUGIN_PATH, 'utf8')
  assert.doesNotMatch(src, /data-slot/)
  assert.doesNotMatch(src, /MutationObserver/)
  assert.doesNotMatch(src, /querySelector|closest\(|getElementById|getElementsBy/)
  assert.doesNotMatch(src, /document\.body|document\.documentElement/)
  assert.doesNotMatch(src, /innerHTML|outerHTML\s*=|insertAdjacentHTML|document\.write/)
  assert.doesNotMatch(src, /\beval\s*\(|new\s+Function\b/)
  for (const m of src.matchAll(/(?:from\s*|import\s*\(\s*|import\s+)(['"])([^'"]+)\1/g)) {
    assert.ok(['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'].includes(m[2]), `disallowed import ${m[2]}`)
  }
})
