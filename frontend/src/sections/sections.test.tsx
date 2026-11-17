import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { apiClient } from '../api/client';
import type { NormalizedSession } from '../api/types';
import { AdaptersStats } from './AdaptersStats';
import { HandoffWorkbench } from './HandoffWorkbench';
import { MemoryHub } from './MemoryHub';
import { SearchExplorer } from './SearchExplorer';

describe('Omnimex UI Sections', () => {
  beforeEach(() => {
    apiClient.setMode('demo');
  });

  it('renders SearchExplorer, expands thinking/tool cards with empty tool IDs, and searches hits', async () => {
    const sessionWithEmptyToolIds: NormalizedSession = {
      id: 'antigravity:test-empty-ids',
      agent: 'antigravity',
      external_id: 'test-empty-ids',
      title: 'Session with multiple unkeyed tool calls',
      workspace: 'E:/AAAAA project test/omnimex',
      source_path: '~/.gemini/antigravity/brain/test/transcript.jsonl',
      turn_count: 1,
      tags: ['test'],
      turns: [
        {
          id: 'turn-empty-tc',
          session_id: 'antigravity:test-empty-ids',
          turn_index: 0,
          role: 'assistant',
          content: 'Running two checks.\n\n```bash\npytest -q\n```',
          thinking: 'Verify unique React keys for empty ToolCallRecord ids.',
          tool_calls: [
            { id: '', name: 'tool_alpha', arguments: { step: 1 }, output: 'alpha ok', status: 'ok' },
            { id: '', name: 'tool_beta', arguments: { step: 2 }, output: 'beta ok', status: 'ok' },
          ],
          redacted: false,
        },
      ],
    };

    const getSessionSpy = vi.spyOn(apiClient, 'getSession').mockResolvedValueOnce({
      session: sessionWithEmptyToolIds,
      linked_memories: [],
    });

    const onSelectSession = vi.fn();
    const onCreateHandoff = vi.fn();

    render(
      <SearchExplorer
        selectedSessionId="antigravity:test-empty-ids"
        highlightTurnIndex={null}
        onSelectSession={onSelectSession}
        onCreateHandoff={onCreateHandoff}
        refreshSignal={0}
      />
    );

    expect(await screen.findByText('Session with multiple unkeyed tool calls')).toBeInTheDocument();
    expect(screen.getByText(/Running two checks\./)).toBeInTheDocument();
    // Thinking & tool details are hidden by default until revealed
    expect(screen.queryByText('Reasoning / Thinking')).not.toBeInTheDocument();
    expect(
      screen.queryByText('Verify unique React keys for empty ToolCallRecord ids.')
    ).not.toBeInTheDocument();
    expect(screen.queryByText('tool_alpha')).not.toBeInTheDocument();

    const foldToggle = screen.getByRole('button', { name: /Thinking & 2 tool calls/i });
    expect(foldToggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(foldToggle);
    expect(foldToggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Reasoning / Thinking')).toBeInTheDocument();
    expect(
      screen.getByText('Verify unique React keys for empty ToolCallRecord ids.')
    ).toBeInTheDocument();
    expect(screen.getByText('tool_alpha')).toBeInTheDocument();
    expect(screen.getByText('tool_beta')).toBeInTheDocument();

    fireEvent.click(screen.getByText('tool_alpha'));
    expect(screen.getByText('alpha ok')).toBeInTheDocument();

    fireEvent.click(foldToggle);
    expect(foldToggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('tool_alpha')).not.toBeInTheDocument();

    const searchInput = screen.getByPlaceholderText(/Search transcripts across/i);
    fireEvent.change(searchInput, { target: { value: 'sqlite' } });
    fireEvent.click(screen.getByRole('button', { name: /^Search$/i }));

    await waitFor(() => {
      expect(screen.getByText(/Search Hits \([1-9]\d*\)/)).toBeInTheDocument();
    });

    getSessionSpy.mockRestore();
  });

  it('renders MemoryHub categories, badges, draft approval, native sync, and provenance link', async () => {
    const onOpenSession = vi.fn();

    render(<MemoryHub onOpenSession={onOpenSession} refreshSignal={0} />);

    expect(await screen.findByText(/Use RRF \(k=60\)/i)).toBeInTheDocument();
    expect(screen.getByText(/Native Memory Sync:/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Preference \(\d+\)$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Decision \(\d+\)$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Pitfall \(\d+\)$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Fact \(\d+\)$/i })).toBeInTheDocument();

    // Sync & Import Native
    fireEvent.click(screen.getByRole('button', { name: /Sync & Import Native/i }));
    await waitFor(() => {
      expect(screen.getByText(/Synced \d+ Markdown files/i)).toBeInTheDocument();
    });

    // Select an unreviewed heuristic draft and approve it
    fireEvent.click(
      screen.getByText(/Temp-copy Cursor\/Windsurf SQLite DB/i)
    );
    const approveBtn = await screen.findByRole('button', { name: /Approve Draft/i });
    fireEvent.click(approveBtn);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /^Reviewed$/i })).toBeInTheDocument();
    });

    // Click provenance link
    const sourceBtn = screen.getByRole('button', { name: /Source: claude_code:omnimex-adapters/i });
    fireEvent.click(sourceBtn);
    expect(onOpenSession).toHaveBeenCalledWith('claude_code:omnimex-adapters', 1);
  });

  it('renders HandoffWorkbench, switches target agent, and displays generated brief preview', async () => {
    render(<HandoffWorkbench initialSessionId={null} refreshSignal={0} />);

    expect(await screen.findByText(/1\. Source Session/i)).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /Copy Brief/i })).toBeInTheDocument();
    expect(screen.getByText(/Secrets Redacted/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Codex CLI/i }));
    await waitFor(() => {
      expect(screen.getByText(/Cross-Agent Handoff Brief \(.*-> Codex CLI\)/i)).toBeInTheDocument();
    });
  });

  it('renders AdaptersStats health cards, zero-cost distiller mode, and triggers scan/backfill', async () => {
    const onDataChanged = vi.fn();
    render(<AdaptersStats refreshSignal={0} onDataChanged={onDataChanged} />);

    expect(await screen.findByText(/Adapter Discovery Health/i)).toBeInTheDocument();
    expect(screen.getByText(/Zero-Cost Mode/i)).toBeInTheDocument();
    expect(screen.getByText(/Session & Turn Counts per Agent/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Scan Now/i }));
    await waitFor(() => {
      expect(screen.getByText(/Scan complete in/i)).toBeInTheDocument();
    });
    expect(onDataChanged).toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /Run Backfill/i }));
    await waitFor(() => {
      expect(screen.getByText(/Backfill \(heuristic\):/i)).toBeInTheDocument();
    });
  });

  it('renders App with top navigation tabs and switches sections cleanly', async () => {
    render(<App />);

    expect(await screen.findByText('Demo Fallback')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Memory Hub/i }));
    expect(await screen.findByText(/Central Markdown Memories/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Handoff Workbench/i }));
    expect(await screen.findByText(/2\. Target Agent/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Adapters & Stats/i }));
    expect(await screen.findByText(/Adapter Discovery Health/i)).toBeInTheDocument();
  });

  it('supports paginated earlier-turns loading and full-turn expansion for oversized turns', async () => {
    const giantSession: NormalizedSession = {
      id: 'codex:giant-test',
      agent: 'codex',
      external_id: 'giant-test',
      title: 'Giant paginated transcript',
      workspace: 'E:/AAAAA project test/omnimex',
      source_path: '~/.codex/sessions/giant.jsonl',
      turn_count: 5,
      tags: [],
      turns: [
        {
          id: 'giant#t3',
          session_id: 'codex:giant-test',
          turn_index: 3,
          role: 'user',
          content: 'Short turn #3',
          tool_calls: [],
          redacted: false,
        },
        {
          id: 'giant#t4',
          session_id: 'codex:giant-test',
          turn_index: 4,
          role: 'assistant',
          content: 'Truncated preview... [truncated 9,000 chars]',
          tool_calls: [],
          redacted: false,
        },
      ],
    };

    const getSessionSpy = vi
      .spyOn(apiClient, 'getSession')
      .mockResolvedValueOnce({
        session: giantSession,
        linked_memories: [],
        total_turns: 5,
        turn_offset: 3,
        turn_limit: 40,
        has_earlier_turns: true,
        has_later_turns: false,
        truncated_turn_indices: [4],
      })
      .mockResolvedValueOnce({
        session: {
          ...giantSession,
          turns: [
            {
              id: 'giant#t0',
              session_id: 'codex:giant-test',
              turn_index: 0,
              role: 'user',
              content: 'Earlier turn #0 loaded on demand',
              tool_calls: [],
              redacted: false,
            },
          ],
        },
        linked_memories: [],
        total_turns: 5,
        turn_offset: 0,
        turn_limit: 3,
        has_earlier_turns: false,
        has_later_turns: true,
        truncated_turn_indices: [],
      });

    const getTurnSpy = vi.spyOn(apiClient, 'getSessionTurn').mockResolvedValueOnce({
      turn: {
        id: 'giant#t4',
        session_id: 'codex:giant-test',
        turn_index: 4,
        role: 'assistant',
        content: 'X'.repeat(8500),
        tool_calls: [],
        redacted: false,
      },
      truncated: false,
    });

    render(
      <SearchExplorer
        selectedSessionId="codex:giant-test"
        highlightTurnIndex={null}
        onSelectSession={vi.fn()}
        onCreateHandoff={vi.fn()}
        refreshSignal={0}
      />
    );

    const loadEarlierBtn = await screen.findByRole('button', {
      name: /Load earlier turns \(3 earlier of 5\)/i,
    });
    fireEvent.click(loadEarlierBtn);
    expect(await screen.findByText('Earlier turn #0 loaded on demand')).toBeInTheDocument();

    const loadFullBtn = await screen.findByRole('button', { name: /Load full turn/i });
    fireEvent.click(loadFullBtn);
    expect(await screen.findByText(/rendered in fast plain mode/i)).toBeInTheDocument();

    getSessionSpy.mockRestore();
    getTurnSpy.mockRestore();
  });

  it('hides intermediate thinking/tool steps by default via qa_only and expands them via pill or header toggle', async () => {
    const rawTurns = [
      {
        id: 'proc-0',
        session_id: 'claude_code:process-hide',
        turn_index: 0,
        role: 'user' as const,
        content: 'How does Omnimex store sessions?',
        tool_calls: [],
        redacted: false,
      },
      {
        id: 'proc-1',
        session_id: 'claude_code:process-hide',
        turn_index: 1,
        role: 'assistant' as const,
        content: '',
        thinking: 'Checking storage db schema first.',
        tool_calls: [
          { id: 't1', name: 'Read', arguments: { path: 'db.py' }, output: '', status: 'ok' as const },
        ],
        redacted: false,
      },
      {
        id: 'proc-2',
        session_id: 'claude_code:process-hide',
        turn_index: 2,
        role: 'tool' as const,
        content: 'CREATE TABLE sessions ...',
        tool_calls: [],
        redacted: false,
      },
      {
        id: 'proc-3',
        session_id: 'claude_code:process-hide',
        turn_index: 3,
        role: 'assistant' as const,
        content: 'Sessions live in SQLite with WAL enabled.',
        thinking: 'Summarize concisely for the user.',
        tool_calls: [],
        redacted: false,
      },
    ];

    const baseSession: NormalizedSession = {
      id: 'claude_code:process-hide',
      agent: 'claude_code',
      external_id: 'process-hide',
      title: 'Clean QA default test',
      workspace: 'E:/AAAAA project test/omnimex',
      source_path: '~/.claude/projects/demo/process.jsonl',
      turn_count: 4,
      tags: [],
      turns: rawTurns,
    };

    const getSessionSpy = vi
      .spyOn(apiClient, 'getSession')
      .mockImplementation(async (_id, params) => {
        const qaOnly = Boolean(params?.qa_only);
        const turns = qaOnly ? [rawTurns[0], rawTurns[3]] : rawTurns;
        return {
          session: { ...baseSession, turns },
          linked_memories: [],
          total_turns: turns.length,
          turn_offset: 0,
          turn_limit: 40,
          has_earlier_turns: false,
          has_later_turns: false,
          truncated_turn_indices: [],
        };
      });

    render(
      <SearchExplorer
        selectedSessionId="claude_code:process-hide"
        highlightTurnIndex={null}
        onSelectSession={vi.fn()}
        onCreateHandoff={vi.fn()}
        refreshSignal={0}
      />
    );

    expect(await screen.findByText('How does Omnimex store sessions?')).toBeInTheDocument();
    expect(screen.getByText('Sessions live in SQLite with WAL enabled.')).toBeInTheDocument();
    const foldBtn = screen.getByRole('button', {
      name: /2 process steps \(thinking & tools\)/i,
    });
    expect(foldBtn).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('CREATE TABLE sessions ...')).not.toBeInTheDocument();
    expect(screen.queryByText('Summarize concisely for the user.')).not.toBeInTheDocument();

    // One click on the integrated fold expands BOTH intermediate tool steps AND this answer's thinking
    fireEvent.click(foldBtn);
    expect(await screen.findByText('CREATE TABLE sessions ...')).toBeInTheDocument();
    expect(screen.getByText('Summarize concisely for the user.')).toBeInTheDocument();

    // Toggle full view via session header button
    fireEvent.click(screen.getByRole('button', { name: /Show thinking & process/i }));
    expect(
      await screen.findByRole('button', { name: /Thinking & process: On/i })
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(getSessionSpy).toHaveBeenLastCalledWith(
        'claude_code:process-hide',
        expect.objectContaining({ qa_only: false })
      );
    });

    getSessionSpy.mockRestore();
  });

  it('hides subagent/system-only sessions by default and toggles them with Show subagents', async () => {
    const humanSess: NormalizedSession = {
      id: 'antigravity:human-main',
      agent: 'antigravity',
      external_id: 'human-main',
      title: 'Main user conversation',
      workspace: 'E:/AAAAA project test/omnimex',
      source_path: '~/.gemini/antigravity/conversations/human.pb',
      turn_count: 10,
      tags: [],
    };
    const subagentSess: NormalizedSession = {
      id: 'antigravity:sub-001',
      agent: 'antigravity',
      external_id: 'sub-001',
      title: 'The following is a <SYSTEM_MESSAGE> not actually sent by the user.',
      workspace: undefined,
      source_path: '~/.gemini/antigravity/conversations/sub.pb',
      turn_count: 18,
      tags: [],
    };

    const listSpy = vi
      .spyOn(apiClient, 'listSessions')
      .mockImplementation(async (params) => {
        const include = params?.include_subagents ?? true;
        const sessions = include ? [humanSess, subagentSess] : [humanSess];
        return { total: sessions.length, limit: 50, offset: 0, sessions };
      });
    const getSessionSpy = vi.spyOn(apiClient, 'getSession').mockResolvedValue({
      session: { ...humanSess, turns: [] },
      linked_memories: [],
      total_turns: 0,
      turn_offset: 0,
      turn_limit: 40,
      has_earlier_turns: false,
      has_later_turns: false,
      truncated_turn_indices: [],
    });

    render(
      <SearchExplorer
        selectedSessionId="antigravity:human-main"
        highlightTurnIndex={null}
        onSelectSession={vi.fn()}
        onCreateHandoff={vi.fn()}
        refreshSignal={0}
      />
    );

    expect(await screen.findByText('Sessions (1)')).toBeInTheDocument();
    expect(screen.getAllByText('Main user conversation').length).toBeGreaterThanOrEqual(1);
    expect(
      screen.queryByText(/The following is a <SYSTEM_MESSAGE>/i)
    ).not.toBeInTheDocument();
    expect(listSpy).toHaveBeenCalledWith(
      expect.objectContaining({ include_subagents: false })
    );

    const toggleBtn = screen.getByRole('button', { name: /Show subagents/i });
    expect(toggleBtn).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(toggleBtn);

    expect(await screen.findByText('Sessions (2)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Subagents: On/i })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(screen.getByText(/The following is a <SYSTEM_MESSAGE>/i)).toBeInTheDocument();

    listSpy.mockRestore();
    getSessionSpy.mockRestore();
  });
});
