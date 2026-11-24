import {
  AGENT_META,
  INITIAL_ADAPTERS,
  INITIAL_DISTILLER,
  INITIAL_MEMORIES,
  INITIAL_NATIVE_IMPORTS,
  INITIAL_SESSIONS,
} from './mockData';
import type {
  AdapterHealth,
  AdaptersResponse,
  CreateMemoryPayload,
  DistillBackfillRequest,
  DistillBackfillResponse,
  DistillerSettings,
  HandoffBrief,
  HandoffRequest,
  HealthResponse,
  MemoriesResponse,
  MemoryCard,
  MemoryCategory,
  NativeMemoryImportStatus,
  NormalizedSession,
  OmnimexEvent,
  SavedSkillSummary,
  ScanRequest,
  ScanResponse,
  SearchHit,
  SearchMode,
  SearchResponse,
  SessionDetailResponse,
  SessionsResponse,
  SessionTurnResponse,
  SkillDraft,
  SkillGenerateRequest,
  SkillInstallRequest,
  SkillInstallResult,
  SkillListResponse,
  SkillSaveRequest,
  StatsResponse,
  SyncMemoriesPayload,
  SyncMemoriesResponse,
  UpdateDistillerSettingsPayload,
  UpdateMemoryPayload,
} from './types';

export type ConnectionMode = 'auto' | 'live' | 'demo';

export interface SearchParams {
  q?: string;
  agent?: string;
  workspace?: string;
  mode?: SearchMode;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
}

export interface SessionsParams {
  agent?: string;
  workspace?: string;
  q?: string;
  date_from?: string;
  date_to?: string;
  include_subagents?: boolean;
  limit?: number;
  offset?: number;
}

export interface MemoriesParams {
  category?: MemoryCategory | '';
  distill_status?: string;
  origin?: string;
  reviewed?: boolean;
  workspace?: string;
  agent?: string;
  q?: string;
}

export interface ConnectionState {
  mode: ConnectionMode;
  usingMock: boolean;
  backendReachable: boolean;
  lastError: string | null;
}

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ?? '';

function normalizeSlash(value?: string | null): string {
  return (value ?? '').replace(/\\/g, '/').toLowerCase();
}

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '')
    .slice(0, 48) || 'memory';
}

function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') {
      sp.set(k, String(v));
    }
  }
  const qs = sp.toString();
  return qs ? `?${qs}` : '';
}

function clone<T>(val: T): T {
  return JSON.parse(JSON.stringify(val)) as T;
}

class MockStore {
  sessions: NormalizedSession[] = clone(INITIAL_SESSIONS);
  memories: MemoryCard[] = clone(INITIAL_MEMORIES);
  nativeImports: NativeMemoryImportStatus[] = clone(INITIAL_NATIVE_IMPORTS);
  adapters: AdapterHealth[] = clone(INITIAL_ADAPTERS);
  distiller: DistillerSettings = clone(INITIAL_DISTILLER);
  skills: SavedSkillSummary[] = [];

  search(params: SearchParams): SearchResponse {
    const q = (params.q ?? '').trim().toLowerCase();
    const mode: SearchMode = params.mode ?? 'hybrid';
    const agentFilter = params.agent
      ? params.agent.split(',').map((s) => s.trim()).filter(Boolean)
      : [];
    const wsFilter = normalizeSlash(params.workspace);

    const hits: SearchHit[] = [];
    const agentCounts: Record<string, number> = {};
    const workspaceCounts: Record<string, number> = {};

    for (const session of this.sessions) {
      if (agentFilter.length > 0 && !agentFilter.includes(session.agent)) continue;
      if (wsFilter && !normalizeSlash(session.workspace).includes(wsFilter)) continue;

      for (const turn of session.turns ?? []) {
        if (params.date_from && turn.timestamp && turn.timestamp < params.date_from) continue;
        if (params.date_to && turn.timestamp && turn.timestamp > `${params.date_to}T23:59:59Z`) continue;

        const haystack = `${session.title}\n${turn.content}\n${turn.thinking ?? ''}`.toLowerCase();
        const tokens = q ? q.split(/\s+/).filter(Boolean) : [];
        let matched = tokens.length === 0;
        let tokenMatches = 0;

        for (const tok of tokens) {
          if (haystack.includes(tok)) {
            tokenMatches += 1;
            matched = true;
          }
        }

        // Semantic-like synonym matching in vector/hybrid mock mode
        if (!matched && (mode === 'vector' || mode === 'hybrid') && q) {
          const semanticAliases: Array<[string[], string[]]> = [
            [['lock', 'sqlite', 'wal', 'cursor'], ['vscdb', 'sidecar', 'shutil', 'sqlite', 'wal']],
            [['secret', 'token', 'redact', 'key'], ['scrub', 'redacted', 'sk-', 'ghp_', 'dsn']],
            [['cost', 'free', 'local', 'distill'], ['heuristic', 'zero', 'ollama', 'onnx']],
            [['frontend', 'build', 'vite'], ['tailwind', 'npm', 'vite']],
          ];
          for (const [triggers, targets] of semanticAliases) {
            if (triggers.some((t) => q.includes(t)) && targets.some((t) => haystack.includes(t))) {
              matched = true;
              tokenMatches = 1;
              break;
            }
          }
        }

        if (!matched) continue;

        const ftsScore =
          tokens.length === 0 ? 0.6 : Math.min(0.98, 0.52 + (tokenMatches / tokens.length) * 0.44);
        const vectorScore =
          tokens.length === 0 ? 0.65 : Math.min(0.96, 0.58 + (tokenMatches / tokens.length) * 0.35);
        const score =
          mode === 'fts'
            ? ftsScore
            : mode === 'vector'
              ? vectorScore
              : Number(((ftsScore + vectorScore) / 2).toFixed(3));

        agentCounts[session.agent] = (agentCounts[session.agent] ?? 0) + 1;
        if (session.workspace) {
          workspaceCounts[session.workspace] = (workspaceCounts[session.workspace] ?? 0) + 1;
        }

        const idx = q ? turn.content.toLowerCase().indexOf(tokens[0] ?? q) : 0;
        const start = Math.max(0, idx - 40);
        const rawSnippet = turn.content.slice(start, start + 180).replace(/\s+/g, ' ').trim();
        const snippet = (start > 0 ? '...' : '') + rawSnippet + (turn.content.length > start + 180 ? '...' : '');

        hits.push({
          turn_id: turn.id,
          session_id: session.id,
          agent: session.agent,
          session_title: session.title,
          workspace: session.workspace,
          turn_index: turn.turn_index,
          role: turn.role,
          snippet,
          content: turn.content,
          timestamp: turn.timestamp,
          score,
          fts_score: mode === 'vector' ? null : Number(ftsScore.toFixed(3)),
          vector_score: mode === 'fts' ? null : Number(vectorScore.toFixed(3)),
          match_mode: mode,
        });
      }
    }

    hits.sort((a, b) => b.score - a.score);
    const offset = params.offset ?? 0;
    const limit = params.limit ?? 20;

    return {
      query: params.q ?? '',
      mode,
      total: hits.length,
      took_ms: 9.4,
      hits: hits.slice(offset, offset + limit),
      facets: {
        agents: agentCounts,
        workspaces: workspaceCounts,
      },
    };
  }

  listSessions(params: SessionsParams): SessionsResponse {
    const agentFilter = params.agent
      ? params.agent.split(',').map((s) => s.trim()).filter(Boolean)
      : [];
    const wsFilter = normalizeSlash(params.workspace);
    const q = (params.q ?? '').trim().toLowerCase();

    const filtered = this.sessions
      .filter((s) => {
        if (agentFilter.length > 0 && !agentFilter.includes(s.agent)) return false;
        if (wsFilter && !normalizeSlash(s.workspace).includes(wsFilter)) return false;
        if (params.date_from && s.updated_at && s.updated_at < params.date_from) return false;
        if (params.date_to && s.updated_at && s.updated_at > `${params.date_to}T23:59:59Z`) return false;
        if (q) {
          const text = `${s.title} ${s.summary ?? ''} ${s.tags.join(' ')}`.toLowerCase();
          if (!text.includes(q)) return false;
        }
        if (params.include_subagents === false) {
          const turns = s.turns ?? [];
          if (turns.length > 0 && !turns.some((t) => t.role === 'user')) {
            return false;
          }
        }
        return true;
      })
      .sort((a, b) => (b.updated_at ?? '').localeCompare(a.updated_at ?? ''))
      .map((s) => ({ ...s, turns: undefined }));

    const offset = params.offset ?? 0;
    const limit = params.limit ?? 50;
    return {
      total: filtered.length,
      limit,
      offset,
      sessions: filtered.slice(offset, offset + limit),
    };
  }

  getSession(
    id: string,
    params: { turn_offset?: number; turn_limit?: number; max_turn_chars?: number; qa_only?: boolean } = {}
  ): SessionDetailResponse {
    const session = this.sessions.find((s) => s.id === id);
    if (!session) {
      throw new Error(`Session '${id}' not found`);
    }
    const allTurns = session.turns ?? [];
    const filteredTurns = params.qa_only
      ? allTurns.filter((t) => {
          if (t.role === 'user') return true;
          if (t.role !== 'assistant') return false;
          const trimmed = (t.content ?? '').trim();
          return trimmed !== '' && !/^\[Tool:/i.test(trimmed);
        })
      : allTurns;
    const total = filteredTurns.length;
    const limit = params.turn_limit ?? 40;
    const offset =
      params.turn_offset !== undefined
        ? Math.max(0, Math.min(params.turn_offset, total))
        : Math.max(0, total - limit);
    const slicedTurns = filteredTurns.slice(offset, offset + limit);
    const linked = this.memories.filter((m) => m.source_session_id === id);
    const clonedSession = clone(session);
    clonedSession.turns = clone(slicedTurns);
    clonedSession.turn_count = Math.max(clonedSession.turn_count ?? 0, allTurns.length);
    return {
      session: clonedSession,
      linked_memories: clone(linked),
      total_turns: total,
      turn_offset: offset,
      turn_limit: limit,
      has_earlier_turns: offset > 0,
      has_later_turns: offset + slicedTurns.length < total,
      truncated_turn_indices: [],
    };
  }

  getSessionTurn(id: string, turnIndex: number): SessionTurnResponse {
    const session = this.sessions.find((s) => s.id === id);
    if (!session) {
      throw new Error(`Session '${id}' not found`);
    }
    const turn = (session.turns ?? []).find((t) => t.turn_index === turnIndex);
    if (!turn) {
      throw new Error(`Turn #${turnIndex} for session '${id}' not found`);
    }
    return { turn: clone(turn), truncated: false };
  }

  listMemories(params: MemoriesParams): MemoriesResponse {
    const q = (params.q ?? '').trim().toLowerCase();
    const wsFilter = normalizeSlash(params.workspace);

    const countsByCategory: Record<MemoryCategory, number> = {
      preference: 0,
      decision: 0,
      pitfall: 0,
      fact: 0,
    };
    const countsByStatus: Record<'heuristic' | 'llm_distilled' | 'pending' | 'manual', number> = {
      heuristic: 0,
      llm_distilled: 0,
      pending: 0,
      manual: 0,
    };

    for (const m of this.memories) {
      countsByCategory[m.category] += 1;
      if (m.origin === 'manual') {
        countsByStatus.manual += 1;
      } else {
        countsByStatus[m.distill_status] += 1;
      }
    }

    const filtered = this.memories.filter((m) => {
      if (params.category && m.category !== params.category) return false;
      if (params.distill_status && m.distill_status !== params.distill_status) return false;
      if (params.origin && m.origin !== params.origin) return false;
      if (params.reviewed !== undefined && m.reviewed !== params.reviewed) return false;
      if (params.agent && m.source_agent !== params.agent) return false;
      if (wsFilter && !normalizeSlash(m.workspace).includes(wsFilter)) return false;
      if (q) {
        const hay = `${m.title} ${m.content} ${m.tags.join(' ')}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    filtered.sort((a, b) => b.updated_at.localeCompare(a.updated_at));

    return {
      total: filtered.length,
      counts_by_category: countsByCategory,
      counts_by_status: countsByStatus,
      native_imports: clone(this.nativeImports),
      memories: clone(filtered),
    };
  }

  createMemory(payload: CreateMemoryPayload): MemoryCard {
    const now = new Date().toISOString();
    const id = `mem_${Date.now().toString(36)}`;
    const card: MemoryCard = {
      id,
      title: payload.title,
      category: payload.category,
      content: payload.content,
      file_path: `${payload.category}s/${slugify(payload.title)}.md`,
      tags: payload.tags ?? [],
      workspace: payload.workspace ?? null,
      distill_status: 'heuristic',
      origin: 'manual',
      reviewed: payload.reviewed ?? true,
      source_session_id: payload.source_session_id ?? null,
      source_agent: payload.source_agent ?? null,
      source_turn_index: null,
      native_source_path: null,
      confidence: 1.0,
      created_at: now,
      updated_at: now,
    };
    this.memories.unshift(card);
    return clone(card);
  }

  updateMemory(id: string, payload: UpdateMemoryPayload): MemoryCard {
    const idx = this.memories.findIndex((m) => m.id === id);
    if (idx === -1) throw new Error(`Memory '${id}' not found`);
    const existing = this.memories[idx];
    const updated: MemoryCard = {
      ...existing,
      ...payload,
      updated_at: new Date().toISOString(),
    };
    this.memories[idx] = updated;
    return clone(updated);
  }

  deleteMemory(id: string): { deleted: boolean; id: string } {
    const before = this.memories.length;
    this.memories = this.memories.filter((m) => m.id !== id);
    return { deleted: this.memories.length < before, id };
  }

  syncMemories(payload: SyncMemoriesPayload): SyncMemoriesResponse {
    const now = new Date().toISOString();
    let importedNative = 0;

    if (payload.import_native !== false) {
      const geminiEntry = this.nativeImports.find((n) => n.agent === 'gemini_cli');
      if (geminiEntry && geminiEntry.status === 'pending') {
        geminiEntry.status = 'synced';
        geminiEntry.imported_count = 1;
        geminiEntry.last_imported_at = now;
        importedNative = 1;

        this.memories.unshift({
          id: `mem_gemini_native_${Date.now().toString(36)}`,
          title: 'Global Gemini CLI workspace rule (imported from GEMINI.md)',
          category: 'preference',
          content:
            '## Imported from `~/.gemini/GEMINI.md`\nKeep concise diffs and preserve existing docstrings across refactors.',
          file_path: 'preferences/gemini-cli-global.md',
          tags: ['gemini_cli', 'style'],
          workspace: 'E:/AAAAA project test/omnimex',
          distill_status: 'heuristic',
          origin: 'native_import',
          reviewed: false,
          source_session_id: null,
          source_agent: 'gemini_cli',
          source_turn_index: null,
          native_source_path: '~/.gemini/GEMINI.md',
          confidence: 0.9,
          created_at: now,
          updated_at: now,
        });
      } else {
        for (const item of this.nativeImports) {
          item.last_imported_at = now;
        }
      }
    }

    return {
      synced_files: this.memories.length,
      added: importedNative,
      updated: 0,
      deleted: 0,
      imported_native: importedNative,
      native_imports: clone(this.nativeImports),
    };
  }

  generateHandoff(req: HandoffRequest): HandoffBrief {
    const session = this.sessions.find((s) => s.id === req.session_id);
    if (!session) throw new Error(`Session '${req.session_id}' not found`);

    const sourceLabel = AGENT_META[session.agent]?.label ?? session.agent;
    const targetLabel = AGENT_META[req.target_agent]?.label ?? req.target_agent;

    const modifiedFiles = new Set<string>();
    for (const turn of session.turns ?? []) {
      for (const tc of turn.tool_calls) {
        const args = typeof tc.arguments === 'string' ? {} : tc.arguments;
        const candidate = args.Path ?? args.file_path ?? args.path;
        if (typeof candidate === 'string') modifiedFiles.add(candidate);
      }
    }
    if (modifiedFiles.size === 0) {
      modifiedFiles.add('src/omnimex/core/models.py');
    }

    const relevantMemories =
      req.include_memories === false
        ? []
        : this.memories
            .filter(
              (m) =>
                !session.workspace ||
                normalizeSlash(m.workspace) === normalizeSlash(session.workspace) ||
                m.source_session_id === session.id
            )
            .slice(0, 4)
            .map((m) => ({ id: m.id, title: m.title, category: m.category }));

    const keyDecisions = [
      session.summary ?? `Continue work from ${sourceLabel} session "${session.title}".`,
      ...relevantMemories
        .filter((m) => m.category === 'decision')
        .map((m) => m.title),
    ];

    const pitfalls = relevantMemories
      .filter((m) => m.category === 'pitfall')
      .map((m) => m.title);
    if (pitfalls.length === 0) {
      pitfalls.push('Scrub credentials before indexing or pasting session snippets across agents.');
    }

    const nextSteps = [
      `Review workspace state in ${session.workspace ?? 'repository root'} before editing.`,
      req.custom_instructions?.trim()
        ? `Follow operator instruction: ${req.custom_instructions.trim()}`
        : `Verify latest tests and continue remaining implementation tasks in ${targetLabel}.`,
    ];

    const filesList = Array.from(modifiedFiles);
    const markdownLines = [
      `# Cross-Agent Handoff Brief (${sourceLabel} -> ${targetLabel})`,
      '',
      `- **Session:** \`${session.id}\` — ${session.title}`,
      `- **Workspace:** \`${session.workspace ?? 'N/A'}\``,
      `- **Generated:** ${new Date().toISOString()}`,
      '',
      '## Goal',
      session.turns?.[0]?.content ?? session.title,
      '',
      '## Current State',
      session.summary ?? 'Active implementation session.',
      '',
      '## Key Decisions',
      ...keyDecisions.map((d) => `- ${d}`),
      '',
      '## Touched Files',
      ...filesList.map((f) => `- \`${f}\``),
      '',
      '## Pitfalls & Constraints',
      ...pitfalls.map((p) => `- ${p}`),
      '',
      '## Next Steps',
      ...nextSteps.map((s) => `- [ ] ${s}`),
    ];

    if (req.include_tool_summary !== false) {
      const toolNames = Array.from(
        new Set(
          (session.turns ?? []).flatMap((t) => t.tool_calls.map((tc) => `${tc.name} (${tc.status})`))
        )
      );
      if (toolNames.length > 0) {
        markdownLines.push('', '## Recent Tool Activity', ...toolNames.map((t) => `- ${t}`));
      }
    }

    if (relevantMemories.length > 0) {
      markdownLines.push(
        '',
        '## Linked Central Memories',
        ...relevantMemories.map((m) => `- [${m.category}] ${m.title} (\`${m.id}\`)`)
      );
    }

    const markdown = markdownLines.join('\n');
    return {
      session_id: session.id,
      source_agent: session.agent,
      target_agent: req.target_agent,
      workspace: session.workspace ?? null,
      title: `Handoff: ${session.title}`,
      markdown,
      sections: {
        goal: session.turns?.[0]?.content ?? session.title,
        current_state: session.summary ?? 'Active session ready for continuation.',
        key_decisions: keyDecisions,
        modified_files: filesList,
        pitfalls_and_constraints: pitfalls,
        next_steps: nextSteps,
        relevant_memories: relevantMemories,
      },
      token_estimate: Math.max(80, Math.round(markdown.length / 4)),
      generated_at: new Date().toISOString(),
      redacted: true,
    };
  }

  listSkills(): SkillListResponse {
    return {
      total: this.skills.length,
      skills: clone(this.skills),
    };
  }

  generateSkill(req: SkillGenerateRequest): SkillDraft {
    const sessIds = req.session_ids ?? [];
    const memIds = req.memory_ids ?? [];
    const pickedSessions = this.sessions.filter((s) => sessIds.includes(s.id));
    const pickedMemories = this.memories.filter((m) => memIds.includes(m.id));
    const baseTitle =
      req.name?.trim() ||
      pickedSessions[0]?.title ||
      pickedMemories[0]?.title ||
      'Omnimex Packaged Skill';
    const slug = slugify(req.name?.trim() || baseTitle);
    const desc =
      req.description?.trim() ||
      `Reusable engineering workflow and guardrails for ${baseTitle}.`;
    const steps = pickedSessions
      .flatMap((s) => (s.turns ?? []).slice(-3).map((t) => t.content))
      .filter(Boolean)
      .slice(0, 4);
    const pitfalls = pickedMemories
      .filter((m) => m.category === 'pitfall')
      .map((m) => `- **${m.title}**: ${m.content}`);
    const decisions = pickedMemories
      .filter((m) => m.category !== 'pitfall')
      .map((m) => `- **${m.title}**: ${m.content}`);
    const mdLines = [
      '---',
      `name: ${slug}`,
      `description: "${desc.replace(/"/g, '\\"')}"`,
      '---',
      '',
      `# ${baseTitle}`,
      '',
      '## When to Use',
      `- ${desc}`,
      '',
      '## Step-by-Step Workflow',
      ...(steps.length > 0
        ? steps.map((s, idx) => `${idx + 1}. ${s.slice(0, 180)}`)
        : ['1. Inspect relevant workspace files and apply verified constraints before editing.']),
      '',
      ...(decisions.length > 0 ? ['## Architectural Decisions & Preferences', ...decisions, ''] : []),
      ...(pitfalls.length > 0 ? ['## Pitfalls & Guardrails', ...pitfalls, ''] : []),
    ];
    const skillMd = mdLines.join('\n').trim() + '\n';
    return {
      slug,
      title: baseTitle,
      description: desc,
      skill_md: skillMd,
      source_session_ids: sessIds,
      source_memory_ids: memIds,
      tags: Array.from(new Set(pickedMemories.flatMap((m) => m.tags))).slice(0, 8),
      workspace: pickedSessions[0]?.workspace ?? pickedMemories[0]?.workspace ?? null,
      provider: 'heuristic',
      redacted: true,
      token_estimate: Math.max(40, Math.round(skillMd.length / 4)),
      generated_at: new Date().toISOString(),
    };
  }

  saveSkill(req: SkillSaveRequest): SavedSkillSummary {
    const slug = slugify(req.slug);
    const summary: SavedSkillSummary = {
      slug,
      name: slug,
      description: 'Saved packaged SKILL.md (Demo Store)',
      file_path: `data/skills/${slug}/SKILL.md`,
      updated_at: new Date().toISOString(),
      token_estimate: Math.max(20, Math.round(req.skill_md.length / 4)),
      skill_md: req.skill_md,
    };
    this.skills = [summary, ...this.skills.filter((s) => s.slug !== slug)];
    return clone(summary);
  }

  deleteSkill(slug: string): { deleted: boolean; slug: string } {
    this.skills = this.skills.filter((s) => s.slug !== slug);
    return { deleted: true, slug };
  }

  installSkill(req: SkillInstallRequest): SkillInstallResult {
    const slug = slugify(req.slug);
    if (req.save_to_archive !== false) {
      this.saveSkill({ slug, skill_md: req.skill_md });
    }
    return {
      slug,
      installed: req.targets.map((target) => ({
        target,
        path: `~/${target}/skills/${slug}/SKILL.md`,
        status: 'created',
        backup_path: null,
      })),
      errors: [],
    };
  }

  listAdapters(): AdaptersResponse {
    const discovered = this.adapters.reduce((acc, a) => acc + a.discovered_files, 0);
    return {
      total_discovered_sources: discovered,
      watcher_running: true,
      adapters: clone(this.adapters),
    };
  }

  triggerScan(req: ScanRequest): ScanResponse {
    const now = new Date().toISOString();
    const targetIds = req.agents && req.agents.length > 0 ? new Set(req.agents) : null;
    let scannedFiles = 0;

    for (const adapter of this.adapters) {
      if (targetIds && !targetIds.has(adapter.id)) continue;
      adapter.last_scan_at = now;
      scannedFiles += adapter.discovered_files;
    }

    return {
      job_id: `scan_${Date.now().toString(36)}`,
      status: 'completed',
      scanned_files: scannedFiles,
      new_sessions: 0,
      new_turns: 0,
      updated_sessions: req.full_rescan ? this.sessions.length : 1,
      errors: [],
      duration_ms: 142.5,
    };
  }

  getStats(): StatsResponse {
    const totalSessions = this.adapters.reduce((sum, a) => sum + a.session_count, 0);
    const totalTurns = this.adapters.reduce((sum, a) => sum + a.turn_count, 0);
    const totalNativeMemories = this.adapters.reduce((sum, a) => sum + a.native_memory_count, 0);

    const memoriesByCategory: Record<MemoryCategory, number> = {
      preference: 0,
      decision: 0,
      pitfall: 0,
      fact: 0,
    };
    const memoriesByStatus: Record<'heuristic' | 'llm_distilled' | 'pending' | 'manual', number> = {
      heuristic: 0,
      llm_distilled: 0,
      pending: 0,
      manual: 0,
    };
    for (const m of this.memories) {
      memoriesByCategory[m.category] += 1;
      if (m.origin === 'manual') memoriesByStatus.manual += 1;
      else memoriesByStatus[m.distill_status] += 1;
    }

    return {
      total_sessions: totalSessions,
      total_turns: totalTurns,
      total_memories: this.memories.length,
      total_native_memories: totalNativeMemories,
      db_size_bytes: 2_845_120,
      index_embeddings_count: totalTurns,
      by_agent: this.adapters
        .filter((a) => a.session_count > 0)
        .map((a) => ({
          agent: a.id,
          name: a.name,
          sessions: a.session_count,
          turns: a.turn_count,
          native_memories: a.native_memory_count,
          last_activity_at: a.last_scan_at,
        }))
        .sort((a, b) => b.turns - a.turns),
      memories_by_category: memoriesByCategory,
      memories_by_status: memoriesByStatus,
      recent_activity: [
        { date: '2026-11-13', sessions: 5, turns: 92 },
        { date: '2026-11-14', sessions: 7, turns: 148 },
        { date: '2026-11-15', sessions: 10, turns: 214 },
        { date: '2026-11-16', sessions: 16, turns: 342 },
        { date: '2026-11-17', sessions: 14, turns: 282 },
      ],
      distiller: clone(this.distiller),
    };
  }

  getDistillerSettings(): DistillerSettings {
    return clone(this.distiller);
  }

  updateDistillerSettings(payload: UpdateDistillerSettingsPayload): DistillerSettings {
    const provider = payload.provider ?? this.distiller.provider;
    this.distiller = {
      ...this.distiller,
      provider,
      model: payload.model !== undefined ? payload.model : this.distiller.model,
      base_url: payload.base_url !== undefined ? payload.base_url : this.distiller.base_url,
      api_key_configured:
        payload.api_key !== undefined
          ? Boolean(payload.api_key && payload.api_key.trim().length > 0)
          : this.distiller.api_key_configured,
      auto_distill_on_scan:
        payload.auto_distill_on_scan ?? this.distiller.auto_distill_on_scan,
      zero_cost_mode: provider === 'heuristic' || provider === 'ollama',
    };
    return clone(this.distiller);
  }

  runDistillBackfill(req: DistillBackfillRequest): DistillBackfillResponse {
    const pending = this.distiller.pending_sessions_count;
    const processed = req.force ? Math.max(pending, 2) : pending;
    const created = processed > 0 ? processed : 0;

    if (created > 0) {
      const now = new Date().toISOString();
      const targetStatus =
        this.distiller.provider === 'heuristic' ? 'heuristic' : 'llm_distilled';
      this.memories.unshift({
        id: `mem_backfill_${Date.now().toString(36)}`,
        title: `Backfilled insight (${this.distiller.provider}): Incremental cursor ingestion`,
        category: 'fact',
        content:
          '## Distilled Insight\nIncremental ingestion tracks byte/line offsets in `ingest_cursors` so repeated scans only parse newly appended transcript turns.',
        file_path: 'facts/incremental-cursor-ingestion.md',
        tags: ['ingestion', 'cursors', this.distiller.provider],
        workspace: 'E:/AAAAA project test/omnimex',
        distill_status: targetStatus,
        origin: 'distilled',
        reviewed: false,
        source_session_id: 'codex:rollout-2026-11-16',
        source_agent: 'codex',
        source_turn_index: 1,
        native_source_path: null,
        confidence: 0.85,
        created_at: now,
        updated_at: now,
      });
      this.distiller.pending_sessions_count = 0;
      this.distiller.distilled_memories_count += 1;
    }

    return {
      job_id: `distill_${Date.now().toString(36)}`,
      status: 'completed',
      provider: this.distiller.provider,
      processed_sessions: processed,
      created_memories: created,
      skipped_sessions: req.limit && req.limit < processed ? processed - req.limit : 0,
    };
  }
}

export class OmnimexClient {
  private mock = new MockStore();
  private state: ConnectionState = {
    mode: 'auto',
    usingMock: false,
    backendReachable: false,
    lastError: null,
  };
  private listeners = new Set<(state: ConnectionState) => void>();
  private hasProbed = false;
  private probePromise: Promise<HealthResponse> | null = null;

  getConnectionState(): ConnectionState {
    return { ...this.state };
  }

  subscribeConnection(fn: (state: ConnectionState) => void): () => void {
    this.listeners.add(fn);
    fn(this.getConnectionState());
    return () => {
      this.listeners.delete(fn);
    };
  }

  setMode(mode: ConnectionMode): void {
    this.state = {
      ...this.state,
      mode,
      usingMock: mode === 'demo',
      lastError: mode === 'demo' ? null : this.state.lastError,
    };
    if (mode !== 'demo') {
      this.hasProbed = false;
    }
    this.emitState();
  }

  private emitState(): void {
    const snap = this.getConnectionState();
    for (const fn of this.listeners) {
      fn(snap);
    }
  }

  async probeHealth(): Promise<HealthResponse> {
    if (this.state.mode === 'demo') {
      this.hasProbed = true;
      return {
        status: 'ok',
        version: '0.1.0-demo',
        db_ready: true,
        distiller_provider: this.mock.distiller.provider,
      };
    }

    if (this.probePromise) {
      return this.probePromise;
    }

    const runProbe = async (): Promise<HealthResponse> => {
      const controller = new AbortController();
      const timer = window.setTimeout(() => controller.abort(), 2200);
      try {
        const res = await fetch(`${API_BASE}/api/health`, { signal: controller.signal });
        const contentType = res.headers.get('content-type') ?? '';
        if (!res.ok || !contentType.includes('application/json')) {
          throw new Error(`HTTP ${res.status}`);
        }
        const data = (await res.json()) as HealthResponse;
        this.state = {
          ...this.state,
          usingMock: false,
          backendReachable: true,
          lastError: null,
        };
        this.emitState();
        return data;
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Backend unreachable';
        if (this.state.mode === 'auto') {
          this.state = {
            ...this.state,
            usingMock: true,
            backendReachable: false,
            lastError: msg,
          };
          this.emitState();
          return {
            status: 'ok',
            version: '0.1.0-demo',
            db_ready: true,
            distiller_provider: this.mock.distiller.provider,
          };
        }
        this.state = {
          ...this.state,
          usingMock: false,
          backendReachable: false,
          lastError: msg,
        };
        this.emitState();
        throw err;
      } finally {
        this.hasProbed = true;
        this.probePromise = null;
        window.clearTimeout(timer);
      }
    };

    this.probePromise = runProbe();
    return this.probePromise;
  }

  private async request<T>(path: string, init: RequestInit | undefined, fallback: () => T): Promise<T> {
    if (this.state.mode === 'auto' && !this.hasProbed) {
      try {
        await this.probeHealth();
      } catch {
        // probeHealth already transitioned to demo fallback in auto mode
      }
    }

    if (this.state.mode === 'demo' || (this.state.mode === 'auto' && this.state.usingMock)) {
      return fallback();
    }

    try {
      const headers = new Headers(init?.headers);
      if (init?.body && !headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json');
      }
      const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
      const contentType = res.headers.get('content-type') ?? '';
      const isJson = contentType.includes('application/json');

      if (!res.ok) {
        let message = `HTTP ${res.status}`;
        let hasApiErrorEnvelope = false;
        if (isJson) {
          try {
            const errJson = (await res.json()) as { error?: { message?: string }; detail?: string };
            if (errJson.error?.message || errJson.detail) {
              message = errJson.error?.message ?? errJson.detail ?? message;
              hasApiErrorEnvelope = true;
            }
          } catch {
            // ignore malformed json body
          }
        }
        // In auto mode, proxy failures (502+), or non-JSON 500/404 from Vite dev proxy / preview server mean backend is offline
        if (this.state.mode === 'auto' && (res.status >= 502 || (!hasApiErrorEnvelope && (res.status === 500 || res.status === 404)))) {
          throw new Error(`Proxy unavailable (${res.status})`);
        }
        throw new Error(message);
      }

      if (!isJson) {
        if (this.state.mode === 'auto') {
          throw new Error('Proxy unavailable (non-JSON response)');
        }
        throw new Error('Expected JSON response from API');
      }

      if (!this.state.backendReachable || this.state.usingMock) {
        this.state = {
          ...this.state,
          backendReachable: true,
          usingMock: false,
          lastError: null,
        };
        this.emitState();
      }
      return (await res.json()) as T;
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Network error';
      const isNetworkOrProxyError =
        err instanceof TypeError ||
        msg.includes('Proxy unavailable') ||
        msg.includes('Failed to fetch') ||
        msg.includes('NetworkError');

      if (this.state.mode === 'auto' && isNetworkOrProxyError) {
        this.state = {
          ...this.state,
          usingMock: true,
          backendReachable: false,
          lastError: msg,
        };
        this.emitState();
        return fallback();
      }
      throw err;
    }
  }

  search(params: SearchParams): Promise<SearchResponse> {
    const qs = buildQuery({
      q: params.q,
      agent: params.agent,
      workspace: params.workspace,
      mode: params.mode ?? 'hybrid',
      date_from: params.date_from,
      date_to: params.date_to,
      limit: params.limit,
      offset: params.offset,
    });
    return this.request<SearchResponse>(`/api/search${qs}`, undefined, () => this.mock.search(params));
  }

  listSessions(params: SessionsParams = {}): Promise<SessionsResponse> {
    const qs = buildQuery({
      agent: params.agent,
      workspace: params.workspace,
      q: params.q,
      date_from: params.date_from,
      date_to: params.date_to,
      include_subagents: params.include_subagents,
      limit: params.limit,
      offset: params.offset,
    });
    return this.request<SessionsResponse>(`/api/sessions${qs}`, undefined, () =>
      this.mock.listSessions(params)
    );
  }

  getSession(
    id: string,
    params: { turn_offset?: number; turn_limit?: number; max_turn_chars?: number; qa_only?: boolean } = {}
  ): Promise<SessionDetailResponse> {
    const qs = buildQuery({
      turn_offset: params.turn_offset,
      turn_limit: params.turn_limit,
      max_turn_chars: params.max_turn_chars,
      qa_only: params.qa_only ? 'true' : undefined,
    });
    return this.request<SessionDetailResponse>(
      `/api/sessions/${encodeURIComponent(id)}${qs}`,
      undefined,
      () => this.mock.getSession(id, params)
    );
  }

  getSessionTurn(id: string, turnIndex: number): Promise<SessionTurnResponse> {
    return this.request<SessionTurnResponse>(
      `/api/sessions/${encodeURIComponent(id)}/turns/${turnIndex}`,
      undefined,
      () => this.mock.getSessionTurn(id, turnIndex)
    );
  }

  listMemories(params: MemoriesParams = {}): Promise<MemoriesResponse> {
    const qs = buildQuery({
      category: params.category,
      distill_status: params.distill_status,
      origin: params.origin,
      reviewed: params.reviewed,
      workspace: params.workspace,
      agent: params.agent,
      q: params.q,
    });
    return this.request<MemoriesResponse>(`/api/memories${qs}`, undefined, () =>
      this.mock.listMemories(params)
    );
  }

  createMemory(payload: CreateMemoryPayload): Promise<MemoryCard> {
    return this.request<MemoryCard>(
      '/api/memories',
      { method: 'POST', body: JSON.stringify(payload) },
      () => this.mock.createMemory(payload)
    );
  }

  updateMemory(id: string, payload: UpdateMemoryPayload): Promise<MemoryCard> {
    return this.request<MemoryCard>(
      `/api/memories/${encodeURIComponent(id)}`,
      { method: 'PATCH', body: JSON.stringify(payload) },
      () => this.mock.updateMemory(id, payload)
    );
  }

  deleteMemory(id: string): Promise<{ deleted: boolean; id: string }> {
    return this.request<{ deleted: boolean; id: string }>(
      `/api/memories/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
      () => this.mock.deleteMemory(id)
    );
  }

  syncMemories(payload: SyncMemoriesPayload = {}): Promise<SyncMemoriesResponse> {
    return this.request<SyncMemoriesResponse>(
      '/api/memories/sync',
      { method: 'POST', body: JSON.stringify(payload) },
      () => this.mock.syncMemories(payload)
    );
  }

  generateHandoff(payload: HandoffRequest): Promise<HandoffBrief> {
    return this.request<HandoffBrief>(
      '/api/handoff',
      { method: 'POST', body: JSON.stringify(payload) },
      () => this.mock.generateHandoff(payload)
    );
  }

  listSkills(): Promise<SkillListResponse> {
    return this.request<SkillListResponse>('/api/skills', undefined, () =>
      this.mock.listSkills()
    );
  }

  generateSkill(payload: SkillGenerateRequest): Promise<SkillDraft> {
    return this.request<SkillDraft>(
      '/api/skills/generate',
      { method: 'POST', body: JSON.stringify(payload) },
      () => this.mock.generateSkill(payload)
    );
  }

  saveSkill(payload: SkillSaveRequest): Promise<SavedSkillSummary> {
    return this.request<SavedSkillSummary>(
      '/api/skills/save',
      { method: 'POST', body: JSON.stringify(payload) },
      () => this.mock.saveSkill(payload)
    );
  }

  deleteSkill(slug: string): Promise<{ deleted: boolean; slug: string }> {
    return this.request<{ deleted: boolean; slug: string }>(
      `/api/skills/${encodeURIComponent(slug)}`,
      { method: 'DELETE' },
      () => this.mock.deleteSkill(slug)
    );
  }

  installSkill(payload: SkillInstallRequest): Promise<SkillInstallResult> {
    return this.request<SkillInstallResult>(
      '/api/skills/install',
      { method: 'POST', body: JSON.stringify(payload) },
      () => this.mock.installSkill(payload)
    );
  }

  async exportSkillZip(slug: string, skill_md: string): Promise<Blob> {
    if (this.state.mode === 'demo' || this.state.usingMock) {
      return new Blob([skill_md], { type: 'text/markdown;charset=utf-8' });
    }
    const res = await fetch(`${API_BASE}/api/skills/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug, skill_md }),
    });
    if (!res.ok) {
      throw new Error(`Export failed (${res.status})`);
    }
    return await res.blob();
  }

  listAdapters(): Promise<AdaptersResponse> {
    return this.request<AdaptersResponse>('/api/adapters', undefined, () =>
      this.mock.listAdapters()
    );
  }

  triggerScan(payload: ScanRequest = {}): Promise<ScanResponse> {
    return this.request<ScanResponse>(
      '/api/scan',
      { method: 'POST', body: JSON.stringify(payload) },
      () => this.mock.triggerScan(payload)
    );
  }

  getStats(): Promise<StatsResponse> {
    return this.request<StatsResponse>('/api/stats', undefined, () => this.mock.getStats());
  }

  getDistillerSettings(): Promise<DistillerSettings> {
    return this.request<DistillerSettings>('/api/settings/distiller', undefined, () =>
      this.mock.getDistillerSettings()
    );
  }

  updateDistillerSettings(
    payload: UpdateDistillerSettingsPayload
  ): Promise<DistillerSettings> {
    return this.request<DistillerSettings>(
      '/api/settings/distiller',
      { method: 'PATCH', body: JSON.stringify(payload) },
      () => this.mock.updateDistillerSettings(payload)
    );
  }

  runDistillBackfill(
    payload: DistillBackfillRequest = {}
  ): Promise<DistillBackfillResponse> {
    return this.request<DistillBackfillResponse>(
      '/api/distill/backfill',
      { method: 'POST', body: JSON.stringify(payload) },
      () => this.mock.runDistillBackfill(payload)
    );
  }

  subscribeEvents(onEvent: (event: OmnimexEvent) => void): () => void {
    if (this.state.usingMock || this.state.mode === 'demo' || typeof EventSource === 'undefined') {
      return () => {};
    }

    const es = new EventSource(`${API_BASE}/api/events`);
    const eventTypes: OmnimexEvent['type'][] = [
      'connected',
      'scan.started',
      'scan.progress',
      'scan.completed',
      'session.ingested',
      'memory.created',
      'memory.updated',
      'memory.synced',
      'distill.progress',
      'distill.completed',
      'embedding.started',
      'embedding.progress',
      'embedding.completed',
      'skill.saved',
      'skill.installed',
    ];

    const handlers: Array<[string, EventListener]> = eventTypes.map((evtType) => {
      const listener: EventListener = (evt) => {
        const messageEvent = evt as MessageEvent<string>;
        try {
          const parsed = JSON.parse(messageEvent.data) as Record<string, unknown>;
          onEvent({
            type: evtType,
            data: parsed,
            receivedAt: new Date().toISOString(),
          });
        } catch {
          // ignore malformed SSE payload
        }
      };
      es.addEventListener(evtType, listener);
      return [evtType, listener];
    });

    es.onerror = () => {
      // Close silently if backend stream drops while offline
      if (this.state.usingMock) {
        es.close();
      }
    };

    return () => {
      for (const [evtType, listener] of handlers) {
        es.removeEventListener(evtType, listener);
      }
      es.close();
    };
  }
}

export const apiClient = new OmnimexClient();
