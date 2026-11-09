const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')

function harness({ timeout = false, text = '**Status:** `confirmed`' } = {}) {
  const handlers = {}, requests = [], posts = [], notices = [], timers = []
  const job = { ok: true, id: 'f1', command: 'npm run build', cwd: 'C:/demo',
    toolsets: ['backstop'], max_turns: 8, prompt: 'diagnose', title: 'Backstop' }
  const ctx = {
    onEvent: (name, fn) => { handlers[name] = fn },
    setTimeout: fn => timers.push(fn),
    rest: async (route, options = {}) => {
      posts.push([route, options.body])
      if (route === '/claim') return job
      if (route === '/status') return { items: [] }
      if (route === '/result') return { id: 'f1', status: 'ready', answer: options.body.text }
      return { ok: true }
    }
  }
  const host = {
    notify: data => notices.push(data),
    request: async (method, params) => {
      requests.push([method, JSON.parse(JSON.stringify(params))])
      if (method === 'session.create') {
        if (params.max_turns) throw new Error('Canonical gateway does not support explicit session options: max_turns')
        return { session_id: 's1', stored_session_id: 'stored1', info: {} }
      }
      if (method === 'prompt.submit') {
        if (timeout) timers[0]()
        else handlers['message.complete']({ session_id: 's1', payload: { text, outcome: 'completed' } })
      }
      return { ok: true }
    }
  }
  const source = fs.readFileSync(path.join(__dirname, '../desktop/plugin.js'), 'utf8')
    .replace(/^import .*$/gm, '')
    .replace('export default', 'globalThis.plugin =')
  const context = vm.createContext({ host, useValue() {}, useEffect() {}, useState() {},
    jsx() {}, jsxs() {}, setTimeout, clearTimeout, console })
  vm.runInContext(source + '\nglobalThis.testApi = { createRunner, verdict };', context)
  return { api: context.testApi, ctx, requests, posts, notices }
}

test('unsupported turn cap retries without broadening read-only tools', async () => {
  const h = harness()
  await h.api.createRunner(h.ctx)({ id: 'f1', status: 'pending', command: 'npm run build' })
  const creates = h.requests.filter(([method]) => method === 'session.create')
  assert.equal(creates.length, 2)
  assert.equal(creates[0][1].max_turns, 8)
  assert.equal(creates[1][1].max_turns, undefined)
  for (const [, params] of creates) assert.deepEqual(params.toolsets, ['backstop'])
  assert.ok(h.notices.some(n => n.kind === 'warning'))
})

test('timeout interrupts the diagnosis session', async () => {
  const h = harness({ timeout: true })
  await h.api.createRunner(h.ctx)({ id: 'f1', status: 'pending', command: 'npm run build' })
  assert.ok(h.requests.some(([m, p]) => m === 'session.interrupt' && p.session_id === 's1'))
  assert.ok(h.posts.some(([route, body]) => route === '/result' && /interruption requested/.test(body.error || '')))
})

test('an empty completion is a failure, not a ready diagnosis', async () => {
  const h = harness({ text: '' })
  await h.api.createRunner(h.ctx)({ id: 'f1', status: 'pending', command: 'npm run build' })
  assert.ok(h.posts.some(([route, body]) => route === '/result' && /empty|did not complete/.test(body.error || '')))
  assert.equal(h.posts.some(([route, body]) => route === '/result' && 'text' in body), false)
})

test('only confirmed status is treated as confirmed', () => {
  const h = harness()
  for (const status of ['confirmed', '`confirmed`', 'confirmed.']) {
    assert.equal(h.api.verdict({ answer: `**Status:** ${status}` }), 'confirmed')
  }
  for (const status of ['unconfirmed', 'not confirmed', '`hypothesis`']) {
    assert.equal(h.api.verdict({ answer: `**Status:** ${status}` }), 'hypothesis')
  }
})
