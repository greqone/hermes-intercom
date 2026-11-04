// Behavioral tests for desktop/plugin.js: progressive enhancement of Hermes Desktop's native
// inter-agent note for hermes-intercom deliveries. Runs on real jsdom DOM + MutationObserver.
//
//   HERMES_DESKTOP_ROOT=<hermes-agent>/apps/desktop node --test tests/desktop/
//
// When desktop/plugin.js is absent the harness registers an explicit no-op baseline, so the
// semantic assertions run against the unchanged native display (the RED state).
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
  renderNativeMessage,
  renderUserMessageText,
  visibleText
} from './native-fixture.mjs'

const { JSDOM } = loadJsdom()
const CARD = '[data-hermes-intercom-card]'
const NOTE = '[data-slot="aui_agent-message-note"]'

const base = { sender_card_name: 'Deploy checks (local-1a2b3c4d)', sender_session_id: 'local-1a2b3c4d', max_hops: 3 }
const [FYI, REPLY, FINAL, CODE, HOSTILE, EMPTY, LONG, OTHER] = formatViaPython([
  { ...base, hop: 1, expect_reply: false, message: 'Build is green on main.' },
  { ...base, hop: 1, expect_reply: true, message: 'Can you confirm the migration ran?' },
  { ...base, hop: 3, expect_reply: true, message: 'Final answer: yes.' },
  { ...base, hop: 2, expect_reply: false, message: 'Run `pytest -q` then:\n```py\nprint("ok")\n```\nDone.' },
  { ...base, hop: 1, expect_reply: false, message: '<img src=x onerror="alert(1)"><script>alert(2)</script> & <b>bold</b>' },
  { ...base, hop: 1, expect_reply: false, message: '' },
  { ...base, hop: 1, expect_reply: false, message: 'Zażółć gęślą jaźń 🚀 مرحبا ' + 'x'.repeat(4000) + ' end' },
  { sender_card_name: 'Other bot (local-99999999)', sender_session_id: 'local-99999999', max_hops: 3, hop: 2, expect_reply: false, message: 'Second message body.' }
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
  return { dom, doc, win: dom.window, thread, roots }
}

async function start(env) {
  const plugin = await loadPlugin()
  const life = createLifecycle()
  plugin.register(life.ctx)
  await flush(env.win)
  return { plugin, life }
}

function cardOf(root) {
  return root.querySelector(CARD)
}

function cardBodyText(root) {
  const body = root.querySelector(`${CARD} [data-part="body"]`)
  return body ? body.textContent : null
}

const HINT_FRAGMENTS = ['No automatic reply needed', 'When done, send your concise reply', 'do not call session_send back', '[intercom ']

function assertClean(root, bodyText) {
  const seen = visibleText(root)
  assert.ok(seen.includes(bodyText), `body must be visible without clicking; visible=${JSON.stringify(seen.slice(0, 200))}`)
  for (const fragment of HINT_FRAGMENTS) {
    assert.ok(!seen.includes(fragment), `protocol text "${fragment}" must not be in the readable view`)
  }
}

test('fyi delivery: body readable at a glance, protocol envelope hidden, FYI badge', async () => {
  const env = setup([FYI])
  const { life } = await start(env)
  const [root] = env.roots
  assertClean(root, 'Build is green on main.')
  assert.equal(root.querySelectorAll(CARD).length, 1)
  const card = cardOf(root)
  assert.equal(card.getAttribute('data-intercom-mode'), 'fyi')
  assert.match(card.querySelector('[data-part="status"]').textContent, /FYI/)
  assert.match(card.querySelector('[data-part="sender"]').textContent, /Deploy checks \(local-1a2b3c4d\)/)
  assert.equal(cardBodyText(root), 'Build is green on main.')
  life.dispose()
})

test('reply delivery: reply-requested badge; hint and session id kept in technical details only', async () => {
  const env = setup([REPLY])
  const { life } = await start(env)
  const [root] = env.roots
  assertClean(root, 'Can you confirm the migration ran?')
  const card = cardOf(root)
  assert.equal(card.getAttribute('data-intercom-mode'), 'reply')
  assert.match(card.querySelector('[data-part="status"]').textContent, /Reply requested/)
  const meta = card.querySelector('details[data-part="meta"]')
  assert.ok(meta && !meta.open, 'technical metadata is collapsed by default')
  assert.match(meta.textContent, /local-1a2b3c4d/)
  assert.match(meta.textContent, /1\/3/)
  assert.ok(meta.querySelector('summary'), 'metadata toggle is a native keyboard-reachable summary')
  life.dispose()
})

test('final hop: final-hop badge, no reply prompt text', async () => {
  const env = setup([FINAL])
  const { life } = await start(env)
  const [root] = env.roots
  assertClean(root, 'Final answer: yes.')
  assert.match(cardOf(root).querySelector('[data-part="status"]').textContent, /Final hop/)
  life.dispose()
})

test('inline code and fences from the native renderer are preserved in the card body', async () => {
  const env = setup([CODE])
  const { life } = await start(env)
  const [root] = env.roots
  const body = root.querySelector(`${CARD} [data-part="body"]`)
  assert.ok(body, 'card body rendered')
  const inline = body.querySelector('[data-slot="aui_user-inline-code"]')
  assert.equal(inline?.textContent, 'pytest -q')
  const fence = body.querySelector('[data-slot="aui_user-fence"] code')
  assert.equal(fence?.textContent, 'print("ok")\n')
  assert.ok(visibleText(root).includes('Done.'))
  assert.ok(!visibleText(root).includes('[intercom '))
  life.dispose()
})

test('hostile body stays literal text: no elements created from message content', async () => {
  const env = setup([HOSTILE])
  const { life } = await start(env)
  const [root] = env.roots
  const body = root.querySelector(`${CARD} [data-part="body"]`)
  assert.ok(body, 'card body rendered')
  assert.equal(body.querySelectorAll('img,script,b').length, 0)
  assert.equal(body.textContent, '<img src=x onerror="alert(1)"><script>alert(2)</script> & <b>bold</b>')
  life.dispose()
})

test('unknown hint line is retained as message text; only the validated tag is consumed', async () => {
  const text = "[Message from agent 'Legacy (local-1)'] [intercom from=local-1 hop=1/3 mode=fyi]\nCustom protocol note.\n\nActual body."
  const nearMiss = "[Message from agent 'Legacy (local-1)'] [intercom from=local-1 hop=1/3 mode=reply]\nFYI from session `local-1` (hop 1/3). No automatic reply needed unless asked.\n\nMismatched hint body."
  const env = setup([text, nearMiss])
  const { life } = await start(env)
  const [a, b] = env.roots
  assert.ok(cardOf(a), 'tagged note is enhanced')
  assert.equal(cardBodyText(a), 'Custom protocol note.\n\nActual body.')
  assert.ok(!visibleText(a).includes('[intercom '))
  assert.ok(cardOf(b), 'tagged note with a mode-mismatched hint is enhanced')
  assert.ok(cardBodyText(b).includes('No automatic reply needed unless asked.'), 'a hint that is not the exact one for this tag is kept')
  assert.ok(cardBodyText(b).endsWith('Mismatched hint body.'))
  life.dispose()
})

test('long unicode body is reproduced exactly, readable without expansion', async () => {
  const env = setup([LONG])
  const { life } = await start(env)
  const [root] = env.roots
  const expected = 'Zażółć gęślą jaźń 🚀 مرحبا ' + 'x'.repeat(4000) + ' end'
  assert.equal(cardBodyText(root), expected)
  assertClean(root, expected)
  const body = root.querySelector(`${CARD} [data-part="body"]`)
  assert.equal(body.closest('details'), null, 'body is not collapsed inside a disclosure')
  life.dispose()
})

test('empty body: card still shows sender and an empty-state, never the protocol text', async () => {
  const env = setup([EMPTY])
  const { life } = await start(env)
  const [root] = env.roots
  const card = cardOf(root)
  assert.ok(card, 'enhanced')
  const seen = visibleText(root)
  assert.match(seen, /Deploy checks/)
  for (const fragment of HINT_FRAGMENTS) assert.ok(!seen.includes(fragment))
  assert.ok(card.querySelector('[data-part="empty"]'))
  life.dispose()
})

test('controls: non-intercom agent notes, look-alike text and human messages are untouched', async () => {
  const controls = [
    "[Message from agent 'turqoise'] ping",
    'Message from 🤖 Hermes: hello there [intercom from=x hop=1/3 mode=fyi]',
    "[Message from agent 'Spoof'] hello [intercom from=x hop=1/3 mode=fyi]\nbody",
    "[Message from agent 'Bad'] [intercom from=x hop=one/3 mode=fyi]\nbody",
    '[intercom from=local-1 hop=1/3 mode=fyi]\nhuman typed this'
  ]
  const env = setup(controls)
  const before = env.roots.map(r => r.outerHTML)
  const { life } = await start(env)
  env.roots.forEach((r, i) => assert.equal(r.outerHTML, before[i], `control ${i} unchanged`))
  life.dispose()
  env.roots.forEach((r, i) => assert.equal(r.outerHTML, before[i], `control ${i} unchanged after dispose`))
})

test('live add: a note mounted after register is enhanced', async () => {
  const env = setup([])
  const { life } = await start(env)
  const root = renderNativeMessage(env.doc, OTHER, { messageId: 'late' })
  env.thread.append(root)
  await flush(env.win)
  assert.equal(root.querySelectorAll(CARD).length, 1)
  assertClean(root, 'Second message body.')
  life.dispose()
})

test('React node reuse: body/header text replaced in place re-renders the card; non-intercom reuse restores native', async () => {
  const env = setup([FYI])
  const { life } = await start(env)
  const [root] = env.roots
  const note = root.querySelector(NOTE)
  // React keeps the same note element and swaps the inner text subtree.
  const box = note.querySelector('details > div')
  const replacement = renderNativeMessage(env.doc, OTHER)
  box.replaceChildren(replacement.querySelector('[data-slot="aui_user-message-text"]'))
  note.firstElementChild.lastElementChild.childNodes[1].data = 'Other bot (local-99999999)'
  await flush(env.win)
  assert.equal(root.querySelectorAll(CARD).length, 1, 'no duplicate cards')
  assert.equal(cardBodyText(root), 'Second message body.')
  assert.match(cardOf(root).querySelector('[data-part="sender"]').textContent, /Other bot/)

  // Reused for a plain bot notice: back to the untouched native note.
  box.replaceChildren(renderUserMessageText(env.doc, 'just a ping'))
  await flush(env.win)
  assert.equal(root.querySelector(CARD), null)
  assert.equal(note.hasAttribute('hidden'), false, 'native note visible again')
  assert.equal(root.querySelector('[hidden]'), null)
  assert.ok(note.querySelector('details'))
  life.dispose()
})

test('React swaps the Root children (note -> human bubble): orphan card is removed', async () => {
  const env = setup([FYI])
  const { life } = await start(env)
  const [root] = env.roots
  assert.ok(cardOf(root), 'enhanced first')
  const note = root.querySelector(NOTE)
  const human = renderNativeMessage(env.doc, 'plain human text').firstElementChild
  root.removeChild(note)
  root.appendChild(human)
  await flush(env.win)
  assert.equal(root.querySelector(CARD), null)
  assert.equal(root.firstElementChild, human)
  assert.equal(note.hasAttribute('hidden'), false, 'detached native note restored too')
  life.dispose()
})

test('avatar arriving later (React swaps emoji span for img) is reflected without duplicates', async () => {
  const env = setup([FYI])
  const { life } = await start(env)
  const [root] = env.roots
  const header = root.querySelector(NOTE).firstElementChild
  const img = env.doc.createElement('img')
  img.setAttribute('src', 'data:image/png;base64,AAAA')
  img.setAttribute('aria-hidden', 'true')
  img.setAttribute('alt', '')
  header.firstElementChild.replaceWith(img)
  await flush(env.win)
  assert.equal(root.querySelectorAll(CARD).length, 1)
  assert.equal(cardOf(root).querySelector('[data-part="avatar"] img')?.getAttribute('src'), 'data:image/png;base64,AAAA')
  life.dispose()
})

test('dispose restores the exact native DOM (incl. details.open) and stops observing', async () => {
  const env = setup([FYI, REPLY])
  env.roots[1].querySelector('details').open = true
  const before = env.roots.map(r => r.outerHTML)
  const { plugin, life } = await start(env)
  assert.ok(cardOf(env.roots[0]), 'enhanced while active')
  life.dispose()
  env.roots.forEach((r, i) => assert.equal(r.outerHTML, before[i], `native DOM restored for ${i}`))
  assert.equal(env.roots[1].querySelector('details').open, true)

  const late = renderNativeMessage(env.doc, OTHER)
  env.thread.append(late)
  await flush(env.win)
  assert.equal(late.querySelector(CARD), null, 'no observer survives dispose')

  // Re-enable (hot reload) twice: exactly one card per note, no leaked observers.
  const again = createLifecycle()
  plugin.register(again.ctx)
  await flush(env.win)
  again.dispose()
  const third = createLifecycle()
  plugin.register(third.ctx)
  await flush(env.win)
  for (const r of [...env.roots, late]) assert.equal(r.querySelectorAll(CARD).length, 1)
  third.dispose()
  assert.equal(env.doc.querySelectorAll(CARD).length, 0)
  assert.equal(env.doc.querySelectorAll('[data-hermes-intercom-hidden]').length, 0)
})

test('unrelated streaming mutations do not rebuild existing cards', async () => {
  const env = setup([FYI])
  const { life } = await start(env)
  const card = cardOf(env.roots[0])
  assert.ok(card, 'enhanced before streaming starts')
  const stream = env.doc.createElement('div')
  env.thread.append(stream)
  for (let i = 0; i < 50; i++) stream.append(env.doc.createTextNode(`token${i} `))
  await flush(env.win)
  assert.equal(cardOf(env.roots[0]), card, 'same card node: not rebuilt')
  life.dispose()
})

test('interactive nodes cloned from the native body forward activation to the native original', async () => {
  const env = setup([FYI])
  const [root] = env.roots
  const span = root.querySelector('[data-slot="aui_user-message-text"] [data-slot="aui_directive-text"]:last-child')
  const chip = env.doc.createElement('button')
  chip.type = 'button'
  chip.textContent = 'notes.md'
  let clicks = 0
  chip.addEventListener('click', () => clicks++)
  span.append(chip)
  const { life } = await start(env)
  const cloned = root.querySelector(`${CARD} [data-part="body"] button`)
  assert.ok(cloned, 'chip is present in the card body')
  cloned.click()
  assert.equal(clicks, 1)
  life.dispose()
})

test('attribute-only native updates (thumbnail src swap, text direction) reach the visible card', async () => {
  const env = setup([FYI])
  const [root] = env.roots
  const span = root.querySelector('[data-slot="aui_user-message-text"] [data-slot="aui_directive-text"]:last-child')
  const img = env.doc.createElement('img')
  img.setAttribute('src', 'before.png')
  img.setAttribute('alt', 'screenshot')
  span.append(img)
  const { life } = await start(env)
  const native = root.querySelector(`${NOTE} [data-slot="aui_user-message-text"]`)
  // React keeps the same nodes and only patches attributes (directive-text.tsx thumbnail swap,
  // user-message-text.tsx forced text direction).
  native.querySelector('img').setAttribute('src', 'after.png')
  native.querySelector('[data-slot="aui_user-inline-text"]').setAttribute('dir', 'rtl')
  await flush(env.win)
  assert.equal(root.querySelectorAll(CARD).length, 1, 'no duplicate cards')
  const body = root.querySelector(`${CARD} [data-part="body"]`)
  assert.equal(body.querySelector('img')?.getAttribute('src'), 'after.png')
  assert.equal(body.querySelector('[data-slot="aui_user-inline-text"]')?.getAttribute('dir'), 'rtl')
  life.dispose()
})

test('link gestures keep modifiers and middle-click when forwarded to the native original', async () => {
  const env = setup([FYI])
  const [root] = env.roots
  const span = root.querySelector('[data-slot="aui_user-message-text"] [data-slot="aui_directive-text"]:last-child')
  const link = env.doc.createElement('a')
  link.setAttribute('href', 'https://example.com/doc')
  link.textContent = 'doc'
  const seen = []
  const record = e => {
    seen.push({ type: e.type, ctrl: e.ctrlKey, meta: e.metaKey, shift: e.shiftKey, button: e.button })
    e.preventDefault()
  }
  link.addEventListener('click', record)
  link.addEventListener('auxclick', record)
  span.append(link)
  const { life } = await start(env)
  const cloned = root.querySelector(`${CARD} [data-part="body"] a`)
  assert.ok(cloned, 'link is present in the card body')
  const ctrlClick = new env.win.MouseEvent('click', { bubbles: true, cancelable: true, ctrlKey: true, button: 0 })
  cloned.dispatchEvent(ctrlClick)
  assert.equal(ctrlClick.defaultPrevented, true, 'the clone never navigates by itself')
  const middle = new env.win.MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 })
  cloned.dispatchEvent(middle)
  assert.equal(middle.defaultPrevented, true, 'middle-click on the clone is not handled by the clone')
  assert.deepEqual(seen, [
    { type: 'click', ctrl: true, meta: false, shift: false, button: 0 },
    { type: 'auxclick', ctrl: false, meta: false, shift: false, button: 1 }
  ])
  life.dispose()
})

test('session ids the native renderer would mangle (backticks) keep the untouched native note', async () => {
  const [ODD_FYI, ODD_REPLY, PLAIN] = formatViaPython([
    { ...base, sender_session_id: 'a``b', hop: 1, expect_reply: false, message: 'Body one.' },
    { ...base, sender_session_id: 'a`b', hop: 1, expect_reply: true, message: 'Body two.' },
    { ...base, sender_session_id: 'local-1a2b3c4d', hop: 1, expect_reply: false, message: 'Control body.' }
  ])
  const env = setup([ODD_FYI, ODD_REPLY, PLAIN])
  const before = env.roots.map(r => r.outerHTML)
  const { life } = await start(env)
  assert.equal(env.roots[0].outerHTML, before[0], 'mangled id: never shown with a wrong session id')
  assert.equal(env.roots[1].outerHTML, before[1], 'mangled id: never shown with a wrong session id')
  assert.ok(cardOf(env.roots[2]), 'positive control still enhanced')
  assertClean(env.roots[2], 'Control body.')
  life.dispose()
})

test('non-BMP sender names within the producer cap are still enhanced', async () => {
  const [EMOJI] = formatViaPython([
    { ...base, sender_card_name: '🚀'.repeat(35), hop: 1, expect_reply: false, message: 'Rocket body.' }
  ])
  const env = setup([EMOJI])
  assert.ok(env.roots[0].querySelector(NOTE), 'native renders it as an agent note')
  const { life } = await start(env)
  assert.ok(cardOf(env.roots[0]), 'enhanced')
  assert.equal(cardOf(env.roots[0]).querySelector('[data-part="sender"]').textContent, '🚀'.repeat(35))
  assertClean(env.roots[0], 'Rocket body.')
  life.dispose()
})

test('packaging: single ESM file, public default export, only allowed imports, no HTML sinks', { skip: !existsSync(PLUGIN_PATH) && 'desktop/plugin.js absent (baseline)' }, async () => {
  const plugin = await loadPlugin()
  assert.equal(plugin.id, 'hermes-intercom')
  assert.equal(typeof plugin.register, 'function')
  const src = readFileSync(PLUGIN_PATH, 'utf8')
  for (const m of src.matchAll(/(?:from\s*|import\s*\(\s*|import\s+)(['"])([^'"]+)\1/g)) {
    assert.ok(['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'].includes(m[2]), `disallowed import ${m[2]}`)
  }
  assert.doesNotMatch(src, /innerHTML|outerHTML\s*=|insertAdjacentHTML|document\.write/)
})
