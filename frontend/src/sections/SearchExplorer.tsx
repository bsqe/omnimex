import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowRightLeft,
  Brain,
  Calendar,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  FolderGit2,
  MessageSquare,
  PackagePlus,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
  Sparkles,
  Terminal,
  Wrench,
} from 'lucide-react';
import { apiClient } from '../api/client';
import { AGENT_META } from '../api/mockData';
import type {
  MemoryCard,
  NormalizedSession,
  NormalizedTurn,
  SearchHit,
  SearchMode,
  ToolCallRecord,
} from '../api/types';
import { AgentBadge } from '../components/AgentBadge';
import { CopyButton } from '../components/CopyButton';
import { MarkdownContent } from '../components/MarkdownContent';
import { formatRelativeTime, formatTimestamp, shortenWorkspace } from '../utils/format';
import { useLocale } from '../utils/i18n';

const TURN_PAGE_SIZE = 40;
const GAP_FETCH_LIMIT = 60;
const MAX_TURN_CHARS = 12000;

function isProcessTurn(turn: NormalizedTurn): boolean {
  if (turn.role === 'tool' || turn.role === 'system') return true;
  if (turn.role !== 'assistant') return false;
  const trimmed = (turn.content ?? '').trim();
  return trimmed === '' || /^\[Tool:[^\]]*\]$/i.test(trimmed);
}

function formatDurationLabel(startIso?: string | null, endIso?: string | null): string | null {
  if (!startIso || !endIso) return null;
  const startMs = Date.parse(startIso);
  const endMs = Date.parse(endIso);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return null;
  const diffSec = Math.round((endMs - startMs) / 1000);
  if (diffSec < 1 || diffSec > 21600) return null;
  if (diffSec < 60) return `${diffSec}s`;
  const mins = Math.floor(diffSec / 60);
  const secs = diffSec % 60;
  if (mins >= 5 || secs === 0) return `${mins}m`;
  return `${mins}m ${secs}s`;
}

interface GapGroupState {
  open: boolean;
  loading: boolean;
  turns: NormalizedTurn[];
  loadedUpTo: number;
  truncatedIndices: number[];
  error?: string;
}

interface AttachedProcess {
  key: string;
  gapRange?: { startIndex: number; endIndex: number };
  localTurns: NormalizedTurn[];
  prevTimestamp?: string | null;
}

type TranscriptItem =
  | {
      kind: 'turn';
      turn: NormalizedTurn;
      precedingProcess?: AttachedProcess;
      autoRevealProcess: boolean;
    }
  | { kind: 'standalone-process'; process: AttachedProcess };

interface SearchExplorerProps {
  selectedSessionId: string | null;
  highlightTurnIndex: number | null;
  onSelectSession: (sessionId: string, turnIndex?: number | null) => void;
  onCreateHandoff: (sessionId: string) => void;
  onPackageSkill?: (sessionId: string) => void;
  refreshSignal: number;
  listRefreshSignal?: number;
  hasPendingSessionUpdate?: boolean;
  onClearPendingSessionUpdate?: () => void;
}

const ToolCallCard = memo(function ToolCallCard({ tool }: { tool: ToolCallRecord }) {
  const [open, setOpen] = useState(false);

  const formattedArgs = useMemo(() => {
    if (typeof tool.arguments === 'string') return tool.arguments;
    try {
      return JSON.stringify(tool.arguments, null, 2);
    } catch {
      return String(tool.arguments);
    }
  }, [tool.arguments]);

  const statusTone =
    tool.status === 'error'
      ? 'border-rose-500/40 bg-rose-500/15 text-rose-300'
      : tool.status === 'pending'
        ? 'border-amber-500/40 bg-amber-500/15 text-amber-300'
        : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300';

  return (
    <div className="rounded-lg border border-slate-800/90 bg-slate-950/60">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs hover:bg-slate-900/60"
      >
        <div className="flex min-w-0 items-center gap-2">
          {open ? (
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-400" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-slate-400" />
          )}
          <Wrench className="h-3.5 w-3.5 shrink-0 text-sky-400" />
          <span className="truncate font-mono font-medium text-slate-200">{tool.name}</span>
          <span className={`rounded border px-1.5 py-0.5 text-[10px] uppercase ${statusTone}`}>
            {tool.status}
          </span>
        </div>
        {tool.duration_ms !== null && tool.duration_ms !== undefined && (
          <span className="shrink-0 font-mono text-[11px] tabular-nums text-slate-500">
            {tool.duration_ms}ms
          </span>
        )}
      </button>

      {open && (
        <div className="space-y-2 border-t border-slate-800/80 p-3 text-xs">
          <div>
            <div className="mb-1 flex items-center justify-between text-[11px] font-medium uppercase tracking-wider text-slate-400">
              <span>Arguments</span>
              <CopyButton text={formattedArgs} label="Copy JSON" />
            </div>
            <pre className="overflow-x-auto rounded-md border border-slate-800 bg-slate-900/80 p-2 font-mono text-[11px] text-slate-300">
              {formattedArgs}
            </pre>
          </div>
          {tool.output && (
            <div>
              <div className="mb-1 flex items-center justify-between text-[11px] font-medium uppercase tracking-wider text-slate-400">
                <span>Output</span>
                <CopyButton text={tool.output} label="Copy output" />
              </div>
              <pre className="max-h-48 overflow-auto rounded-md border border-slate-800 bg-slate-900/80 p-2 font-mono text-[11px] text-slate-300 whitespace-pre-wrap">
                {tool.output}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
});

const ThinkingCard = memo(function ThinkingCard({
  thinking,
  defaultOpen = false,
}: {
  thinking: string;
  defaultOpen?: boolean;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-lg border border-indigo-500/20 bg-indigo-950/20">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs text-indigo-300 hover:bg-indigo-950/35"
      >
        <div className="flex items-center gap-2">
          {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          <Brain className="h-3.5 w-3.5 text-indigo-400" />
          <span className="font-medium">{t('Reasoning / Thinking', '思考过程 / 推理')}</span>
          <span translate="no" className="font-mono text-[11px] text-indigo-400/70">
            ({thinking.length} {t('chars', '字符')})
          </span>
        </div>
        <span className="text-[11px] text-indigo-300/70">
          {open ? t('Hide', '收起') : t('Expand', '展开')}
        </span>
      </button>
      {open && (
        <div className="border-t border-indigo-500/20 px-3 py-2.5 font-mono text-xs leading-relaxed text-indigo-100/80 whitespace-pre-wrap">
          {thinking}
        </div>
      )}
    </div>
  );
});

const TurnCard = memo(function TurnCard({
  turn,
  highlighted,
  isTruncated,
  expanding,
  onExpandFullTurn,
  showProcess,
  autoRevealProcess = false,
  precedingProcess,
  gapState,
  onToggleGap,
  onLoadMoreGap,
  onExpandFullGapTurn,
  compact = false,
}: {
  turn: NormalizedTurn;
  highlighted: boolean;
  isTruncated: boolean;
  expanding: boolean;
  onExpandFullTurn: (turnIndex: number) => void;
  showProcess: boolean;
  autoRevealProcess?: boolean;
  precedingProcess?: AttachedProcess;
  gapState?: GapGroupState;
  onToggleGap?: (key: string, startIndex: number, endIndex: number) => void;
  onLoadMoreGap?: (key: string, endIndex: number) => void;
  onExpandFullGapTurn?: (turnIndex: number) => void;
  compact?: boolean;
}) {
  const { isZh, t } = useLocale();
  const [revealLocalProcess, setRevealLocalProcess] = useState(false);
  const isUser = turn.role === 'user';
  const gapCount = precedingProcess?.gapRange
    ? precedingProcess.gapRange.endIndex - precedingProcess.gapRange.startIndex + 1
    : 0;
  const localCount = precedingProcess?.localTurns.length ?? 0;
  const intermediateCount = gapCount + localCount;
  const hasOwnProcess = Boolean(turn.thinking) || turn.tool_calls.length > 0;
  const hasIntegratedFold =
    !showProcess && !autoRevealProcess && (intermediateCount > 0 || hasOwnProcess);

  const hasGap = Boolean(precedingProcess?.gapRange);
  const integratedExpanded = hasGap ? Boolean(gapState?.open) : revealLocalProcess;

  const durationLabel = precedingProcess
    ? formatDurationLabel(precedingProcess.prevTimestamp, turn.timestamp)
    : null;

  let foldLabel = '';
  if (intermediateCount > 0) {
    if (isZh) {
      const durPrefix = durationLabel ? `耗时 ${durationLabel} · ` : '';
      foldLabel = `${durPrefix}${intermediateCount} 步思考与工具过程`;
    } else {
      const durPrefix = durationLabel ? `Thought ${durationLabel} · ` : '';
      foldLabel = `${durPrefix}${intermediateCount} process step${intermediateCount === 1 ? '' : 's'} (thinking & tools)`;
    }
  } else if (turn.thinking && turn.tool_calls.length > 0) {
    foldLabel = isZh
      ? `思考过程与 ${turn.tool_calls.length} 次工具调用`
      : `Thinking & ${turn.tool_calls.length} tool call${turn.tool_calls.length === 1 ? '' : 's'}`;
  } else if (turn.thinking) {
    foldLabel = isZh
      ? `思考过程 (${turn.thinking.length.toLocaleString()} 字符)`
      : `Thinking process (${turn.thinking.length.toLocaleString()} chars)`;
  } else if (turn.tool_calls.length > 0) {
    foldLabel = isZh
      ? `${turn.tool_calls.length} 次工具调用`
      : `${turn.tool_calls.length} tool call${turn.tool_calls.length === 1 ? '' : 's'}`;
  }

  const handleToggleIntegrated = () => {
    if (hasGap && precedingProcess?.gapRange && onToggleGap) {
      const nextOpen = !(gapState?.open ?? false);
      setRevealLocalProcess(nextOpen);
      onToggleGap(
        precedingProcess.key,
        precedingProcess.gapRange.startIndex,
        precedingProcess.gapRange.endIndex
      );
    } else {
      setRevealLocalProcess((v) => !v);
    }
  };

  const roleBadge = isUser
    ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30'
    : turn.role === 'assistant'
      ? 'bg-indigo-500/15 text-indigo-300 border-indigo-500/30'
      : 'bg-slate-700/40 text-slate-300 border-slate-600/40';

  const cardClass = compact
    ? 'rounded-lg border border-slate-800/80 bg-slate-900/40 p-3'
    : highlighted
      ? 'rounded-xl border border-indigo-400/80 bg-indigo-950/25 p-4 ring-2 ring-indigo-500/30 transition-all'
      : !showProcess && isUser
        ? 'ml-auto max-w-[88%] rounded-2xl border border-slate-700/70 bg-slate-800/75 px-4 py-3 text-slate-100 shadow-sm transition-all'
        : !showProcess && turn.role === 'assistant'
          ? 'rounded-xl border border-slate-800/60 bg-slate-900/25 px-4 py-3.5 transition-all'
          : 'rounded-xl border border-slate-800/80 bg-slate-900/30 p-4 transition-all';

  const showDirectTrace = showProcess || autoRevealProcess;
  const gapEnd = precedingProcess?.gapRange?.endIndex ?? -1;
  const hasMoreGapSteps = Boolean(gapState) && gapEnd >= 0 && gapState!.loadedUpTo < gapEnd;
  const remainingGapSteps =
    gapState && gapEnd >= 0 ? Math.min(GAP_FETCH_LIMIT, gapEnd - gapState.loadedUpTo) : 0;
  const gapTruncatedSet = new Set(gapState?.truncatedIndices ?? []);

  return (
    <div id={`turn-${turn.turn_index}`} className={cardClass}>
      {hasIntegratedFold && (
        <div className="mb-2.5">
          <button
            type="button"
            aria-expanded={integratedExpanded}
            onClick={handleToggleIntegrated}
            className="inline-flex items-center gap-1.5 -mx-1 rounded-md px-1.5 py-1 text-left text-xs text-slate-400 transition-colors hover:bg-slate-800/60 hover:text-slate-200"
          >
            <Brain className="h-3.5 w-3.5 shrink-0 text-slate-500" />
            <span>{foldLabel}</span>
            {gapState?.loading && <span className="text-[11px] text-slate-500">· Loading...</span>}
            {integratedExpanded ? (
              <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-500" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-slate-500" />
            )}
          </button>

          {gapState?.error && (
            <div className="mt-1.5 rounded-lg border border-rose-500/30 bg-rose-950/20 px-3 py-1.5 text-xs text-rose-300">
              {gapState.error}
            </div>
          )}

          {integratedExpanded && (
            <div className="mt-2 space-y-2.5 border-l-2 border-indigo-500/30 pl-3.5">
              {(precedingProcess?.localTurns ?? []).map((pt) => (
                <TurnCard
                  key={pt.id}
                  turn={pt}
                  highlighted={false}
                  isTruncated={false}
                  expanding={expanding}
                  onExpandFullTurn={onExpandFullTurn}
                  showProcess={true}
                  compact={true}
                />
              ))}

              {(gapState?.turns ?? []).map((gt) => (
                <TurnCard
                  key={gt.id}
                  turn={gt}
                  highlighted={false}
                  isTruncated={gapTruncatedSet.has(gt.turn_index)}
                  expanding={expanding}
                  onExpandFullTurn={onExpandFullGapTurn ?? onExpandFullTurn}
                  showProcess={true}
                  compact={true}
                />
              ))}

              {hasMoreGapSteps && onLoadMoreGap && precedingProcess && (
                <div className="pt-0.5">
                  <button
                    type="button"
                    disabled={gapState?.loading}
                    onClick={() => onLoadMoreGap(precedingProcess.key, gapEnd)}
                    className="inline-flex items-center gap-1.5 rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1 text-[11px] font-medium text-slate-300 hover:border-indigo-500/60 hover:text-white disabled:opacity-50"
                  >
                    <ChevronDown className="h-3 w-3" />
                    {gapState?.loading ? 'Loading steps...' : `Load next ${remainingGapSteps} steps`}
                  </button>
                </div>
              )}

              {turn.thinking && (
                <ThinkingCard thinking={turn.thinking} defaultOpen={true} />
              )}

              {turn.tool_calls.length > 0 && (
                <div className="space-y-2 pt-1">
                  <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                    <Terminal className="h-3 w-3 text-sky-400" />
                    <span>Tool Calls ({turn.tool_calls.length})</span>
                  </div>
                  {turn.tool_calls.map((tc, idx) => (
                    <ToolCallCard
                      key={`${turn.id}-tc-${idx}-${tc.id || 'noid'}`}
                      tool={tc}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {(showProcess || compact || (!isUser && turn.role !== 'assistant')) && (
        <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className={`rounded-md border px-2 py-0.5 text-[11px] font-semibold uppercase ${roleBadge}`}>
              {turn.role}
            </span>
            <span className="font-mono text-xs tabular-nums text-slate-500">
              #{turn.turn_index}
            </span>
            {turn.model && (
              <span className="rounded bg-slate-800/80 px-1.5 py-0.5 font-mono text-[11px] text-slate-400">
                {turn.model}
              </span>
            )}
            {turn.redacted && (
              <span
                title="Secrets were redacted from this turn before storage"
                className="inline-flex items-center gap-1 rounded border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[11px] text-amber-300"
              >
                <ShieldCheck className="h-3 w-3" />
                Redacted
              </span>
            )}
          </div>

          <div className="flex items-center gap-2">
            {turn.token_count != null && (
              <span className="font-mono text-[11px] tabular-nums text-slate-500">
                ~{turn.token_count} tok
              </span>
            )}
            <span className="text-xs text-slate-500">{formatTimestamp(turn.timestamp)}</span>
            <CopyButton text={turn.content} label="Copy turn" />
          </div>
        </div>
      )}

      {showDirectTrace && turn.thinking && (
        <div className="mb-3">
          <ThinkingCard
            thinking={turn.thinking}
            defaultOpen={autoRevealProcess}
          />
        </div>
      )}

      <MarkdownContent content={turn.content} />

              {isTruncated && (
        <div className="mt-2.5 flex items-center justify-between gap-2 rounded-lg border border-amber-500/30 bg-amber-950/20 px-3 py-1.5 text-[11px] text-amber-200">
          <span>
            {t(
              'Oversized payload truncated for smooth rendering.',
              '超长内容已摘要截断以保证流畅浏览。'
            )}
          </span>
          <button
            type="button"
            disabled={expanding}
            onClick={() => onExpandFullTurn(turn.turn_index)}
            className="rounded border border-amber-500/40 bg-amber-500/15 px-2.5 py-0.5 font-medium text-amber-100 hover:bg-amber-500/25 disabled:opacity-50"
          >
            <span>
              {expanding ? t('Loading...', '加载中...') : t('Load full turn', '加载完整内容')}
            </span>
          </button>
        </div>
      )}

      {showDirectTrace && turn.tool_calls.length > 0 && (
        <div className="mt-3 space-y-2 border-t border-slate-800/70 pt-3">
          <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
            <Terminal className="h-3 w-3 text-sky-400" />
            <span>{`${t('Tool Calls', '工具调用')} (${turn.tool_calls.length})`}</span>
          </div>
          {turn.tool_calls.map((tc, idx) => (
            <ToolCallCard
              key={`${turn.id}-tc-${idx}-${tc.id || 'noid'}`}
              tool={tc}
            />
          ))}
        </div>
      )}

      {!showProcess && !compact && (isUser || turn.role === 'assistant') && (
        <div className="mt-2 flex flex-wrap items-center justify-end gap-2.5 text-[11px] text-slate-500">
          {!isUser && turn.model && (
            <span translate="no" className="font-mono text-slate-500">
              {turn.model}
            </span>
          )}
          <span translate="no" className="font-mono tabular-nums">
            #{turn.turn_index}
          </span>
          <span translate="no">{formatTimestamp(turn.timestamp)}</span>
          <CopyButton text={turn.content} label={t('Copy turn', '复制本轮')} />
        </div>
      )}
    </div>
  );
});

const ProcessGroupPill = memo(function ProcessGroupPill({
  count,
  subtitle,
  open,
  loading,
  error,
  turns,
  truncatedSet,
  expandingTurnIndex,
  hasMoreInGroup,
  remainingCount,
  onToggle,
  onLoadMore,
  onExpandFullTurn,
}: {
  count: number;
  subtitle: string;
  open: boolean;
  loading: boolean;
  error?: string;
  turns: NormalizedTurn[];
  truncatedSet: Set<number>;
  expandingTurnIndex: number | null;
  hasMoreInGroup?: boolean;
  remainingCount?: number;
  onToggle: () => void;
  onLoadMore?: () => void;
  onExpandFullTurn: (turnIndex: number) => void;
}) {
  const { isZh, t } = useLocale();
  const label = isZh
    ? `${count} 步过程记录 ${subtitle}`
    : `${count} process step${count === 1 ? '' : 's'} ${subtitle}`;
  return (
    <div className="px-1">
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-left text-xs text-slate-400 transition-colors hover:bg-slate-800/60 hover:text-slate-200"
      >
        <Brain className="h-3.5 w-3.5 shrink-0 text-slate-500" />
        <span>{label}</span>
        {loading && (
          <span className="text-[11px] text-slate-500">{t('· Loading...', '· 加载中...')}</span>
        )}
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-500" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-slate-500" />
        )}
      </button>

      {error && (
        <div className="mt-1.5 rounded-lg border border-rose-500/30 bg-rose-950/20 px-3 py-1.5 text-xs text-rose-300">
          {error}
        </div>
      )}

      {open && turns.length > 0 && (
        <div className="mt-2 space-y-2.5 border-l-2 border-indigo-500/30 pl-3.5">
          {turns.map((turn) => (
            <TurnCard
              key={turn.id}
              turn={turn}
              highlighted={false}
              isTruncated={truncatedSet.has(turn.turn_index)}
              expanding={expandingTurnIndex === turn.turn_index}
              onExpandFullTurn={onExpandFullTurn}
              showProcess={true}
              compact={true}
            />
          ))}
          {hasMoreInGroup && onLoadMore && (
            <div className="pt-0.5">
              <button
                type="button"
                disabled={loading}
                onClick={onLoadMore}
                className="inline-flex items-center gap-1.5 rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1 text-[11px] font-medium text-slate-300 hover:border-indigo-500/60 hover:text-white disabled:opacity-50"
              >
                <ChevronDown className="h-3 w-3" />
                <span>
                  {loading
                    ? t('Loading steps...', '加载步骤中...')
                    : isZh
                      ? `继续加载下 ${remainingCount} 步`
                      : `Load next ${remainingCount} steps`}
                </span>
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
});

export function SearchExplorer({
  selectedSessionId,
  highlightTurnIndex,
  onSelectSession,
  onCreateHandoff,
  onPackageSkill,
  refreshSignal,
  listRefreshSignal = 0,
  hasPendingSessionUpdate = false,
  onClearPendingSessionUpdate,
}: SearchExplorerProps) {
  const { locale, isZh, t } = useLocale();
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<SearchMode>('hybrid');
  const [selectedAgents, setSelectedAgents] = useState<string[]>([]);
  const [workspaceFilter, setWorkspaceFilter] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [showSubagents, setShowSubagents] = useState(false);

  const [listView, setListView] = useState<'sessions' | 'hits'>('sessions');
  const [sessions, setSessions] = useState<NormalizedSession[]>([]);
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [searchMeta, setSearchMeta] = useState<{ total: number; took_ms: number } | null>(null);
  const [agentFacets, setAgentFacets] = useState<Record<string, number>>({});
  const [workspaceOptions, setWorkspaceOptions] = useState<string[]>([]);
  const [loadingList, setLoadingList] = useState(false);

  const [detail, setDetail] = useState<NormalizedSession | null>(null);
  const [linkedMemories, setLinkedMemories] = useState<MemoryCard[]>([]);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [totalTurns, setTotalTurns] = useState(0);
  const [turnWindowStart, setTurnWindowStart] = useState(0);
  const [hasEarlierTurns, setHasEarlierTurns] = useState(false);
  const [hasLaterTurns, setHasLaterTurns] = useState(false);
  const [truncatedIndices, setTruncatedIndices] = useState<Set<number>>(() => new Set());
  const [loadingMoreTurns, setLoadingMoreTurns] = useState(false);
  const [expandingTurnIndex, setExpandingTurnIndex] = useState<number | null>(null);
  const [detailReloadTick, setDetailReloadTick] = useState(0);
  const [processVisibleSessionId, setProcessVisibleSessionId] = useState<string | null>(null);
  const [expandedLocalGroups, setExpandedLocalGroups] = useState<Record<string, boolean>>({});
  const [expandedGaps, setExpandedGaps] = useState<Record<string, GapGroupState>>({});
  const [error, setError] = useState<string | null>(null);
  const [resetTick, setResetTick] = useState(0);

  const showProcess =
    selectedSessionId !== null && processVisibleSessionId === selectedSessionId;
  const fetchQaOnly = !showProcess && highlightTurnIndex === null;

  const transcriptContainerRef = useRef<HTMLDivElement>(null);
  const selectedSessionIdRef = useRef(selectedSessionId);
  selectedSessionIdRef.current = selectedSessionId;
  const prevShowSubagentsRef = useRef(showSubagents);

  const loadSessions = useCallback(async () => {
    setLoadingList(true);
    setError(null);
    try {
      const res = await apiClient.listSessions({
        agent: selectedAgents.join(',') || undefined,
        workspace: workspaceFilter || undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        include_subagents: showSubagents,
        limit: 50,
      });
      setSessions(res.sessions);

      const facets: Record<string, number> = {};
      const workspaces = new Set<string>();
      for (const s of res.sessions) {
        facets[s.agent] = (facets[s.agent] ?? 0) + 1;
        if (s.workspace) workspaces.add(s.workspace);
      }
      if (!query.trim()) {
        setAgentFacets(facets);
      }
      setWorkspaceOptions((prev) => {
        const merged = new Set([...prev, ...workspaces]);
        return Array.from(merged).sort();
      });

      if (!selectedSessionIdRef.current && res.sessions.length > 0) {
        onSelectSession(res.sessions[0].id, null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load sessions');
    } finally {
      setLoadingList(false);
    }
  }, [selectedAgents, workspaceFilter, dateFrom, dateTo, showSubagents, query, onSelectSession]);

  const runSearch = useCallback(
    async (overrideQuery?: string) => {
      const effectiveQ = overrideQuery !== undefined ? overrideQuery : query;
      if (!effectiveQ.trim()) {
        setListView('sessions');
        setSearchMeta(null);
        await loadSessions();
        return;
      }

      setLoadingList(true);
      setError(null);
      setListView('hits');
      try {
        const res = await apiClient.search({
          q: effectiveQ,
          mode,
          agent: selectedAgents.join(',') || undefined,
          workspace: workspaceFilter || undefined,
          date_from: dateFrom || undefined,
          date_to: dateTo || undefined,
          limit: 40,
        });
        setHits(res.hits);
        setSearchMeta({ total: res.total, took_ms: res.took_ms });
        setAgentFacets(res.facets.agents);
        const ws = Object.keys(res.facets.workspaces);
        if (ws.length > 0) {
          setWorkspaceOptions((prev) => Array.from(new Set([...prev, ...ws])).sort());
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Search failed');
      } finally {
        setLoadingList(false);
      }
    },
    [query, mode, selectedAgents, workspaceFilter, dateFrom, dateTo, loadSessions]
  );

  useEffect(() => {
    if (query.trim()) {
      void runSearch();
    } else {
      void loadSessions();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, selectedAgents, workspaceFilter, dateFrom, dateTo, refreshSignal, listRefreshSignal, resetTick]);

  useEffect(() => {
    if (prevShowSubagentsRef.current === showSubagents) return;
    prevShowSubagentsRef.current = showSubagents;
    void loadSessions();
  }, [showSubagents, loadSessions]);

  useEffect(() => {
    setExpandedLocalGroups({});
    setExpandedGaps({});
    if (!selectedSessionId) {
      setDetail(null);
      setLinkedMemories([]);
      setTotalTurns(0);
      setTurnWindowStart(0);
      setHasEarlierTurns(false);
      setHasLaterTurns(false);
      setTruncatedIndices(new Set());
      return;
    }
    let cancelled = false;
    setLoadingDetail(true);
    const targetOffset =
      highlightTurnIndex !== null ? Math.max(0, highlightTurnIndex - 15) : undefined;
    apiClient
      .getSession(selectedSessionId, {
        turn_offset: targetOffset,
        turn_limit: TURN_PAGE_SIZE,
        max_turn_chars: MAX_TURN_CHARS,
        qa_only: fetchQaOnly,
      })
      .then((res) => {
        if (cancelled) return;
        setDetail(res.session);
        setLinkedMemories(res.linked_memories);
        const loadedCount = res.session.turns?.length ?? 0;
        const total = res.total_turns ?? Math.max(res.session.turn_count, loadedCount);
        const start = res.turn_offset ?? 0;
        setTotalTurns(total);
        setTurnWindowStart(start);
        setHasEarlierTurns(res.has_earlier_turns ?? start > 0);
        setHasLaterTurns(res.has_later_turns ?? start + loadedCount < total);
        setTruncatedIndices(new Set(res.truncated_turn_indices ?? []));
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load session');
      })
      .finally(() => {
        if (!cancelled) setLoadingDetail(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedSessionId, highlightTurnIndex, refreshSignal, detailReloadTick, fetchQaOnly]);

  const handleLoadEarlierTurns = useCallback(async () => {
    if (!detail || !hasEarlierTurns || loadingMoreTurns) return;
    const prevLimit = Math.min(TURN_PAGE_SIZE, turnWindowStart);
    const prevOffset = Math.max(0, turnWindowStart - prevLimit);
    if (prevLimit <= 0) return;
    setLoadingMoreTurns(true);
    try {
      const res = await apiClient.getSession(detail.id, {
        turn_offset: prevOffset,
        turn_limit: prevLimit,
        max_turn_chars: MAX_TURN_CHARS,
        qa_only: fetchQaOnly,
      });
      const olderTurns = res.session.turns ?? [];
      setDetail((prev) => {
        if (!prev || prev.id !== detail.id) return prev;
        const existingIds = new Set((prev.turns ?? []).map((t) => t.turn_index));
        const merged = [...olderTurns.filter((t) => !existingIds.has(t.turn_index)), ...(prev.turns ?? [])];
        return { ...prev, turns: merged };
      });
      setTurnWindowStart(prevOffset);
      setHasEarlierTurns(prevOffset > 0);
      setTruncatedIndices((prev) => {
        const next = new Set(prev);
        for (const idx of res.truncated_turn_indices ?? []) next.add(idx);
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load earlier turns');
    } finally {
      setLoadingMoreTurns(false);
    }
  }, [detail, hasEarlierTurns, loadingMoreTurns, turnWindowStart, fetchQaOnly]);

  const handleLoadNewerTurns = useCallback(async () => {
    if (!detail || !hasLaterTurns || loadingMoreTurns) return;
    const loadedTurns = detail.turns ?? [];
    const nextOffset = turnWindowStart + loadedTurns.length;
    setLoadingMoreTurns(true);
    try {
      const res = await apiClient.getSession(detail.id, {
        turn_offset: nextOffset,
        turn_limit: TURN_PAGE_SIZE,
        max_turn_chars: MAX_TURN_CHARS,
        qa_only: fetchQaOnly,
      });
      const newerTurns = res.session.turns ?? [];
      setDetail((prev) => {
        if (!prev || prev.id !== detail.id) return prev;
        const existingIds = new Set((prev.turns ?? []).map((t) => t.turn_index));
        const merged = [...(prev.turns ?? []), ...newerTurns.filter((t) => !existingIds.has(t.turn_index))];
        return { ...prev, turns: merged };
      });
      setHasLaterTurns(res.has_later_turns ?? nextOffset + newerTurns.length < totalTurns);
      setTruncatedIndices((prev) => {
        const next = new Set(prev);
        for (const idx of res.truncated_turn_indices ?? []) next.add(idx);
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load newer turns');
    } finally {
      setLoadingMoreTurns(false);
    }
  }, [detail, hasLaterTurns, loadingMoreTurns, turnWindowStart, totalTurns, fetchQaOnly]);

  const handleExpandFullTurn = useCallback(
    async (turnIndex: number) => {
      if (!detail || expandingTurnIndex !== null) return;
      setExpandingTurnIndex(turnIndex);
      try {
        const res = await apiClient.getSessionTurn(detail.id, turnIndex);
        setDetail((prev) => {
          if (!prev || prev.id !== detail.id) return prev;
          return {
            ...prev,
            turns: (prev.turns ?? []).map((t) =>
              t.turn_index === turnIndex ? res.turn : t
            ),
          };
        });
        setTruncatedIndices((prev) => {
          const next = new Set(prev);
          next.delete(turnIndex);
          return next;
        });
        setExpandedGaps((prev) => {
          let changed = false;
          const next: Record<string, GapGroupState> = {};
          for (const [k, gap] of Object.entries(prev)) {
            if (gap.turns.some((t) => t.turn_index === turnIndex)) {
              changed = true;
              next[k] = {
                ...gap,
                turns: gap.turns.map((t) => (t.turn_index === turnIndex ? res.turn : t)),
                truncatedIndices: gap.truncatedIndices.filter((idx) => idx !== turnIndex),
              };
            } else {
              next[k] = gap;
            }
          }
          return changed ? next : prev;
        });
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load full turn');
      } finally {
        setExpandingTurnIndex(null);
      }
    },
    [detail, expandingTurnIndex]
  );

  const handleToggleGapGroup = useCallback(
    async (key: string, startIndex: number, endIndex: number) => {
      if (!detail) return;
      const existing = expandedGaps[key];
      if (existing && (existing.loading || existing.turns.length > 0)) {
        setExpandedGaps((prev) => ({
          ...prev,
          [key]: { ...existing, open: !existing.open },
        }));
        return;
      }

      const fetchCount = Math.min(GAP_FETCH_LIMIT, endIndex - startIndex + 1);
      setExpandedGaps((prev) => ({
        ...prev,
        [key]: {
          open: true,
          loading: true,
          turns: [],
          loadedUpTo: startIndex - 1,
          truncatedIndices: [],
        },
      }));
      try {
        const res = await apiClient.getSession(detail.id, {
          turn_offset: startIndex,
          turn_limit: fetchCount,
          max_turn_chars: MAX_TURN_CHARS,
          qa_only: false,
        });
        const fetched = (res.session.turns ?? []).filter(
          (t) => t.turn_index >= startIndex && t.turn_index <= endIndex
        );
        const lastFetched =
          fetched.length > 0
            ? fetched[fetched.length - 1].turn_index
            : startIndex + fetchCount - 1;
        setExpandedGaps((prev) => ({
          ...prev,
          [key]: {
            open: true,
            loading: false,
            turns: fetched,
            loadedUpTo: lastFetched,
            truncatedIndices: res.truncated_turn_indices ?? [],
          },
        }));
      } catch (err) {
        setExpandedGaps((prev) => ({
          ...prev,
          [key]: {
            open: false,
            loading: false,
            turns: [],
            loadedUpTo: startIndex - 1,
            truncatedIndices: [],
            error: err instanceof Error ? err.message : 'Failed to load intermediate steps',
          },
        }));
      }
    },
    [detail, expandedGaps]
  );

  const handleLoadMoreGapGroup = useCallback(
    async (key: string, endIndex: number) => {
      if (!detail) return;
      const existing = expandedGaps[key];
      if (!existing || existing.loading || existing.loadedUpTo >= endIndex) return;
      const nextStart = existing.loadedUpTo + 1;
      const remaining = endIndex - nextStart + 1;
      const fetchCount = Math.min(GAP_FETCH_LIMIT, remaining);
      setExpandedGaps((prev) => ({
        ...prev,
        [key]: { ...existing, loading: true, error: undefined },
      }));
      try {
        const res = await apiClient.getSession(detail.id, {
          turn_offset: nextStart,
          turn_limit: fetchCount,
          max_turn_chars: MAX_TURN_CHARS,
          qa_only: false,
        });
        const fetched = (res.session.turns ?? []).filter(
          (t) => t.turn_index >= nextStart && t.turn_index <= endIndex
        );
        const seen = new Set(existing.turns.map((t) => t.turn_index));
        const merged = [...existing.turns, ...fetched.filter((t) => !seen.has(t.turn_index))];
        const lastFetched =
          merged.length > 0
            ? merged[merged.length - 1].turn_index
            : nextStart + fetchCount - 1;
        const truncSet = new Set([
          ...existing.truncatedIndices,
          ...(res.truncated_turn_indices ?? []),
        ]);
        setExpandedGaps((prev) => ({
          ...prev,
          [key]: {
            open: true,
            loading: false,
            turns: merged,
            loadedUpTo: lastFetched,
            truncatedIndices: Array.from(truncSet),
          },
        }));
      } catch (err) {
        setExpandedGaps((prev) => ({
          ...prev,
          [key]: {
            ...existing,
            loading: false,
            error: err instanceof Error ? err.message : 'Failed to load more steps',
          },
        }));
      }
    },
    [detail, expandedGaps]
  );

  const transcriptItems = useMemo<TranscriptItem[]>(() => {
    if (!detail) return [];
    const turns = detail.turns ?? [];
    if (showProcess) {
      return turns.map((turn) => ({
        kind: 'turn' as const,
        turn,
        autoRevealProcess: highlightTurnIndex === turn.turn_index && isProcessTurn(turn),
      }));
    }
    if (turns.length === 0) return [];

    const items: TranscriptItem[] = [];
    let pendingGap: { startIndex: number; endIndex: number } | undefined;
    let pendingLocal: NormalizedTurn[] = [];
    let prevVisibleTs: string | null | undefined;

    const buildAttachedProcess = (): AttachedProcess | undefined => {
      if (!pendingGap && pendingLocal.length === 0) return undefined;
      const key = pendingGap
        ? `gap-${pendingGap.startIndex}-${pendingGap.endIndex}`
        : `local-${pendingLocal[0].turn_index}-${pendingLocal[pendingLocal.length - 1].turn_index}`;
      return {
        key,
        gapRange: pendingGap,
        localTurns: pendingLocal,
        prevTimestamp: prevVisibleTs,
      };
    };

    const flushStandalone = () => {
      const proc = buildAttachedProcess();
      if (proc) {
        items.push({ kind: 'standalone-process', process: proc });
      }
      pendingGap = undefined;
      pendingLocal = [];
    };

    if (!hasEarlierTurns && highlightTurnIndex === null && turns[0].turn_index > 0) {
      pendingGap = { startIndex: 0, endIndex: turns[0].turn_index - 1 };
    }

    for (let i = 0; i < turns.length; i++) {
      const turn = turns[i];
      if (i > 0) {
        const prevIdx = turns[i - 1].turn_index;
        if (turn.turn_index - prevIdx > 1) {
          flushStandalone();
          pendingGap = { startIndex: prevIdx + 1, endIndex: turn.turn_index - 1 };
        }
      }

      const hideLocally = isProcessTurn(turn) && turn.turn_index !== highlightTurnIndex;
      if (hideLocally) {
        pendingLocal.push(turn);
        continue;
      }

      if (turn.role === 'assistant') {
        const precedingProcess = buildAttachedProcess();
        pendingGap = undefined;
        pendingLocal = [];
        items.push({
          kind: 'turn',
          turn,
          precedingProcess,
          autoRevealProcess: highlightTurnIndex === turn.turn_index,
        });
      } else {
        flushStandalone();
        items.push({
          kind: 'turn',
          turn,
          autoRevealProcess: highlightTurnIndex === turn.turn_index,
        });
      }
      prevVisibleTs = turn.timestamp ?? prevVisibleTs;
    }

    flushStandalone();

    const lastIdx = turns[turns.length - 1].turn_index;
    const rawTotal = Math.max(detail.turn_count ?? 0, totalTurns);
    if (!hasLaterTurns && highlightTurnIndex === null && lastIdx + 1 < rawTotal) {
      items.push({
        kind: 'standalone-process',
        process: {
          key: `gap-${lastIdx + 1}-${rawTotal - 1}`,
          gapRange: { startIndex: lastIdx + 1, endIndex: rawTotal - 1 },
          localTurns: [],
          prevTimestamp: prevVisibleTs,
        },
      });
    }

    return items;
  }, [detail, showProcess, hasEarlierTurns, hasLaterTurns, highlightTurnIndex, totalTurns]);

  useEffect(() => {
    if (highlightTurnIndex === null || !detail) return;
    const timer = window.setTimeout(() => {
      const el = document.getElementById(`turn-${highlightTurnIndex}`);
      el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 80);
    return () => window.clearTimeout(timer);
  }, [highlightTurnIndex, detail]);

  const toggleAgent = (agent: string) => {
    setSelectedAgents((prev) =>
      prev.includes(agent) ? prev.filter((a) => a !== agent) : [...prev, agent]
    );
  };

  const resetFilters = () => {
    setQuery('');
    setSelectedAgents([]);
    setWorkspaceFilter('');
    setDateFrom('');
    setDateTo('');
    setListView('sessions');
    setSearchMeta(null);
    setResetTick((t) => t + 1);
  };

  const availableAgents = useMemo(() => {
    const keys = new Set([
      ...Object.keys(agentFacets),
      ...sessions.map((s) => s.agent),
      'antigravity',
      'claude_code',
      'codex',
      'cursor',
    ]);
    return Array.from(keys);
  }, [agentFacets, sessions]);

  return (
    <div className="flex h-full flex-col gap-4">
      {/* Search & Filter Control Panel */}
      <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4 shadow-sm">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void runSearch();
          }}
          className="flex flex-col gap-3 lg:flex-row lg:items-center"
        >
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute top-1/2 left-3.5 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t(
                'Search transcripts across Antigravity, Claude Code, Codex, Cursor... (e.g. sqlite wal, redaction, cursor)',
                '跨 Antigravity、Claude Code、Codex、Cursor 搜索历史对话...（例如 sqlite wal、redaction、权限）'
              )}
              className="w-full rounded-lg border border-slate-700/80 bg-slate-950/90 py-2.5 pr-4 pl-10 text-sm text-slate-100 placeholder-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 focus:outline-none"
            />
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex rounded-lg border border-slate-800 bg-slate-950 p-0.5">
              {(['hybrid', 'fts', 'vector'] as SearchMode[]).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setMode(m)}
                  className={`rounded-md px-3 py-1.5 text-xs font-medium uppercase tracking-wide transition-colors ${
                    mode === m
                      ? 'bg-indigo-600 text-white shadow-sm'
                      : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  <span>
                    {m === 'hybrid'
                      ? t('hybrid', '混合')
                      : m === 'fts'
                        ? 'FTS'
                        : t('vector', '矢量')}
                  </span>
                </button>
              ))}
            </div>

            <button
              type="submit"
              className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-xs font-semibold text-white transition-colors hover:bg-indigo-500"
            >
              <Sparkles className="h-3.5 w-3.5" />
              <span>{t('Search', '搜索')}</span>
            </button>

            {(query || selectedAgents.length > 0 || workspaceFilter || dateFrom || dateTo) && (
              <button
                type="button"
                onClick={resetFilters}
                className="inline-flex items-center gap-1 rounded-lg border border-slate-700/80 bg-slate-800/60 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 hover:text-white"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                <span>{t('Reset', '重置')}</span>
              </button>
            )}
          </div>
        </form>

        {/* Facet Filters */}
        <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-slate-800/80 pt-3">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="mr-1 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
              {t('Agents:', '智能体:')}
            </span>
            {availableAgents.map((agent) => {
              const active = selectedAgents.includes(agent);
              const count = agentFacets[agent];
              const label = AGENT_META[agent]?.label ?? agent;
              return (
                <button
                  key={agent}
                  type="button"
                  onClick={() => toggleAgent(agent)}
                  className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors ${
                    active
                      ? 'border-indigo-500/70 bg-indigo-500/20 text-indigo-200'
                      : 'border-slate-800 bg-slate-950/70 text-slate-400 hover:border-slate-700 hover:text-slate-200'
                  }`}
                >
                  <span translate="no">{label}</span>
                  {count !== undefined && (
                    <span
                      translate="no"
                      className="font-mono text-[10px] tabular-nums text-slate-500"
                    >
                      {count}
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-1.5 rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1">
              <FolderGit2 className="h-3.5 w-3.5 text-slate-500" />
              <select
                value={workspaceFilter}
                onChange={(e) => setWorkspaceFilter(e.target.value)}
                className="bg-transparent text-xs text-slate-300 focus:outline-none"
                aria-label="Workspace filter"
              >
                <option value="" className="bg-slate-900">
                  {t('All workspaces', '全部工作区')}
                </option>
                {workspaceOptions.map((ws) => (
                  <option key={ws} value={ws} className="bg-slate-900">
                    {shortenWorkspace(ws)}
                  </option>
                ))}
              </select>
            </div>

            <div className="flex items-center gap-1.5 rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1 text-xs text-slate-400">
              <Calendar className="h-3.5 w-3.5 text-slate-500" />
              <input
                type="date"
                value={dateFrom}
                onChange={(e) => setDateFrom(e.target.value)}
                aria-label="Date from"
                className="bg-transparent text-xs text-slate-300 focus:outline-none"
              />
              <span>–</span>
              <input
                type="date"
                value={dateTo}
                onChange={(e) => setDateTo(e.target.value)}
                aria-label="Date to"
                className="bg-transparent text-xs text-slate-300 focus:outline-none"
              />
            </div>
          </div>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-rose-500/40 bg-rose-950/30 px-4 py-2.5 text-xs text-rose-300">
          {error}
        </div>
      )}

      {/* Main Split Pane */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-12">
        {/* Left: Results / Sessions List */}
        <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/50 lg:col-span-5 xl:col-span-4">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-3.5 py-2.5">
            <div className="flex items-center gap-1 rounded-lg bg-slate-950 p-0.5">
              <button
                type="button"
                onClick={() => setListView('sessions')}
                className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                  listView === 'sessions'
                    ? 'bg-slate-800 text-white'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                <span key={`sess-${locale}`}>
                  {`${t('Sessions', '会话')} (${sessions.length})`}
                </span>
              </button>
              <button
                type="button"
                onClick={() => setListView('hits')}
                className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                  listView === 'hits'
                    ? 'bg-slate-800 text-white'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                <span key={`hits-${locale}`}>
                  {`${t('Search Hits', '搜索命中')} (${hits.length})`}
                </span>
              </button>
            </div>
            {listView === 'sessions' && (
              <button
                type="button"
                aria-pressed={showSubagents}
                onClick={() => setShowSubagents((v) => !v)}
                title={t(
                  'Include internal subagent / system-initiated sessions (hidden by default)',
                  '显示后台子代理/系统触发的会话（默认隐藏）'
                )}
                className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors ${
                  showSubagents
                    ? 'border-indigo-500/50 bg-indigo-500/20 text-indigo-200 hover:bg-indigo-500/30'
                    : 'border-slate-800 bg-slate-950/70 text-slate-400 hover:border-slate-700 hover:text-slate-200'
                }`}
              >
                <span>
                  {showSubagents
                    ? t('Subagents: On', '子代理: 开')
                    : t('Show subagents', '显示子代理')}
                </span>
              </button>
            )}
            {searchMeta && listView === 'hits' && (
              <span translate="no" className="font-mono text-[11px] text-slate-500">
                {`${searchMeta.total} ${t('hits', '条')} · ${searchMeta.took_ms}ms`}
              </span>
            )}
          </div>

          <div className="flex-1 divide-y divide-slate-800/70 overflow-y-auto">
            {loadingList ? (
              <div className="p-6 text-center text-xs text-slate-500">
                <span>{t('Loading transcripts...', '正在加载会话列表...')}</span>
              </div>
            ) : listView === 'hits' ? (
              hits.length === 0 ? (
                <div className="p-8 text-center text-xs text-slate-500">
                  <span>
                    {t(
                      'No matching turns found. Try switching to hybrid mode or clearing filters.',
                      '未找到匹配对话。可尝试切换到混合检索或清除筛选条件。'
                    )}
                  </span>
                </div>
              ) : (
                hits.map((hit) => {
                  const isSelected =
                    selectedSessionId === hit.session_id && highlightTurnIndex === hit.turn_index;
                  return (
                    <button
                      key={hit.turn_id}
                      type="button"
                      onClick={() => onSelectSession(hit.session_id, hit.turn_index)}
                      className={`w-full p-3.5 text-left transition-colors hover:bg-slate-800/40 ${
                        isSelected ? 'bg-indigo-500/10 ring-1 ring-inset ring-indigo-500/40' : ''
                      }`}
                    >
                      <div className="mb-1.5 flex items-center justify-between gap-2">
                        <AgentBadge agent={hit.agent} compact />
                        <div className="flex items-center gap-1.5 font-mono text-[10px] text-slate-400">
                          <span className="rounded bg-indigo-500/15 px-1.5 py-0.5 text-indigo-300">
                            {hit.match_mode.toUpperCase()} {hit.score.toFixed(2)}
                          </span>
                          <span>#{hit.turn_index}</span>
                        </div>
                      </div>
                      <div className="line-clamp-1 text-sm font-medium text-slate-100">
                        {hit.session_title}
                      </div>
                      <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-slate-400">
                        {hit.snippet}
                      </p>
                      <div className="mt-2 flex items-center justify-between text-[11px] text-slate-500">
                        <span className="truncate font-mono">{shortenWorkspace(hit.workspace)}</span>
                        <span>{formatRelativeTime(hit.timestamp)}</span>
                      </div>
                    </button>
                  );
                })
              )
            ) : sessions.length === 0 ? (
              <div className="p-8 text-center text-xs text-slate-500">
                No sessions match the selected filters.
              </div>
            ) : (
              sessions.map((session) => {
                const isSelected = selectedSessionId === session.id;
                return (
                  <button
                    key={session.id}
                    type="button"
                    onClick={() => onSelectSession(session.id, null)}
                    className={`w-full p-3.5 text-left transition-colors hover:bg-slate-800/40 ${
                      isSelected ? 'bg-indigo-500/10 ring-1 ring-inset ring-indigo-500/40' : ''
                    }`}
                  >
                    <div className="mb-1.5 flex items-center justify-between gap-2">
                      <AgentBadge agent={session.agent} />
                      <span className="font-mono text-[11px] tabular-nums text-slate-400">
                        {session.turn_count} turns
                      </span>
                    </div>
                    <div className="line-clamp-1 text-sm font-medium text-slate-100">
                      {session.title}
                    </div>
                    {session.summary && (
                      <p className="mt-1 line-clamp-2 text-xs text-slate-400">{session.summary}</p>
                    )}
                    <div className="mt-2 flex items-center justify-between text-[11px] text-slate-500">
                      <span className="truncate font-mono">
                        {shortenWorkspace(session.workspace)}
                      </span>
                      <span>{formatRelativeTime(session.updated_at)}</span>
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>

        {/* Right: Turn-by-Turn Transcript Viewer */}
        <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40 lg:col-span-7 xl:col-span-8">
          {loadingDetail ? (
            <div className="flex flex-1 items-center justify-center p-10 text-sm text-slate-500">
              <span>{t('Loading transcript...', '正在加载对话记录...')}</span>
            </div>
          ) : !detail ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 p-10 text-center">
              <MessageSquare className="h-8 w-8 text-slate-600" />
              <p className="text-sm font-medium text-slate-300">
                {t('Select a session or search hit', '请选择左侧会话或搜索结果')}
              </p>
              <p className="max-w-sm text-xs text-slate-500">
                {t(
                  'Inspect full turn-by-turn conversations, collapsed thinking blocks, tool call arguments/outputs, and linked memories.',
                  '支持一问一答清晰浏览、按需展开思考与工具调用过程，以及查看关联的中央记忆。'
                )}
              </p>
            </div>
          ) : (
            <>
              <div className="border-b border-slate-800 bg-slate-900/70 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="mb-1.5 flex flex-wrap items-center gap-2">
                      <AgentBadge agent={detail.agent} />
                      {detail.model && (
                        <span
                          translate="no"
                          className="rounded border border-slate-700 bg-slate-800/80 px-2 py-0.5 font-mono text-[11px] text-slate-300"
                        >
                          {detail.model}
                        </span>
                      )}
                      {linkedMemories.length > 0 && (
                        <span
                          key={`mem-${locale}`}
                          className="rounded border border-violet-500/30 bg-violet-500/15 px-2 py-0.5 text-[11px] font-medium text-violet-300"
                        >
                          {isZh
                            ? `${linkedMemories.length} 条关联记忆`
                            : `${linkedMemories.length} linked memor${linkedMemories.length === 1 ? 'y' : 'ies'}`}
                        </span>
                      )}
                    </div>
                    <h2 className="text-base font-semibold text-white">{detail.title}</h2>
                    <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px] text-slate-400">
                      <span translate="no">{detail.workspace ?? t('No workspace', '无工作区')}</span>
                      <span translate="no" className="truncate text-slate-500">
                        {detail.source_path}
                      </span>
                    </div>
                  </div>

                  <div className="flex shrink-0 flex-wrap items-center gap-2">
                    {hasPendingSessionUpdate && (
                      <button
                        type="button"
                        onClick={() => {
                          onClearPendingSessionUpdate?.();
                          setDetailReloadTick((t) => t + 1);
                        }}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/15 px-2.5 py-1.5 text-xs font-medium text-emerald-200 hover:bg-emerald-500/25"
                      >
                        <RefreshCw className="h-3 w-3" />
                        <span>{t('New turns · Reload', '有新回复 · 刷新')}</span>
                      </button>
                    )}

                    <button
                      type="button"
                      aria-pressed={showProcess}
                      onClick={() =>
                        setProcessVisibleSessionId((prev) =>
                          prev === detail.id ? null : detail.id
                        )
                      }
                      title={t(
                        'Toggle hidden thinking traces, tool calls, and intermediate steps',
                        '切换是否默认展开全部思考过程、工具调用与中间步骤'
                      )}
                      className={`inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors ${
                        showProcess
                          ? 'border-indigo-500/50 bg-indigo-500/20 text-indigo-200 hover:bg-indigo-500/30'
                          : 'border-slate-700 bg-slate-800/80 text-slate-300 hover:border-indigo-500/50 hover:text-white'
                      }`}
                    >
                      <Brain className="h-3.5 w-3.5" />
                      <span>
                        {showProcess
                          ? t('Thinking & process: On', '思考与过程: 开')
                          : t('Show thinking & process', '显示思考与过程')}
                      </span>
                    </button>

                    <button
                      type="button"
                      onClick={() => onCreateHandoff(detail.id)}
                      className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-indigo-500/40 bg-indigo-500/15 px-3 py-1.5 text-xs font-semibold text-indigo-200 transition-colors hover:bg-indigo-500/25"
                    >
                      <ArrowRightLeft className="h-3.5 w-3.5" />
                      <span>{t('Create Handoff', '生成交接简报')}</span>
                    </button>

                    {onPackageSkill && (
                      <button
                        type="button"
                        onClick={() => onPackageSkill(detail.id)}
                        className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/15 px-3 py-1.5 text-xs font-semibold text-emerald-200 transition-colors hover:bg-emerald-500/25"
                      >
                        <PackagePlus className="h-3.5 w-3.5" />
                        <span>{t('Package Skill', '打包为 Skill')}</span>
                      </button>
                    )}
                  </div>
                </div>
              </div>

              <div ref={transcriptContainerRef} className="flex-1 space-y-3 overflow-y-auto p-4">
                {hasEarlierTurns && (
                  <div className="flex justify-center">
                    <button
                      type="button"
                      disabled={loadingMoreTurns}
                      onClick={() => void handleLoadEarlierTurns()}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-900 px-3.5 py-1.5 text-xs font-medium text-slate-200 hover:border-indigo-500/60 hover:text-white disabled:opacity-50"
                    >
                      <ChevronUp className="h-3.5 w-3.5" />
                      <span>
                        {loadingMoreTurns
                          ? t('Loading earlier turns...', '正在加载更早轮次...')
                          : isZh
                            ? `加载更早对话（前面还有 ${turnWindowStart} / 共 ${totalTurns}）`
                            : `Load earlier turns (${turnWindowStart} earlier of ${totalTurns})`}
                      </span>
                    </button>
                  </div>
                )}

                {transcriptItems.length === 0 && (detail.turn_count ?? 0) > 0 && !showProcess ? (
                  <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-950/60 px-4 py-3 text-xs text-slate-300">
                    <div className="flex items-center gap-2">
                      <Brain className="h-4 w-4 text-slate-500" />
                      <span>
                        {isZh
                          ? `该会话包含 ${detail.turn_count} 步内部思考与工具记录（默认精简模式下已隐藏）。`
                          : `This session contains ${detail.turn_count} internal thinking & tool steps (hidden in clean Q&A view).`}
                      </span>
                    </div>
                    <button
                      type="button"
                      onClick={() => setProcessVisibleSessionId(detail.id)}
                      className="rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1 text-[11px] font-medium text-slate-200 hover:border-indigo-500/50 hover:text-white"
                    >
                      <span>{t('Show thinking & process', '显示思考与过程')}</span>
                    </button>
                  </div>
                ) : (
                  transcriptItems.map((item) => {
                    if (item.kind === 'turn') {
                      const gapKey = item.precedingProcess?.gapRange
                        ? item.precedingProcess.key
                        : undefined;
                      return (
                        <TurnCard
                          key={item.turn.id}
                          turn={item.turn}
                          highlighted={highlightTurnIndex === item.turn.turn_index}
                          isTruncated={truncatedIndices.has(item.turn.turn_index)}
                          expanding={expandingTurnIndex === item.turn.turn_index}
                          onExpandFullTurn={handleExpandFullTurn}
                          showProcess={showProcess}
                          autoRevealProcess={item.autoRevealProcess}
                          precedingProcess={item.precedingProcess}
                          gapState={gapKey ? expandedGaps[gapKey] : undefined}
                          onToggleGap={(k, s, e) => void handleToggleGapGroup(k, s, e)}
                          onLoadMoreGap={(k, e) => void handleLoadMoreGapGroup(k, e)}
                          onExpandFullGapTurn={(idx) => void handleExpandFullTurn(idx)}
                        />
                      );
                    }
                    const proc = item.process;
                    if (proc.gapRange) {
                      const gapCount = proc.gapRange.endIndex - proc.gapRange.startIndex + 1;
                      const gapState = expandedGaps[proc.key];
                      const hasMoreInGroup =
                        Boolean(gapState) && gapState.loadedUpTo < proc.gapRange.endIndex;
                      const remainingCount = gapState
                        ? Math.min(GAP_FETCH_LIMIT, proc.gapRange.endIndex - gapState.loadedUpTo)
                        : 0;
                      return (
                        <ProcessGroupPill
                          key={proc.key}
                          count={gapCount}
                          subtitle="(thinking & tools)"
                          open={Boolean(gapState?.open)}
                          loading={Boolean(gapState?.loading)}
                          error={gapState?.error}
                          turns={gapState?.turns ?? []}
                          truncatedSet={new Set(gapState?.truncatedIndices ?? [])}
                          expandingTurnIndex={expandingTurnIndex}
                          hasMoreInGroup={hasMoreInGroup}
                          remainingCount={remainingCount}
                          onToggle={() =>
                            void handleToggleGapGroup(
                              proc.key,
                              proc.gapRange!.startIndex,
                              proc.gapRange!.endIndex
                            )
                          }
                          onLoadMore={() =>
                            void handleLoadMoreGapGroup(proc.key, proc.gapRange!.endIndex)
                          }
                          onExpandFullTurn={handleExpandFullTurn}
                        />
                      );
                    }
                    const thinkCnt = proc.localTurns.filter((t) => Boolean(t.thinking)).length;
                    const toolCnt = proc.localTurns.filter(
                      (t) => t.role === 'tool' || t.tool_calls.length > 0
                    ).length;
                    const parts: string[] = [];
                    if (thinkCnt > 0) parts.push(`${thinkCnt} thinking`);
                    if (toolCnt > 0) parts.push(`${toolCnt} tool`);
                    const subtitle =
                      parts.length > 0 ? `(${parts.join(', ')})` : '(thinking & tools)';
                    const isOpen = Boolean(expandedLocalGroups[proc.key]);
                    return (
                      <ProcessGroupPill
                        key={proc.key}
                        count={proc.localTurns.length}
                        subtitle={subtitle}
                        open={isOpen}
                        loading={false}
                        turns={proc.localTurns}
                        truncatedSet={truncatedIndices}
                        expandingTurnIndex={expandingTurnIndex}
                        onToggle={() =>
                          setExpandedLocalGroups((prev) => ({
                            ...prev,
                            [proc.key]: !prev[proc.key],
                          }))
                        }
                        onExpandFullTurn={handleExpandFullTurn}
                      />
                    );
                  })
                )}

                {hasLaterTurns && (
                  <div className="flex justify-center">
                    <button
                      type="button"
                      disabled={loadingMoreTurns}
                      onClick={() => void handleLoadNewerTurns()}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-900 px-3.5 py-1.5 text-xs font-medium text-slate-200 hover:border-indigo-500/60 hover:text-white disabled:opacity-50"
                    >
                      <ChevronDown className="h-3.5 w-3.5" />
                      <span>
                        {loadingMoreTurns
                          ? t('Loading newer turns...', '正在加载后续轮次...')
                          : t('Load newer turns', '加载后续对话')}
                      </span>
                    </button>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
