// Backstop — Desktop half.
// Picks up a failed terminal command from the backend, opens a READ-ONLY diagnosis session for it
// (toolsets: ['backstop']), waits for the answer and tells you, with a stale warning when the
// project changed while the agent was reading it.
import { host, useValue, Popover, PopoverContent, PopoverTrigger } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ANSWER_TIMEOUT_MS = 5 * 60 * 1000
const POLL_MS = 4000

// ── tiny store ────────────────────────────────────────────────────────────────
let state = { items: [], busy: null, last: null, error: null }
const listeners = new Set()
function setState(patch) {
  state = { ...state, ...patch }
  listeners.forEach(fn => fn(state))
}
function useBackstop() {
  const [s, set] = useState(state)
  useEffect(() => {
    listeners.add(set)
    return () => listeners.delete(set)
  }, [])
  return s
}

function firstLine(text, label) {
  const m = new RegExp(`\\*\\*${label}:\\*\\*\\s*([^\\n]+)`).exec(text || '')
  return m ? m[1].trim() : ''
}

function shorten(s, n) {
  s = String(s || '')
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}

function uid() {
  return (globalThis.crypto && crypto.randomUUID) ? crypto.randomUUID().replace(/-/g, '') : String(Date.now()) + Math.random().toString(16).slice(2)
}

// ── the run ───────────────────────────────────────────────────────────────────
function createRunner(ctx) {
  const inFlight = new Set()
  const waiters = new Map() // session_id -> resolve

  ctx.onEvent('message.complete', ev => {
    const sid = ev && ev.session_id
    const resolve = sid && waiters.get(sid)
    if (resolve) {
      waiters.delete(sid)
      resolve(ev.payload || {})
    }
  })

  const report = (path, body) => ctx.rest(path, { method: 'POST', body }).catch(() => null)

  function waitForAnswer(sid) {
    return new Promise((resolve, reject) => {
      waiters.set(sid, resolve)
      ctx.setTimeout(() => {
        if (waiters.delete(sid)) {
          host.request('session.interrupt', { session_id: sid }).catch(() => null)
          reject(new Error('no answer within 5 minutes; diagnosis interruption requested'))
        }
      }, ANSWER_TIMEOUT_MS)
    })
  }

  async function run(item) {
    if (!item || item.status !== 'pending' || inFlight.has(item.id)) return
    inFlight.add(item.id)
    try {
      const job = await ctx.rest('/claim', { method: 'POST', body: { id: item.id, client: 'desktop' } })
      if (!job || !job.ok) return // another window took it
      setState({ busy: { id: job.id, command: item.command, since: Date.now() } })

      const params = {
        request_id: `backstop-${job.id}`, // a retried create returns the same session
        source: 'gui',
        cwd: job.cwd,
        title: job.title,
        toolsets: job.toolsets // read-only: the session gets these tools and nothing else
      }
      if (job.model) params.model = job.model
      if (job.max_turns) params.max_turns = job.max_turns // cost cap per diagnosis
      let created
      try {
        created = await host.request('session.create', params)
      } catch (err) {
        const message = String(err && err.message || err)
        if (!/does not support explicit session options:\s*max_turns\s*$/i.test(message)) throw err
        delete params.max_turns
        host.notify({ kind: 'warning', title: 'Backstop: turn limit unavailable',
          message: 'This gateway cannot enforce the per-diagnosis turn cap. The 5-minute interruption limit still applies.' })
        created = await host.request('session.create', params)
      }
      const sid = created.session_id
      const stored = created.stored_session_id || sid
      const granted = (created.info && created.info.tools) || {}
      await report('/started', { id: job.id, session_id: sid, stored_session_id: stored, tools: granted })

      const answer = waitForAnswer(sid)
      await host.request('prompt.submit', { session_id: sid, submission_id: uid(), text: job.prompt })
      const payload = await answer
      const text = String(payload.text || payload.content || '')
      if (!text.trim() || (payload.outcome && payload.outcome !== 'completed')) {
        throw new Error(`Diagnosis did not complete (${payload.outcome || 'empty answer'})`)
      }
      const result = await ctx.rest('/result', {
        method: 'POST',
        body: { id: job.id, session_id: sid, stored_session_id: stored, text, outcome: payload.outcome }
      })
      setState({ busy: null, last: result })
      announce(ctx, result, report)
    } catch (err) {
      const msg = err && err.message ? err.message : String(err)
      await report('/result', { id: item.id, error: msg })
      setState({ busy: null, error: msg })
      host.notify({ kind: 'error', title: 'Backstop could not finish', message: msg })
    } finally {
      inFlight.delete(item.id)
      refresh(ctx, run)
    }
  }
  return run
}

function openResult(ctx, result, report, via) {
  if (!result || !result.stored_session_id) return
  host.openSession(result.stored_session_id)
  report('/event', { id: result.id, event: 'opened', via })
}

function verdict(result) {
  return /^`?confirmed`?(?:\s|[.;]|$)/i.test(firstLine(result && result.answer, 'Status')) ? 'confirmed' : 'hypothesis'
}

const sleep = ms => new Promise(r => setTimeout(r, ms))

// "Fix it": a NORMAL chat in the project (your usual tools and approvals), with the fix drafted
// in its composer. Nothing is sent until you press Enter.
async function fixIt(ctx, result, report) {
  if (!result || !result.answer) return
  try {
    const params = { request_id: `backstop-fix-${result.id}`, source: 'gui', cwd: result.cwd,
      title: `Fix · ${shorten(result.command, 40)} · ${String(result.id).slice(0, 6)}` }
    const st = await ctx.rest('/status').catch(() => null)
    if (st && st.model) params.model = st.model
    const created = await host.request('session.create', params)
    const stored = created.stored_session_id || created.session_id
    host.openSession(stored)
    const draft = `Backstop diagnosed this failure of \`${result.command}\` (exit ${result.exit}) in ${result.cwd}.\n` +
      `Apply the fix below, then run \`${result.command}\` once to verify.\n\n${result.answer}`
    let ok = false
    for (let i = 0; i < 24 && !ok; i++) {
      ok = await host.composer.setDraft(stored, draft).catch(() => false)
      if (!ok) await sleep(250)
    }
    report('/event', { id: result.id, event: ok ? 'fix-drafted' : 'fix-opened', via: 'chip-menu' })
    if (!ok) host.notify({ kind: 'info', title: 'Backstop', message: 'Fix chat opened; paste the diagnosis to apply it.' })
  } catch (err) {
    host.notify({ kind: 'error', title: 'Backstop could not open a fix chat', message: String(err && err.message || err) })
  }
}

function announceResolved(item) {
  if (!item) return
  host.notify({
    kind: 'success',
    title: `Backstop: ${shorten(item.command, 40)} passes now`,
    message: `Incident closed${item.minutes_to_fix != null ? ` · ${item.minutes_to_fix} min from failure to green` : ''}.`
  })
}

function announce(ctx, result, report) {
  if (!result) return
  const stale = result.status === 'stale'
  const cause = firstLine(result.answer, 'Cause') || 'Diagnosis ready'
  const where = firstLine(result.answer, 'Where')
  const kind = verdict(result) === 'confirmed' ? 'cause found' : 'likely cause'
  const title = stale
    ? `Backstop: ${shorten(result.command, 40)} — project changed, may be stale`
    : `Backstop: ${kind} · ${shorten(result.command, 40)} (exit ${result.exit})`
  const detail = stale
    ? `Changed while diagnosing: ${(result.stale_files || []).slice(0, 4).join(', ')}`
    : where
  host.notify({
    kind: stale ? 'warning' : 'success',
    title,
    message: shorten(cause, 220),
    ...(detail ? { detail } : {}),
    action: { label: 'Open diagnosis', onClick: () => openResult(ctx, result, report, 'toast') }
  })
  try {
    ctx.os.notify({
      title,
      body: [shorten(cause, 180), detail].filter(Boolean).join('\n'),
      onActivate: () => openResult(ctx, result, report, 'os-notification')
    })
  } catch {
    /* native notification is best-effort */
  }
  report('/event', { id: result.id, event: 'notified', status: result.status })
}

async function refresh(ctx, run) {
  try {
    const s = await ctx.rest('/status')
    setState({ items: s.items || [], error: null })
    for (const item of s.items || []) if (item.status === 'pending') run(item)
  } catch (err) {
    setState({ error: 'backend off — enable "backstop" in plugins.enabled' })
  }
}

// ── UI ────────────────────────────────────────────────────────────────────────
const DOT = { ready: '#22c55e', stale: '#f59e0b', failed: '#ef4444', diagnosing: '#3b82f6', pending: '#3b82f6' }

function MenuButton({ label, onClick, disabled }) {
  return jsx('button', {
    type: 'button',
    disabled,
    onClick,
    style: { display: 'block', width: '100%', padding: '6px 8px', textAlign: 'left',
      fontSize: 12, cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.4 : 1 },
    className: 'block w-full rounded px-2 py-1 text-left text-xs hover:bg-(--ui-bg-hover) disabled:opacity-40',
    children: label
  })
}

function Chip({ ctx, report }) {
  const s = useBackstop()
  const [open, setOpen] = useState(false)
  useValue(host.state.gateway)
  const latest = s.items.find(i => i.status !== 'skipped')
  let label = 'Backstop'
  let color = '#71717a'
  if (s.error) {
    label = 'Backstop · off'
  } else if (s.busy) {
    label = `Backstop · diagnosing ${shorten(s.busy.command, 24)}`
    color = DOT.diagnosing
  } else if (latest && latest.resolved) {
    label = 'Backstop · fixed'
    color = DOT.ready
  } else if (latest && (latest.status === 'ready' || latest.status === 'stale')) {
    label = latest.status === 'stale' ? 'Backstop · stale result'
      : verdict(latest) === 'confirmed' ? 'Backstop · cause found' : 'Backstop · likely cause'
    color = DOT[latest.status]
  }
  const hasResult = !!(latest && latest.stored_session_id && latest.answer)
  const close = fn => () => { setOpen(false); fn() }
  return jsxs(Popover, {
    open,
    onOpenChange: setOpen,
    children: [
      jsx(PopoverTrigger, { asChild: true, children: jsxs('button', {
        type: 'button',
        title: s.error || (latest ? `${latest.command} → ${latest.resolved ? 'fixed' : latest.status}` : 'Watching your terminal for failed commands'),
        className: 'flex items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-primary)',
        children: [
          jsx('span', { style: { width: 6, height: 6, borderRadius: 3, background: color, display: 'inline-block' } }),
          jsx('span', { children: label })
        ]
      }) }),
      jsxs(PopoverContent, {
        side: 'top', align: 'end', variant: 'menu', sideOffset: 8,
        style: { width: 288, padding: 8 },
        children: [
          jsx('div', { className: 'px-2 pb-1 text-xs font-medium', children: latest ? `${shorten(latest.command, 40)} · exit ${latest.exit}` : 'No failures yet' }),
          latest && latest.answer && jsx('div', {
            className: 'px-2 pb-2 text-xs text-(--ui-text-tertiary)',
            children: shorten(firstLine(latest.answer, 'Cause'), 200)
          }),
          latest && latest.status === 'stale' && jsx('div', {
            className: 'px-2 pb-2 text-xs text-amber-500',
            children: `Changed while diagnosing: ${(latest.stale_files || []).slice(0, 3).join(', ')}`
          }),
          jsx(MenuButton, { label: 'Open diagnosis', disabled: !hasResult, onClick: close(() => openResult(ctx, latest, report, 'chip-menu')) }),
          jsx(MenuButton, { label: 'Fix it in a new chat (you review, then send)', disabled: !hasResult || latest.resolved, onClick: close(() => fixIt(ctx, latest, report)) }),
          jsx(MenuButton, {
            label: 'Dismiss', disabled: !latest,
            onClick: close(() => report('/dismiss', { id: latest.id }).then(() => refresh(ctx, () => {})))
          })
        ]
      })
    ]
  })
}

export default {
  id: 'backstop',
  name: 'Backstop',
  register(ctx) {
    const report = (path, body) => ctx.rest(path, { method: 'POST', body }).catch(() => null)
    const run = createRunner(ctx)
    ctx.onEvent('plugin.backstop.failure.new', ev => run((ev && ev.payload) || null))
    ctx.onEvent('plugin.backstop.failure.done', () => refresh(ctx, run))
    ctx.onEvent('plugin.backstop.failure.resolved', ev => {
      announceResolved(ev && ev.payload)
      refresh(ctx, run)
    })
    ctx.setInterval(() => refresh(ctx, run), POLL_MS)
    refresh(ctx, run)
    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 125,
      render: () => jsx(Chip, { ctx, report })
    })
  }
}
