import { afterEach, describe, expect, it, vi } from 'vitest';
import { OmnimexClient } from './client';

describe('OmnimexClient', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('supports hybrid, fts, and vector search plus Windows slash workspace filtering in demo mode', async () => {
    const client = new OmnimexClient();
    client.setMode('demo');

    const hybridRes = await client.search({
      q: 'sqlite wal',
      mode: 'hybrid',
      workspace: 'E:\\AAAAA project test\\omnimex',
    });
    expect(hybridRes.mode).toBe('hybrid');
    expect(hybridRes.hits.length).toBeGreaterThan(0);
    expect(hybridRes.hits[0].fts_score).not.toBeNull();
    expect(hybridRes.hits[0].vector_score).not.toBeNull();

    const ftsRes = await client.search({ q: 'sqlite', mode: 'fts' });
    expect(ftsRes.hits[0].vector_score).toBeNull();

    const vectorRes = await client.search({ q: 'free local distill', mode: 'vector' });
    expect(vectorRes.hits.length).toBeGreaterThan(0);
    expect(vectorRes.hits[0].fts_score).toBeNull();
  });

  it('performs full Memory CRUD, draft review approval, and native memory sync in demo mode', async () => {
    const client = new OmnimexClient();
    client.setMode('demo');

    const initial = await client.listMemories();
    expect(initial.memories.length).toBeGreaterThanOrEqual(4);

    const created = await client.createMemory({
      title: 'Test Vitest decision',
      category: 'decision',
      content: 'Use Vitest + React Testing Library for frontend smoke tests.',
      tags: ['testing', 'frontend'],
      reviewed: false,
    });
    expect(created.id).toMatch(/^mem_/);
    expect(created.origin).toBe('manual');
    expect(created.reviewed).toBe(false);

    const updated = await client.updateMemory(created.id, {
      reviewed: true,
      title: 'Approved Vitest decision',
    });
    expect(updated.reviewed).toBe(true);
    expect(updated.title).toBe('Approved Vitest decision');

    const syncRes = await client.syncMemories({ import_native: true });
    expect(syncRes.imported_native).toBeGreaterThanOrEqual(1);

    const delRes = await client.deleteMemory(created.id);
    expect(delRes.deleted).toBe(true);
  });

  it('generates redacted HandoffBrief and updates distiller settings + backfill in demo mode', async () => {
    const client = new OmnimexClient();
    client.setMode('demo');

    const sessionsRes = await client.listSessions();
    expect(sessionsRes.sessions.length).toBeGreaterThan(0);
    const detailRes = await client.getSession(sessionsRes.sessions[0].id);
    expect(detailRes.session.turns?.length).toBeGreaterThan(0);

    const brief = await client.generateHandoff({
      session_id: sessionsRes.sessions[0].id,
      target_agent: 'codex',
      include_memories: true,
      custom_instructions: 'Finish API tests',
    });
    expect(brief.target_agent).toBe('codex');
    expect(brief.redacted).toBe(true);
    expect(brief.markdown).toContain('Finish API tests');
    expect(brief.token_estimate).toBeGreaterThan(0);

    const initialSettings = await client.getDistillerSettings();
    expect(initialSettings.provider).toBe('heuristic');
    expect(initialSettings.zero_cost_mode).toBe(true);

    const updatedSettings = await client.updateDistillerSettings({
      provider: 'ollama',
      model: 'qwen2.5:7b-instruct',
      base_url: 'http://127.0.0.1:11434/v1',
    });
    expect(updatedSettings.provider).toBe('ollama');
    expect(updatedSettings.zero_cost_mode).toBe(true);

    const backfill = await client.runDistillBackfill({ limit: 10 });
    expect(backfill.status).toBe('completed');
    expect(backfill.created_memories).toBeGreaterThan(0);
  });

  it('shares initial probeHealth() and falls back cleanly on Vite proxy 500 or preview 404 HTML in auto mode', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/health')) {
        return new Response('Proxy error: ECONNREFUSED', {
          status: 500,
          headers: { 'content-type': 'text/plain' },
        });
      }
      return new Response('<html>404</html>', {
        status: 404,
        headers: { 'content-type': 'text/html' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const client = new OmnimexClient();
    const [sessions, adapters] = await Promise.all([
      client.listSessions(),
      client.listAdapters(),
    ]);

    expect(sessions.sessions.length).toBeGreaterThan(0);
    expect(adapters.adapters.length).toBeGreaterThan(0);
    expect(client.getConnectionState().usingMock).toBe(true);
    // Only one /api/health probe call should have been issued for concurrent startup calls
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('falls back on non-JSON 200 SPA HTML and preserves FastAPI JSON 404 errors in auto mode', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/health')) {
        return new Response(
          JSON.stringify({ status: 'ok', version: '0.1.0', db_ready: true, distiller_provider: 'heuristic' }),
          { status: 200, headers: { 'content-type': 'application/json' } }
        );
      }
      if (url.includes('/api/sessions/missing')) {
        return new Response(
          JSON.stringify({ error: { code: 'NOT_FOUND', message: "Session 'missing' was not found" } }),
          { status: 404, headers: { 'content-type': 'application/json' } }
        );
      }
      return new Response('<!doctype html><html></html>', {
        status: 200,
        headers: { 'content-type': 'text/html' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const client = new OmnimexClient();
    await client.probeHealth();
    expect(client.getConnectionState().usingMock).toBe(false);

    // Real FastAPI JSON 404 should surface as a clean API error, not trigger demo fallback
    await expect(client.getSession('missing')).rejects.toThrow("Session 'missing' was not found");
    expect(client.getConnectionState().usingMock).toBe(false);

    // Non-JSON 200 (e.g. vite preview SPA fallback) should trigger demo fallback in auto mode
    const adapters = await client.listAdapters();
    expect(adapters.adapters.length).toBeGreaterThan(0);
    expect(client.getConnectionState().usingMock).toBe(true);
  });

  it('throws on network/proxy failures when explicitly set to live mode', async () => {
    const fetchMock = vi.fn(async () =>
      new Response('Bad Gateway', { status: 502, headers: { 'content-type': 'text/plain' } })
    );
    vi.stubGlobal('fetch', fetchMock);

    const client = new OmnimexClient();
    client.setMode('live');

    await expect(client.listSessions()).rejects.toThrow('HTTP 502');
    expect(client.getConnectionState().usingMock).toBe(false);
  });

  it('supports paginated getSession and single-turn getSessionTurn in demo mode', async () => {
    const client = new OmnimexClient();
    client.setMode('demo');
    const sessions = await client.listSessions();
    const sid = sessions.sessions[0].id;

    const page = await client.getSession(sid, { turn_offset: 0, turn_limit: 1 });
    expect(page.session.turns?.length).toBe(1);
    expect(page.turn_offset).toBe(0);
    expect(page.total_turns).toBeGreaterThanOrEqual(1);

    const single = await client.getSessionTurn(sid, 0);
    expect(single.turn.turn_index).toBe(0);
    expect(single.truncated).toBe(false);
  });
});
