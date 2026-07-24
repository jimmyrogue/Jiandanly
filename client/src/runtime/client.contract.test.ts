/**
 * Contract round-trip tests — real HTTP against a live runtime.
 *
 * Catches every shape drift between TS client + Python runtime that
 * the regular `client.test.ts` (MockTransport) can't see. The
 * Phase 5'+ migration shipped with 9+ silent contract drifts; this
 * suite is the floor that keeps them from coming back.
 *
 * Skipped entirely when `VITE_TEST_RUNTIME_URL` isn't set so
 * `pnpm test` stays hermetic. CI sets it after starting the runtime.
 *
 * The suite boots a real Runtime and uses its explicit fake-model seam, so
 * HTTP, commands, SSE and the compiled agent loop are all exercised without
 * depending on an external model service.
 */
import { describe, it, expect } from 'vitest'
import { mkdtempSync, realpathSync, rmSync } from 'node:fs'
import { createServer, request as httpRequest, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  probeRuntime,
  createLocalRun,
  listLocalRuns,
  parseAgentSSEBuffer,
  streamLocalRun,
  authorizeLocalWorkspace,
  listAuthorizedWorkspaces,
  revokeLocalWorkspace,
} from './client'

const BASE_URL = process.env.VITE_TEST_RUNTIME_URL
const TOKEN = process.env.VITE_TEST_RUNTIME_TOKEN ?? 'dev-local-token'
const CONTRACT_SETTINGS = { memory: 'off', skills: 'off', mcp: 'off' } as const
const CONTRACT_MODE = 'local:test:model'

// Vitest 2.x — describe.skipIf(condition) drops the whole block.
describe.skipIf(!BASE_URL)('flow:P1-P4 > contract: runtime HTTP (live runtime)', () => {
  const config = { baseURL: BASE_URL!, token: TOKEN }

  // -----------------------------------------------------------------
  // GET /v1/health
  // -----------------------------------------------------------------
  describe('probeRuntime', () => {
    it('flow:P1 > reports online=true via {status: "ok"} envelope', async () => {
      const probe = await probeRuntime(BASE_URL!)
      expect(probe.online).toBe(true)
      expect(probe.status).toBe('ok')
    })
  })

  // -----------------------------------------------------------------
  // POST/GET /v1/runs — flat shape, list endpoint exists
  // -----------------------------------------------------------------
  describe('runs', () => {
    it('POST returns flat LocalRun (no {run: ...} wrapper)', async () => {
      const run = await createLocalRun(
        {
          commandId: 'cmd_contract_create',
          clientMessageId: 'msg_contract_create',
          goal: 'contract test run',
          mode: CONTRACT_MODE,
          settings: CONTRACT_SETTINGS,
        },
        config,
      )
      expect(run.id).toMatch(/^run_/)
      expect(run.goal).toBe('contract test run')
      expect(run.status).toMatch(/queued|running|completed/)
      // Anti-regression — earlier runtime wrapped in {run: ...}.
      expect((run as unknown as { run?: unknown }).run).toBeUndefined()
    })

    it('GET /runs returns the list (not 404)', async () => {
      // Just smoke that the route is registered and returns an array.
      // listLocalRuns wraps the actual fetch; if the route 404s the
      // helper throws.
      const runs = await listLocalRuns(config)
      expect(Array.isArray(runs)).toBe(true)
    })

    it('previously-created run appears in list with envelope fields', async () => {
      const created = await createLocalRun(
        {
          commandId: 'cmd_contract_list',
          clientMessageId: 'msg_contract_list',
          goal: 'list-me',
          mode: CONTRACT_MODE,
          settings: CONTRACT_SETTINGS,
        },
        config,
      )
      const runs = await listLocalRuns(config)
      const found = runs.find((r) => r.id === created.id)
      expect(found).toBeDefined()
      expect(found?.goal).toBe('list-me')
    })

    it('flow:P2-P3 > replays the same create command and rejects changed content', async () => {
      const suffix = Date.now().toString(36)
      const input = {
        commandId: `cmd_contract_idempotent_${suffix}`,
        clientMessageId: `msg_contract_idempotent_${suffix}`,
        goal: 'idempotent create',
        mode: CONTRACT_MODE,
        settings: CONTRACT_SETTINGS,
      } as const
      const first = await createLocalRun(input, config)
      const replay = await createLocalRun(input, config)
      expect(replay.id).toBe(first.id)
      await expect(createLocalRun({ ...input, goal: 'conflicting create' }, config)).rejects.toThrow(/different content/i)
    })
  })

  // -----------------------------------------------------------------
  // SSE stream — the most drift-prone interface (invariant #3). This test's
  // job is the CLIENT side: that streamLocalRun → parseAgentSSE reads the
  // live wire envelope correctly (event_type populated, terminal events
  // delivered, completion flagged). The content/event payloads themselves are
  // asserted deterministically runtime-side in test_sse_contract.py (which
  // drives the fake model in-process), so we don't re-assert streamed text
  // here — that depends on the subprocess picking up SHEJANE_FAKE_LLM, which
  // isn't this layer's contract.
  // -----------------------------------------------------------------
  describe('runs stream', () => {
    it('flow:P4-P5 > parses SSE and observes the worker-started transition', async () => {
      const created = await createLocalRun(
        {
          commandId: 'cmd_contract_stream',
          clientMessageId: 'msg_contract_stream',
          goal: 'contract stream',
          mode: CONTRACT_MODE,
          settings: CONTRACT_SETTINGS,
        },
        config,
      )
      const events: string[] = []
      const result = await streamLocalRun(created.id, config, {
        onEvent: (event) => events.push(event.event_type),
        onDelta: () => {},
      })

      // The client parsed real envelopes off the wire (event_type populated,
      // never undefined — the silent-no-op bug this guards against).
      expect(events.length).toBeGreaterThan(0)
      expect(events.every((name) => typeof name === 'string' && name.length > 0)).toBe(true)
      expect(events).toContain('run.started')
      expect(events).toContain('run.completed')
      expect(result.completed).toBe(true)
    })

    it('parses a real Runtime stream when UTF-8 and SSE frames are split across tiny chunks', async () => {
      const suffix = Date.now().toString(36)
      const created = await createLocalRun({
        commandId: `cmd_contract_fragmented_${suffix}`,
        clientMessageId: `msg_contract_fragmented_${suffix}`,
        goal: 'fragmented SSE 你好',
        mode: CONTRACT_MODE,
        settings: CONTRACT_SETTINGS,
      }, config)
      const fragmentingFetcher: typeof fetch = async (input, init) => {
        const original = await fetch(input, init)
        const bytes = new Uint8Array(await original.arrayBuffer())
        let offset = 0
        const body = new ReadableStream<Uint8Array>({
          pull(controller) {
            if (offset >= bytes.length) {
              controller.close()
              return
            }
            const width = (offset % 7) + 1
            controller.enqueue(bytes.slice(offset, offset + width))
            offset += width
          },
        })
        return new Response(body, {
          status: original.status,
          statusText: original.statusText,
          headers: original.headers,
        })
      }
      const events: Array<{ event_type: string; seq?: number }> = []
      let content = ''
      const result = await streamLocalRun(created.id, config, {
        onEvent: event => events.push(event),
        onDelta: delta => { content += delta },
      }, fragmentingFetcher)

      expect(result.completed).toBe(true)
      expect(events.map(event => event.event_type)).toContain('run.completed')
      expect(content).toBe('Fake runtime reply for the SSE contract test.')
    })

    it('replays only durable events after the requested cursor', async () => {
      const suffix = Date.now().toString(36)
      const created = await createLocalRun({
        commandId: `cmd_contract_cursor_${suffix}`,
        clientMessageId: `msg_contract_cursor_${suffix}`,
        goal: 'cursor replay',
        mode: CONTRACT_MODE,
        settings: CONTRACT_SETTINGS,
      }, config)
      const first: Array<{ seq?: number; event_type: string }> = []
      await streamLocalRun(created.id, config, {
        onEvent: (event) => first.push(event),
        onDelta: () => undefined,
      })
      const durable = first.filter((event): event is { seq: number; event_type: string } => typeof event.seq === 'number')
      expect(durable.length).toBeGreaterThan(1)
      const after = durable.at(-2)!.seq
      const replay: Array<{ seq?: number; event_type: string }> = []
      await streamLocalRun(created.id, config, {
        afterSeq: after,
        onEvent: (event) => replay.push(event),
        onDelta: () => undefined,
      })
      expect(replay.filter((event) => typeof event.seq === 'number').map((event) => event.seq)).toEqual(
        durable.filter((event) => event.seq > after).map((event) => event.seq),
      )
    })

    it('rejects a future cursor through the live Runtime reset contract', async () => {
      const suffix = Date.now().toString(36)
      const created = await createLocalRun({
        commandId: `cmd_contract_future_cursor_${suffix}`,
        clientMessageId: `msg_contract_future_cursor_${suffix}`,
        goal: 'future cursor contract',
        mode: CONTRACT_MODE,
        settings: CONTRACT_SETTINGS,
      }, config)
      const events: Array<{ seq?: number }> = []
      await streamLocalRun(created.id, config, {
        onEvent: event => events.push(event),
        onDelta: () => undefined,
      })
      const latest = Math.max(...events.flatMap(event => typeof event.seq === 'number' ? [event.seq] : []))

      await expect(streamLocalRun(created.id, config, {
        afterSeq: latest + 1,
        onEvent: () => undefined,
        onDelta: () => undefined,
      })).rejects.toMatchObject({
        name: 'LocalStreamCursorResetRequiredError',
      })
    })

    it('returns a stable run_not_found error for an unknown stream', async () => {
      await expect(streamLocalRun(`run_missing_${Date.now().toString(36)}`, config, {
        onEvent: () => undefined,
        onDelta: () => undefined,
      })).rejects.toMatchObject({
        status: 404,
        code: 'run_not_found',
      })
    })

    it('keeps the Run alive when one SSE subscriber disconnects mid-stream', async () => {
      const suffix = Date.now().toString(36)
      const created = await createLocalRun({
        commandId: `cmd_contract_disconnect_${suffix}`,
        clientMessageId: `msg_contract_disconnect_${suffix}`,
        goal: '[[e2e:disconnect]] finish after the first subscriber leaves',
        mode: CONTRACT_MODE,
        settings: CONTRACT_SETTINGS,
      }, config)
      const response = await fetch(
        `${BASE_URL}/v1/runs/${encodeURIComponent(created.id)}/stream`,
        { headers: { Authorization: `Bearer ${TOKEN}` } },
      )
      expect(response.status).toBe(200)
      const reader = response.body!.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      const observed: Array<{ event_type: string; seq?: number }> = []
      while (!observed.some(event => typeof event.seq === 'number')) {
        const chunk = await reader.read()
        expect(chunk.done).toBe(false)
        buffer += decoder.decode(chunk.value, { stream: true })
        const parsed = parseAgentSSEBuffer(buffer)
        buffer = parsed.rest
        for (const item of parsed.events) {
          if (item.type === 'agent') observed.push(item.event)
        }
      }
      expect(buffer).not.toContain('data: [DONE]')
      const resumeAfter = Math.max(
        ...observed.flatMap(event => typeof event.seq === 'number' ? [event.seq] : []),
      )
      await reader.cancel()

      const events: Array<{ event_type: string; seq?: number }> = []
      await streamLocalRun(created.id, config, {
        afterSeq: resumeAfter,
        onEvent: event => events.push(event),
        onDelta: () => undefined,
      })
      expect(events.filter(event => event.event_type === 'run.completed')).toHaveLength(1)
      expect(events.every(event => typeof event.seq !== 'number' || event.seq > resumeAfter)).toBe(true)
      expect(events.map(event => event.event_type)).not.toContain('run.canceled')
      expect(events.map(event => event.event_type)).not.toContain('run.failed')
    })

    it.each(['half-close', 'reset'] as const)(
      'recovers the exact durable cursor after a real TCP %s mid-frame',
      async (fault) => {
        const suffix = `${fault}_${Date.now().toString(36)}`
        const created = await createLocalRun({
          commandId: `cmd_contract_tcp_${suffix}`,
          clientMessageId: `msg_contract_tcp_${suffix}`,
          goal: '[[e2e:disconnect]] survive a transport fault',
          mode: CONTRACT_MODE,
          settings: CONTRACT_SETTINGS,
        }, config)
        const proxy = await startFaultingSseProxy(BASE_URL!, fault)
        const before: Array<{ event_type: string; seq?: number }> = []
        let completed = false
        let transportError: unknown
        try {
          const result = await streamLocalRun(created.id, {
            baseURL: proxy.baseURL,
            token: TOKEN,
          }, {
            onEvent: event => before.push(event),
            onDelta: () => undefined,
          })
          completed = result.completed
        } catch (error) {
          transportError = error
        } finally {
          await proxy.close()
        }

        expect(completed).toBe(false)
        if (fault === 'reset') expect(transportError).toBeDefined()
        const durableBefore = before.filter(
          (event): event is { event_type: string; seq: number } => typeof event.seq === 'number',
        )
        expect(durableBefore.length).toBeGreaterThan(0)
        const resumeAfter = durableBefore.at(-1)!.seq

        const after: Array<{ event_type: string; seq?: number }> = []
        const resumed = await streamLocalRun(created.id, config, {
          afterSeq: resumeAfter,
          onEvent: event => after.push(event),
          onDelta: () => undefined,
        })
        expect(resumed.completed).toBe(true)
        expect(after.filter(event => event.event_type === 'run.completed')).toHaveLength(1)
        expect(after.every(event => typeof event.seq !== 'number' || event.seq > resumeAfter)).toBe(true)
        const allSeqs = [...durableBefore, ...after]
          .flatMap(event => typeof event.seq === 'number' ? [event.seq] : [])
        expect(new Set(allSeqs).size).toBe(allSeqs.length)
        expect(allSeqs).toEqual([...allSeqs].sort((left, right) => left - right))
      },
    )

    it('delivers the same ordered durable log to concurrent SSE subscribers', async () => {
      const suffix = Date.now().toString(36)
      const created = await createLocalRun({
        commandId: `cmd_contract_subscribers_${suffix}`,
        clientMessageId: `msg_contract_subscribers_${suffix}`,
        goal: 'concurrent subscriber contract',
        mode: CONTRACT_MODE,
        settings: CONTRACT_SETTINGS,
      }, config)
      const first: Array<{ event_type: string; seq?: number }> = []
      const second: Array<{ event_type: string; seq?: number }> = []
      await Promise.all([
        streamLocalRun(created.id, config, {
          onEvent: event => first.push(event),
          onDelta: () => undefined,
        }),
        streamLocalRun(created.id, config, {
          onEvent: event => second.push(event),
          onDelta: () => undefined,
        }),
      ])
      const durable = (events: Array<{ event_type: string; seq?: number }>) => events
        .filter((event): event is { event_type: string; seq: number } => typeof event.seq === 'number')
        .map(event => ({ type: event.event_type, seq: event.seq }))

      expect(durable(first)).toEqual(durable(second))
      expect(durable(first).map(event => event.type).filter(type => type === 'run.completed')).toEqual([
        'run.completed',
      ])
    })

    it('finishes the Run and preserves a burst while an SSE consumer pauses reading', async () => {
      const suffix = Date.now().toString(36)
      const created = await createLocalRun({
        commandId: `cmd_contract_slow_consumer_${suffix}`,
        clientMessageId: `msg_contract_slow_consumer_${suffix}`,
        goal: '[[e2e:burst]] stream enough data to apply consumer backpressure',
        mode: CONTRACT_MODE,
        settings: CONTRACT_SETTINGS,
      }, config)
      const response = await fetch(
        `${BASE_URL}/v1/runs/${encodeURIComponent(created.id)}/stream`,
        { headers: { Authorization: `Bearer ${TOKEN}` } },
      )
      expect(response.status).toBe(200)

      const deadline = Date.now() + 15_000
      let terminal = false
      while (!terminal && Date.now() < deadline) {
        const run = (await listLocalRuns(config)).find(candidate => candidate.id === created.id)
        terminal = run?.status === 'completed'
        if (!terminal) await new Promise(resolve => setTimeout(resolve, 25))
      }
      expect(terminal).toBe(true)

      const parsed = parseAgentSSEBuffer(await response.text())
      const events = parsed.events.flatMap(item => item.type === 'agent' ? [item.event] : [])
      const deltas = events
        .filter(event => event.event_type === 'llm.delta')
        .map(event => event.payload?.content)
        .filter((content): content is string => typeof content === 'string')
      expect(parsed.rest).toBe('')
      expect(parsed.events.filter(item => item.type === 'done')).toHaveLength(1)
      expect(events.filter(event => event.event_type === 'run.completed')).toHaveLength(1)
      expect(events.filter(event => event.event_type === 'llm.delta').every(
        event => event.seq === undefined,
      )).toBe(true)
      expect(deltas.length).toBeGreaterThanOrEqual(256)
      expect(deltas.join('')).toContain('E2E_BURST_000_')
      expect(deltas.join('')).toContain('E2E_BURST_255_')
    }, 20_000)
  })

  // -----------------------------------------------------------------
  // POST/DELETE /v1/workspaces — flat shape
  // -----------------------------------------------------------------
  describe('workspaces', () => {
    it('POST returns flat LocalWorkspaceAuthorization (no wrapper)', async () => {
      const path = mkdtempSync(join(tmpdir(), 'shejane-contract-ws-'))
      const ws = await authorizeLocalWorkspace(path, config)
      expect(ws.id).toBeTruthy()
      expect(ws.path).toBe(realpathSync(path))
      expect((ws as unknown as { workspace?: unknown }).workspace).toBeUndefined()

      // Cleanup
      await revokeLocalWorkspace(ws.id, config)
      rmSync(path, { recursive: true, force: true })
    })

    it('DELETE returns the deleted record (flat shape)', async () => {
      const path = mkdtempSync(join(tmpdir(), 'shejane-contract-del-'))
      const ws = await authorizeLocalWorkspace(path, config)
      const deleted = await revokeLocalWorkspace(ws.id, config)
      // Returns the deleted record, NOT {deleted: bool}.
      expect(deleted.id).toBe(ws.id)
      expect(deleted.path).toBe(ws.path)
      rmSync(path, { recursive: true, force: true })
    })

    it('list returns LocalWorkspaceAuthorization[] from {workspaces} envelope', async () => {
      const path = mkdtempSync(join(tmpdir(), 'shejane-contract-list-'))
      const ws = await authorizeLocalWorkspace(path, config)
      const all = await listAuthorizedWorkspaces(config)
      expect(Array.isArray(all)).toBe(true)
      expect(all.find((w) => w.id === ws.id)).toBeDefined()
      await revokeLocalWorkspace(ws.id, config)
      rmSync(path, { recursive: true, force: true })
    })
  })

  // -----------------------------------------------------------------
  // Error shape — FastAPI `detail` is what runtime emits.
  // localErrorMessage must surface it (not the generic HTTP code).
  // -----------------------------------------------------------------
  describe('error shape', () => {
    it('POST /runs with missing goal returns readable message', async () => {
      try {
        await createLocalRun(
          { commandId: 'cmd_contract_empty', clientMessageId: 'msg_contract_empty', goal: '', mode: CONTRACT_MODE },
          config,
        )
        expect.fail('expected error from empty goal')
      } catch (error) {
        const msg = error instanceof Error ? error.message : String(error)
        // Should be the specific "goal required" message from the
        // runtime's HTTPException(detail=...), NOT a generic
        // "Runtime HTTP 400". Anti-regression for client.ts
        // localErrorMessage reading `body.detail`.
        expect(msg.toLowerCase()).toContain('goal')
      }
    })
  })
})

async function startFaultingSseProxy(
  upstreamBaseURL: string,
  fault: 'half-close' | 'reset',
): Promise<{ baseURL: string; close: () => Promise<void> }> {
  const server: Server = createServer((request, response) => {
    const upstreamURL = new URL(request.url ?? '/', upstreamBaseURL)
    const upstream = httpRequest(upstreamURL, {
      method: request.method,
      headers: request.headers,
    }, (upstreamResponse) => {
      const headers = { ...upstreamResponse.headers }
      delete headers.connection
      delete headers['content-length']
      delete headers['transfer-encoding']
      response.writeHead(upstreamResponse.statusCode ?? 502, headers)
      let pending = Buffer.alloc(0)
      let faulted = false
      upstreamResponse.on('data', (chunk: Buffer) => {
        if (faulted) return
        pending = Buffer.concat([pending, chunk])
        const lf = pending.indexOf('\n\n')
        const crlf = pending.indexOf('\r\n\r\n')
        const boundary = lf >= 0 ? lf + 2 : crlf >= 0 ? crlf + 4 : -1
        if (boundary < 0) return
        faulted = true
        const partialNextFrame = pending.subarray(0, Math.min(pending.length, boundary + 5))
        upstream.destroy()
        if (fault === 'half-close') {
          response.end(partialNextFrame)
          return
        }
        response.write(partialNextFrame, () => {
          response.socket?.resetAndDestroy()
        })
      })
      upstreamResponse.on('end', () => {
        if (!faulted) response.end(pending)
      })
    })
    upstream.on('error', (error) => {
      if (!response.destroyed) response.destroy(error)
    })
    request.pipe(upstream)
  })
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => resolve())
  })
  const address = server.address() as AddressInfo
  return {
    baseURL: `http://127.0.0.1:${address.port}`,
    close: () => new Promise<void>((resolve, reject) => {
      server.close(error => error ? reject(error) : resolve())
    }),
  }
}
