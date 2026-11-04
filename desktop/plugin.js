// hermes-intercom — desktop half.
//
// Hermes Desktop renders every `[Message from agent '<sender>'] …` delivery as a tiny centered
// notice ("Message from X · show message") and, once expanded, shows the raw intercom protocol
// envelope (`[intercom from=… hop=… mode=…]` + the agent-facing reply hint) before the actual
// text. This plugin progressively enhances ONLY those native notes whose body starts with a
// validated intercom tag: the native note is hidden (reversibly, `hidden` attribute) and a
// readable card is inserted right after it — sender header, FYI / reply status, the full message
// body cloned from the native renderer (inline code, fences, chips kept) and collapsed technical
// details. Everything else (other bot notices, human and assistant messages) is never touched.
//
// No RPC, no sends, no wake-ups: presentation only. Disabling the plugin restores the native DOM.
// Zero imports: the SDK is not needed for this, and the file stays a single loadable ESM module.

const NOTE_SELECTOR = '[data-slot="aui_agent-message-note"]'
const CARD_ATTR = 'data-hermes-intercom-card'
const HIDDEN_ATTR = 'data-hermes-intercom-hidden'

// Same shape as store.py BARE_INTERCOM_TAG_RE, anchored to the first body character.
const TAG_RE = /^\[intercom\s+from=([^\s\]]+)\s+hop=(\d{1,4})\/(\d{1,4})\s+mode=(reply|fyi)\]/
// Native header text is `Message from {sender}`; store.py caps the sender at 60 chars.
const HEADER_RE = /^Message from ([^'[\]\r\n]{1,64})$/
// Native UserMessageText renders `code` spans without their backticks.
const INLINE_CODE_RE = /(`+)([^`\n][\s\S]*?)\1/g

const TEXT_NODE = 3
const ELEMENT_NODE = 1

let ownerSeq = 0

/** The exact agent-facing hint store.format_intercom_message emits for a tag. */
export function expectedHint(sid, hop, maxHops, mode) {
  if (mode === 'reply' && hop < maxHops) {
    return (
      `Reply requested by session \`${sid}\` (hop ${hop}/${maxHops}). ` +
      `When done, send your concise reply with \`session_send(target="${sid}", message="...")\`.`
    )
  }
  if (mode === 'reply') {
    return (
      `Message from session \`${sid}\` (final autonomous hop ${hop}/${maxHops} - ` +
      'do not call `session_send` back automatically).'
    )
  }
  return `FYI from session \`${sid}\` (hop ${hop}/${maxHops}). No automatic reply needed unless asked.`
}

/** Text as the native renderer displays it (inline-code backticks removed). */
export function renderedInline(text) {
  return text.replace(INLINE_CODE_RE, (_whole, _ticks, code) => code)
}

/**
 * Parse the rendered text of a native note body. Returns null unless it begins with a valid
 * intercom tag. `bodyStart` is the offset of the human-meant message; only the tag and the exact
 * hint for that tag are skipped — an unknown hint stays part of the message.
 */
export function parseRenderedBody(rendered) {
  const tag = TAG_RE.exec(rendered)
  if (!tag) return null
  const [, sid, hopText, maxText, mode] = tag
  const hop = Number(hopText)
  const maxHops = Number(maxText)
  if (!(hop >= 1 && maxHops >= 1)) return null

  let cursor = tag[0].length
  let hintKnown = false
  const hint = '\n' + renderedInline(expectedHint(sid, hop, maxHops, mode))
  if (rendered.startsWith(hint, cursor)) {
    const after = cursor + hint.length
    if (after === rendered.length || rendered.startsWith('\n\n', after)) {
      cursor = after
      hintKnown = true
    }
  }
  while (cursor < rendered.length && /\s/.test(rendered[cursor])) cursor++

  return {
    sid,
    hop,
    maxHops,
    mode,
    hintKnown,
    bodyStart: cursor,
    status: mode === 'fyi' ? 'fyi' : hop < maxHops ? 'reply' : 'final'
  }
}

const STATUS_LABEL = { fyi: 'FYI', reply: 'Reply requested', final: 'Final hop' }
const STATUS_TITLE = {
  fyi: 'Informational — no reply expected',
  reply: 'The sending session asked for a reply',
  final: 'Last autonomous hop — the agent will not reply automatically'
}

/** Locate the native pieces of an AgentMessageNote; null when the structure is not the known one. */
function nativeParts(note) {
  const header = note.firstElementChild
  if (!header || header.tagName !== 'SPAN') return null
  const label = header.lastElementChild
  if (!label || label === header.firstElementChild) return null
  const headerMatch = HEADER_RE.exec(label.textContent || '')
  if (!headerMatch) return null
  const details = header.nextElementSibling
  if (!details || details.tagName !== 'DETAILS' || details.nextElementSibling) return null
  const bodyRoot = details.querySelector(':scope > div > [data-slot="aui_user-message-text"]')
  if (!bodyRoot) return null
  return { header, sender: headerMatch[1].trim(), avatar: header.firstElementChild, bodyRoot }
}

/**
 * Clone `root`'s content from rendered-text offset `start`, keeping the native element structure
 * (classes, code spans, fences, chips). `map` records clone -> original for activation forwarding.
 */
function cloneFrom(root, start, map) {
  let cursor = 0

  function walk(node) {
    if (node.nodeType === TEXT_NODE) {
      const text = node.data
      const from = cursor
      cursor += text.length
      if (cursor <= start) return null
      return node.ownerDocument.createTextNode(from >= start ? text : text.slice(start - from))
    }
    if (node.nodeType !== ELEMENT_NODE) return null
    const entry = cursor
    const copy = node.cloneNode(false)
    copy.removeAttribute('id')
    for (const child of Array.from(node.childNodes)) {
      const c = walk(child)
      if (c) copy.append(c)
    }
    const hadText = cursor > entry
    if (!copy.firstChild && (hadText || entry < start)) return null
    map.set(copy, node)
    return copy
  }

  const fragment = root.ownerDocument.createDocumentFragment()
  for (const child of Array.from(root.childNodes)) {
    const c = walk(child)
    if (c) fragment.append(c)
  }
  return fragment
}

function styled(doc, tag, style, attrs = {}) {
  const node = doc.createElement(tag)
  node.setAttribute('style', style)
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v)
  return node
}

const S = {
  card:
    'display:flex;flex-direction:column;gap:0.375rem;align-self:center;width:100%;min-width:0;max-width:min(100%,44rem);box-sizing:border-box;' +
    'padding:0.5rem 0.75rem;border:1px solid var(--ui-stroke-tertiary);border-radius:var(--radius-lg,0.75rem);' +
    'background:color-mix(in srgb,currentColor 3%,transparent);color:var(--color-foreground);text-align:left;',
  header:
    'display:flex;flex-wrap:wrap;align-items:center;gap:0.375rem;min-width:0;font-size:0.75rem;line-height:1.25rem;' +
    'color:var(--ui-text-tertiary,var(--color-muted-foreground));',
  avatar: 'display:inline-flex;align-items:center;justify-content:center;width:1rem;height:1rem;flex-shrink:0;overflow:hidden;border-radius:9999px;font-size:0.8125rem;line-height:1;',
  sender: 'min-width:0;overflow-wrap:anywhere;color:var(--ui-text-secondary,var(--color-muted-foreground));font-weight:500;',
  badge:
    'display:inline-flex;align-items:center;padding:0 0.375rem;border:1px solid var(--ui-stroke-secondary);' +
    'border-radius:9999px;font-size:0.6875rem;line-height:1.125rem;white-space:nowrap;',
  badgeReply: 'color:var(--ui-accent);border-color:color-mix(in srgb,var(--ui-accent) 45%,transparent);',
  body: 'display:block;min-width:0;overflow-wrap:anywhere;font-size:inherit;line-height:1.5;',
  empty: 'display:block;font-style:italic;color:var(--ui-text-tertiary,var(--color-muted-foreground));',
  meta: 'font-size:0.6875rem;line-height:1.125rem;color:var(--ui-text-tertiary,var(--color-muted-foreground));',
  summary: 'cursor:pointer;width:fit-content;user-select:none;',
  dl: 'display:grid;grid-template-columns:max-content minmax(0,1fr);gap:0 0.75rem;margin:0.25rem 0 0;',
  dd: 'margin:0;overflow-wrap:anywhere;font-family:var(--font-mono,monospace);'
}

function buildCard(doc, parts, parsed, owner) {
  const map = new WeakMap()
  const card = styled(doc, 'div', S.card, {
    [CARD_ATTR]: owner,
    'data-intercom-mode': parsed.mode,
    'data-intercom-status': parsed.status,
    role: 'group',
    'aria-label': `Message from ${parts.sender}`
  })

  const header = styled(doc, 'div', S.header, { 'data-part': 'header' })
  const avatar = styled(doc, 'span', S.avatar, { 'data-part': 'avatar', 'aria-hidden': 'true' })
  if (parts.avatar && parts.avatar !== parts.header.lastElementChild) {
    const pic = parts.avatar.cloneNode(true)
    if (pic.tagName === 'IMG') pic.setAttribute('style', 'width:100%;height:100%;object-fit:cover;')
    avatar.append(pic)
  }
  const sender = styled(doc, 'span', S.sender, { 'data-part': 'sender' })
  sender.textContent = parts.sender
  const badge = styled(doc, 'span', S.badge + (parsed.status === 'fyi' ? '' : S.badgeReply), {
    'data-part': 'status',
    title: STATUS_TITLE[parsed.status]
  })
  badge.textContent = STATUS_LABEL[parsed.status]
  header.append(avatar, sender, badge)

  const fragment = cloneFrom(parts.bodyRoot, parsed.bodyStart, map)
  let body
  if (fragment.textContent.trim() || fragment.querySelector?.('img,button,a')) {
    body = styled(doc, 'div', S.body, { 'data-part': 'body', dir: 'auto' })
    body.append(fragment)
  } else {
    body = styled(doc, 'div', S.empty, { 'data-part': 'empty' })
    body.textContent = '(empty message)'
  }

  const meta = styled(doc, 'details', S.meta, { 'data-part': 'meta' })
  const summary = styled(doc, 'summary', S.summary)
  summary.textContent = 'Intercom details'
  const dl = styled(doc, 'dl', S.dl)
  const rows = [
    ['From session', parsed.sid],
    ['Hop', `${parsed.hop}/${parsed.maxHops}`],
    ['Mode', parsed.mode === 'reply' ? 'reply requested' : 'fyi'],
    ['Agent hint', parsed.hintKnown ? 'standard (hidden)' : 'none recognized — shown in message']
  ]
  for (const [k, v] of rows) {
    const dt = styled(doc, 'dt', 'margin:0;')
    dt.textContent = k
    const dd = styled(doc, 'dd', S.dd)
    dd.textContent = v
    dl.append(dt, dd)
  }
  meta.append(summary, dl)

  card.append(header, body, meta)
  return { card, map }
}

function renderedText(bodyRoot) {
  return bodyRoot.textContent || ''
}

export function createEnhancer(doc) {
  const owner = `hic-${++ownerSeq}`
  const states = new Map() // note -> { card, map, key }
  const win = doc.defaultView

  function forward(event) {
    const card = event.currentTarget
    const state = [...states.values()].find(s => s.card === card)
    if (!state) return
    const target = event.target && event.target.closest ? event.target.closest('a,button,[role="button"]') : null
    if (!target || !card.querySelector('[data-part="body"]')?.contains(target)) return
    event.preventDefault()
    event.stopPropagation()
    const original = state.map.get(target)
    if (original && original.isConnected && typeof original.click === 'function') original.click()
  }

  function teardown(note) {
    const state = states.get(note)
    if (!state) return
    states.delete(note)
    state.card.removeEventListener('click', forward)
    state.card.remove()
    if (note.getAttribute(HIDDEN_ATTR) === owner) {
      note.removeAttribute(HIDDEN_ATTR)
      if (state.addedHidden) note.removeAttribute('hidden')
    }
  }

  function process(note) {
    if (!note.isConnected) return teardown(note)
    let parts = null
    let parsed = null
    try {
      parts = nativeParts(note)
      parsed = parts && parseRenderedBody(renderedText(parts.bodyRoot))
    } catch {
      parsed = null
    }
    if (!parts || !parsed) return teardown(note)

    const avatarKey = parts.avatar ? parts.avatar.outerHTML : ''
    const key = `${parts.sender}\u0000${avatarKey}\u0000${parts.bodyRoot.outerHTML}`
    const existing = states.get(note)
    if (existing && existing.key === key && existing.card.isConnected && existing.card.previousSibling === note) return

    let built
    try {
      built = buildCard(doc, parts, parsed, owner)
    } catch {
      return teardown(note) // uncertain: keep the native fallback
    }
    const addedHidden = existing ? existing.addedHidden : !note.hasAttribute('hidden')
    if (existing) {
      existing.card.removeEventListener('click', forward)
      existing.card.remove()
    }
    // Another (stale) instance's card for this note.
    const sibling = note.nextElementSibling
    if (sibling && sibling.hasAttribute(CARD_ATTR)) sibling.remove()

    built.card.addEventListener('click', forward)
    note.after(built.card)
    note.setAttribute(HIDDEN_ATTR, owner)
    if (!note.hasAttribute('hidden')) note.setAttribute('hidden', '')
    states.set(note, { card: built.card, map: built.map, key, addedHidden })
  }

  function noteFor(node) {
    const el = node.nodeType === ELEMENT_NODE ? node : node.parentElement
    return el && el.closest ? el.closest(NOTE_SELECTOR) : null
  }

  function onMutations(records) {
    const touched = new Set()
    let removed = false
    for (const record of records) {
      const target = record.target
      if (target.nodeType === ELEMENT_NODE && target.closest && target.closest(`[${CARD_ATTR}]`)) continue
      const note = noteFor(target)
      if (note) touched.add(note)
      for (const added of record.addedNodes) {
        if (added.nodeType !== ELEMENT_NODE || added.hasAttribute(CARD_ATTR)) continue
        if (added.matches(NOTE_SELECTOR)) touched.add(added)
        else for (const n of added.querySelectorAll(NOTE_SELECTOR)) touched.add(n)
      }
      if (record.removedNodes.length) removed = true
    }
    if (removed) {
      for (const [note, state] of states) {
        if (!note.isConnected || !state.card.isConnected || state.card.previousSibling !== note) touched.add(note)
      }
    }
    for (const note of touched) process(note)
  }

  const observer = new win.MutationObserver(onMutations)

  return {
    owner,
    start() {
      observer.observe(doc.body || doc.documentElement, { childList: true, subtree: true, characterData: true })
      for (const note of doc.querySelectorAll(NOTE_SELECTOR)) process(note)
    },
    dispose() {
      observer.disconnect()
      for (const note of [...states.keys()]) teardown(note)
    }
  }
}

export default {
  id: 'hermes-intercom',
  name: 'Hermes Intercom',
  description: 'Shows inter-session intercom messages as readable cards instead of raw protocol notices.',
  register(ctx) {
    const doc = globalThis.document
    if (!doc || !doc.defaultView || typeof doc.defaultView.MutationObserver !== 'function') return
    const enhancer = createEnhancer(doc)
    enhancer.start()
    ctx.onDispose(() => enhancer.dispose())
  }
}
