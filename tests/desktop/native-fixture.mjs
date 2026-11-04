// Test fixture: a faithful port of Hermes Desktop's native inter-agent note DOM.
//
// Mirrors apps/desktop/src/components/assistant-ui/thread/user-message.tsx
// (AGENT_MESSAGE_RE + AgentMessageNote) and user-message-text.tsx
// (UserMessageText: fence split, inline-code split, DirectiveContent text span).
// Classes are copied so snapshots resemble the real tree; only DOM structure,
// data-slot attributes and text-node layout matter to the plugin.
//
// Dependencies resolve from HERMES_DESKTOP_ROOT (the desktop app checkout that
// already has jsdom installed) or from this package's own node_modules.
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
export const PACKAGE_ROOT = path.resolve(here, '..', '..')
export const PLUGIN_PATH = path.join(PACKAGE_ROOT, 'desktop', 'plugin.js')

export function loadJsdom() {
  const base = process.env.HERMES_DESKTOP_ROOT
  const req = createRequire(base ? path.join(path.resolve(base), 'package.json') : import.meta.url)

  try {
    return req('jsdom')
  } catch (error) {
    throw new Error(
      'jsdom not resolvable. Set HERMES_DESKTOP_ROOT to a Hermes desktop checkout with node_modules ' +
        `(e.g. <hermes-agent>/apps/desktop). Cause: ${error.message}`
    )
  }
}

// --- copied from user-message.tsx -----------------------------------------
export const AGENT_MESSAGE_RE =
  /^(?:Message from (?:🤖\s*)?([^:\n(]{1,64}?)(?:\s*\(@([a-z0-9][a-z0-9_-]{0,63})(?:@[a-zA-Z0-9][a-zA-Z0-9_-]{0,63})?\))?:\s*|\[Message from agent '([^']{1,64})'\]\s*)([\s\S]*)$/u

// --- copied from user-message-text.tsx -------------------------------------
const FENCE_RE = /```([^\n`]*)\n([\s\S]*?)```/g
const INLINE_CODE_RE = /(`+)([^`\n][\s\S]*?)\1/g

function splitFences(text) {
  const segments = []
  let cursor = 0

  for (const match of text.matchAll(FENCE_RE)) {
    const start = match.index ?? 0
    if (start > cursor) segments.push({ kind: 'inline', text: text.slice(cursor, start) })
    segments.push({ kind: 'fence', lang: (match[1] || '').trim() || null, code: match[2] ?? '' })
    cursor = start + match[0].length
  }

  if (cursor < text.length) segments.push({ kind: 'inline', text: text.slice(cursor) })

  return segments
}

function splitInlineCode(text) {
  const nodes = []
  let cursor = 0

  for (const match of text.matchAll(INLINE_CODE_RE)) {
    const start = match.index ?? 0
    if (start > cursor) nodes.push({ kind: 'inline-text', text: text.slice(cursor, start) })
    nodes.push({ kind: 'inline-code', code: match[2] })
    cursor = start + match[0].length
  }

  if (cursor < text.length) nodes.push({ kind: 'inline-text', text: text.slice(cursor) })

  return nodes
}

function el(doc, tag, attrs = {}, children = []) {
  const node = doc.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) {
    if (v === true) node.setAttribute(k, '')
    else if (v != null) node.setAttribute(k, v)
  }
  for (const child of children) node.append(child)
  return node
}

/** <UserMessageText text=…/> */
export function renderUserMessageText(doc, text) {
  const root = el(doc, 'span', { class: 'block', 'data-slot': 'aui_user-message-text' })

  for (const segment of splitFences(text)) {
    if (segment.kind === 'fence') {
      const code = el(doc, 'code', { class: 'block whitespace-pre-wrap wrap-anywhere' }, [segment.code])
      root.append(
        el(doc, 'pre', { class: 'my-1.5 max-w-full font-mono', 'data-slot': 'aui_user-fence' }, [code])
      )
      continue
    }

    const inline = el(doc, 'span', { class: 'wrap-anywhere block whitespace-pre-line', 'data-slot': 'aui_user-inline-text' })
    for (const node of splitInlineCode(segment.text)) {
      if (node.kind === 'inline-code') {
        inline.append(
          el(doc, 'code', { class: 'mx-px rounded px-1 py-px font-mono', 'data-slot': 'aui_user-inline-code' }, [node.code])
        )
      } else {
        // DirectiveContent with no references: one span, one text node.
        inline.append(el(doc, 'span', { class: 'whitespace-pre-line', 'data-slot': 'aui_directive-text' }, [node.text]))
      }
    }
    root.append(inline)
  }

  return root
}

/** <AgentMessageNote text=…/> inside its MessagePrimitive.Root, or null when AGENT_MESSAGE_RE misses. */
export function renderNativeMessage(doc, rawText, { avatar = null, messageId = 'm1' } = {}) {
  const text = rawText.trim()
  const match = AGENT_MESSAGE_RE.exec(text)
  const root = el(doc, 'div', {
    class: 'flex w-full min-w-0 flex-col items-stretch',
    'data-role': 'user',
    'data-slot': 'aui_user-message-root',
    'data-message-id': messageId
  })

  if (!match) {
    // Human bubble (simplified: the plugin must never touch it).
    const bubble = el(doc, 'div', { class: 'composer-human-message' }, [renderUserMessageText(doc, text)])
    root.append(bubble)
    return root
  }

  const sender = (match[1] || match[3] || 'agent').trim()
  const body = (match[4] || '').trim()
  const note = el(doc, 'div', {
    class: 'flex max-w-[min(86%,44rem)] flex-col gap-0.5 self-center px-2 py-0.5 text-[0.6875rem] leading-5 text-muted-foreground/60',
    'data-slot': 'aui_agent-message-note'
  })
  const avatarNode = avatar
    ? el(doc, 'img', { alt: '', 'aria-hidden': 'true', class: 'size-4 shrink-0 rounded-full object-cover', src: avatar })
    : el(doc, 'span', { 'aria-hidden': 'true', class: 'text-[0.8125rem] leading-none' }, ['🤖'])
  // React renders `Message from {sender}` as two adjacent text nodes.
  const label = el(doc, 'span', { class: 'wrap-anywhere' }, ['Message from ', sender])
  note.append(el(doc, 'span', { class: 'flex items-center justify-center gap-1.5' }, [avatarNode, label]))

  if (body) {
    const summary = el(doc, 'summary', { class: 'cursor-pointer select-none text-center' }, ['show message'])
    const box = el(doc, 'div', { class: 'mt-1 max-w-[36rem] rounded-lg border border-(--ui-stroke-tertiary) px-3 py-2 text-left' }, [
      renderUserMessageText(doc, body)
    ])
    note.append(el(doc, 'details', { class: 'self-center' }, [summary, box]))
  }

  root.append(note)
  return root
}

/** Real producer strings from store.format_intercom_message (no hand-copied hints). */
export function formatViaPython(cases) {
  const python = process.env.HERMES_PYTHON || 'python'
  const script = [
    'import json, sys',
    `sys.path.insert(0, ${JSON.stringify(PACKAGE_ROOT)})`,
    'import store',
    'cases = json.loads(sys.stdin.read())',
    'out = [store.format_intercom_message(**c) for c in cases]',
    'sys.stdout.buffer.write(json.dumps(out, ensure_ascii=True).encode())'
  ].join('\n')
  const out = execFileSync(python, ['-c', script], { input: JSON.stringify(cases), encoding: 'utf8' })
  return JSON.parse(out)
}

/** Text a user can actually see: skips [hidden], aria-hidden decoration and closed <details> bodies. */
export function visibleText(node) {
  const win = node.ownerDocument.defaultView
  if (node.nodeType === win.Node.TEXT_NODE) return node.data
  if (node.nodeType !== win.Node.ELEMENT_NODE) return ''
  if (node.hasAttribute('hidden')) return ''
  if (node.style && node.style.display === 'none') return ''
  let out = ''
  for (const child of node.childNodes) {
    if (node.tagName === 'DETAILS' && !node.open && !(child.nodeType === 1 && child.tagName === 'SUMMARY')) continue
    out += visibleText(child)
  }
  return out
}

export function createLifecycle() {
  const disposers = []
  const ctx = {
    source: 'plugin:hermes-intercom',
    onDispose(fn) {
      disposers.push(fn)
    },
    register() {
      return () => {}
    }
  }
  return {
    ctx,
    disposers,
    dispose() {
      while (disposers.length) disposers.pop()()
    }
  }
}

/** Load desktop/plugin.js if present; otherwise an explicit no-op baseline (the unchanged native behavior). */
export async function loadPlugin() {
  const fs = await import('node:fs')
  if (!fs.existsSync(PLUGIN_PATH)) {
    return { id: 'hermes-intercom', name: 'no-op baseline (desktop/plugin.js absent)', register() {}, baseline: true }
  }
  const mod = await import(pathToFileURL(PLUGIN_PATH).href + `?t=${Date.now()}`)
  return mod.default
}

export function flush(win) {
  return new Promise(resolve => win.setTimeout(resolve, 0))
}
