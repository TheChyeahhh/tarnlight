// Tarnlight tap for the TypeSafe JavaScript SDK (@typesafe-ai/sdk). Copy this one file into your app. After every Jev
// call, and every failed one, it appends one record to the Tarnlight drop box, ~/.tarnlight/inbox, where the console
// picks it up.
//
//   const tap = await import('./tarnlight-tap.mjs').catch(() => null)   // no tap file: the app runs exactly as before
//   const client = new TypeSafeClient({ fetch: tap?.tarnlightFetch({ source: 'ticket-triage', project: 'support' }) })
//
// (an ES module; README.md has the CommonJS lines and the form for an app that passes its own fetch)
//
//  * the call is left alone: the tap calls fetch as the SDK would and hands the answer straight back, then reads a
//    clone of it beside the SDK, so the SDK's own body, timeouts and retries work as before. When the call is
//    aborted (the SDK's timer or the app), the clone is cancelled too, even if the app's own fetch drops the signal
//  * one record per attempt, as the proxy does: a call the SDK retries leaves one record per try, each with its
//    retry_count. A try that failed says why in error, {"jev": "..."}, in the words the console sorts failures by
//  * writes only while ~/.tarnlight/inbox exists (`tarnlight uninstall` removes it and so turns every tap off) and
//    more than MIN_FREE_BYTES are free on its disk. Never a header, never the key: the key is also cut out of the
//    bodies. Nothing here can throw into the app
//  * whole lines even with many processes on one file: Node opens a file for appending only, so each write lands whole
//  * a label or source for one call: headers: { 'X-Tarnlight-Label': '...' } (or X-Tarnlight-Source) in the call's
//    options, as with the proxy. Like any header you add, it also goes to TypeSafe
//
// No dependencies. Node 20 or later. This file never imports anything from Tarnlight.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const FOLDER = path.join(os.homedir(), '.tarnlight', 'inbox')
const MIN_FREE_BYTES = 2 ** 30 // never help fill a disk

export function tarnlightFetch({ source = 'node', project = null, label = null, fetch: inner } = {}) {
  const send = inner ?? ((url, init) => globalThis.fetch(url, init))
  const file = String(source).replace(/[^A-Za-z0-9._-]+/g, '_').slice(0, 64) || 'node'
  const asText = (x) => (x == null ? null : String(x)) // the console takes these as text only: an id of 7 becomes "7"
  const s = { source: String(source), project: asText(project), label: asText(label), file }
  return async (url, init = {}) => {
    let at = null
    try { at = begin(url, init, s) } catch {}
    if (!at) return send(url, init)
    let res
    try {
      res = await send(url, init)
    } catch (e) {
      end(at, null, undefined, note(e, false))
      throw e
    }
    copy(at, res, init.signal).catch(() => {}) // never an unhandled rejection in the app
    return res // at once: the SDK's timeout watches its own read of the body, which starts only now
  }
}

// Read a clone of the answer as the SDK reads its own, then write the record. A cloned body closes only once both
// copies are done or cancelled, so an abort cancels this one: otherwise a stalled answer stays open until the server
// lets go. A failure here is the one the SDK meets on its own copy, and retries.
async function copy(at, res, signal) {
  let reader
  const stop = () => reader?.cancel().catch(() => {})
  try {
    reader = res.clone().body?.getReader()
    signal?.addEventListener('abort', stop, { once: true })
    if (signal?.aborted) stop()
    const utf8 = new TextDecoder()
    let text = ''
    for (let r; reader && !(r = await reader.read()).done;) text += utf8.decode(r.value, { stream: true })
    signal?.throwIfAborted()
    end(at, res, text + utf8.decode())
  } catch (e) {
    end(at, res, undefined, note(e, true))
  } finally {
    signal?.removeEventListener('abort', stop)
  }
}

function begin(url, init, s) {
  const target = typeof url === 'string' ? url : url?.url ?? String(url)
  const method = (init.method ?? url?.method ?? 'GET').toUpperCase()
  if (method !== 'POST' || !new URL(target).pathname.replace(/\/+$/, '').endsWith('/v1/systemone')) return null
  return { ...s, ts: Date.now() / 1000, t0: performance.now(), headers: new Headers(init.headers), body: init.body }
}

function note(e, answered) {
  const why = [e?.name ?? 'Error', e?.cause?.code ?? e?.cause?.name].filter(Boolean).join(': ')
  const what = /timeout/i.test(why) ? 'timeout'
    : /AbortError/.test(why) ? 'timeout or cancelled' // the SDK's own timer and the app's signal both abort
      : answered ? 'the answer was cut off'
        : /ECONNREFUSED|ENOTFOUND|EAI_AGAIN|EHOSTUNREACH|ENETUNREACH/.test(why) ? 'TypeSafe could not be reached' : 'no answer'
  return `${what} (${why})`
}

// Write the record, once per attempt. text is the whole answer; undefined with a note when it failed.
function end(at, res, text, failed) {
  try {
    const h = at.headers
    const ok = res?.status === 200 && failed === undefined
    const body = text === undefined ? null : parse(text)
    const sdk = h.get('x-typesafe-sdk')
    const retries = Number(h.get('x-typesafe-retry-count') ?? 0)
    const rec = {
      v: 1, ts: at.ts, source: h.get('x-tarnlight-source') ?? at.source, project: at.project,
      label: h.get('x-tarnlight-label') ?? at.label,
      sdk: sdk ? `${(h.get('x-typesafe-runtime') ?? '?').split('/')[0]}/${sdk.split('/').pop()}` : null,
      request_id: res?.headers.get('x-typesafe-request-id') ?? null,
      latency_ms: Math.round(performance.now() - at.t0), status: res?.status ?? null,
      retry_count: Number.isInteger(retries) ? retries : null,
      request: typeof at.body === 'string' ? parse(at.body) : null,
      response: ok ? body : null, error: ok ? null : failed !== undefined ? { jev: failed } : body,
    }
    const key = (h.get('authorization') ?? '').split(' ').slice(1).join(' ').trim()
    write(rec, at.file, key)
  } catch {} // a record lost, never a call
}

function parse(text) {
  if (!text) return null
  try { return JSON.parse(text) } catch { return text }
}

function write(rec, file, key) {
  if (!fs.existsSync(FOLDER)) return
  const disk = fs.statfsSync(FOLDER)
  if (disk.bavail * disk.bsize < MIN_FREE_BYTES) return
  let line = JSON.stringify(rec)
  if (key) line = line.split(JSON.stringify(key).slice(1, -1)).join('[redacted]') // an app that put its key in a state
  fs.appendFileSync(path.join(FOLDER, `${file}-${new Date().toISOString().slice(0, 13)}.jsonl`), line + '\n')
}
