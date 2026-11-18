import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ArrowRight,
  Download,
  FileText,
  Layers,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { apiClient } from '../api/client';
import { AGENT_META, TARGET_AGENTS } from '../api/mockData';
import type { AgentId, HandoffBrief, NormalizedSession } from '../api/types';
import { AgentBadge } from '../components/AgentBadge';
import { CopyButton } from '../components/CopyButton';
import { MarkdownContent } from '../components/MarkdownContent';
import { formatRelativeTime, shortenWorkspace } from '../utils/format';
import { useLocale } from '../utils/i18n';

interface HandoffWorkbenchProps {
  initialSessionId: string | null;
  refreshSignal: number;
}

export function HandoffWorkbench({
  initialSessionId,
  refreshSignal,
}: HandoffWorkbenchProps) {
  const { locale, t } = useLocale();
  const [sessions, setSessions] = useState<NormalizedSession[]>([]);
  const [sessionFilter, setSessionFilter] = useState('');
  const [selectedSessionId, setSelectedSessionId] = useState<string>('');
  const [targetAgent, setTargetAgent] = useState<AgentId>('claude_code');
  const [includeMemories, setIncludeMemories] = useState(true);
  const [includeToolSummary, setIncludeToolSummary] = useState(true);
  const [maxTokens, setMaxTokens] = useState(1200);
  const [customInstructions, setCustomInstructions] = useState('');

  const [brief, setBrief] = useState<HandoffBrief | null>(null);
  const [previewTab, setPreviewTab] = useState<'rendered' | 'raw'>('rendered');
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiClient
      .listSessions({ limit: 100 })
      .then((res) => {
        setSessions(res.sessions);
        setSelectedSessionId((prev) => {
          if (initialSessionId && res.sessions.some((s) => s.id === initialSessionId)) {
            return initialSessionId;
          }
          if (prev && res.sessions.some((s) => s.id === prev)) {
            return prev;
          }
          return res.sessions[0]?.id ?? '';
        });
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : 'Failed to load sessions');
      });
  }, [initialSessionId, refreshSignal]);

  useEffect(() => {
    if (initialSessionId) {
      setSelectedSessionId(initialSessionId);
    }
  }, [initialSessionId]);

  const selectedSession = useMemo(
    () => sessions.find((s) => s.id === selectedSessionId) ?? null,
    [sessions, selectedSessionId]
  );

  // Suggest a different target agent when switching to a new source session
  useEffect(() => {
    if (!selectedSession) return;
    setTargetAgent((prev) => {
      if (prev !== selectedSession.agent) return prev;
      return TARGET_AGENTS.find((a) => a !== selectedSession.agent) ?? 'claude_code';
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedSession?.id]);

  const filteredSessions = useMemo(() => {
    const q = sessionFilter.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter((s) =>
      `${s.title} ${s.agent} ${s.workspace ?? ''}`.toLowerCase().includes(q)
    );
  }, [sessions, sessionFilter]);

  const handleGenerate = useCallback(async () => {
    if (!selectedSessionId) return;
    setGenerating(true);
    setError(null);
    try {
      const res = await apiClient.generateHandoff({
        session_id: selectedSessionId,
        target_agent: targetAgent,
        include_memories: includeMemories,
        include_tool_summary: includeToolSummary,
        max_tokens: maxTokens,
        custom_instructions: customInstructions.trim() || undefined,
      });
      setBrief(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to generate handoff brief');
    } finally {
      setGenerating(false);
    }
  }, [
    selectedSessionId,
    targetAgent,
    includeMemories,
    includeToolSummary,
    maxTokens,
    customInstructions,
  ]);

  // Auto-generate when session or target changes if we already have sessions loaded
  useEffect(() => {
    if (selectedSessionId) {
      void handleGenerate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedSessionId, targetAgent]);

  const handleDownload = () => {
    if (!brief) return;
    const blob = new Blob([brief.markdown], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `handoff-${brief.source_agent}-to-${brief.target_agent}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="grid h-full grid-cols-1 gap-4 lg:grid-cols-12">
      {/* Left Configuration Column */}
      <div className="flex flex-col gap-4 overflow-y-auto rounded-xl border border-slate-800 bg-slate-900/60 p-4 lg:col-span-5">
        <div>
          <h2 className="text-sm font-semibold text-white">
            <span>{t('1. Source Session', '1. 选择源会话')}</span>
          </h2>
          <p className="mt-0.5 text-xs text-slate-400">
            <span>
              {t(
                'Select any indexed session across agents to transfer context cleanly.',
                '从任意 Agent 历史会话中挑选一条，提取上下文交给下一个 Agent 接手。'
              )}
            </span>
          </p>
          <input
            type="text"
            value={sessionFilter}
            onChange={(e) => setSessionFilter(e.target.value)}
            placeholder={t(
              'Filter sessions by title, agent, workspace...',
              '按标题、Agent 或工作区筛选会话...'
            )}
            className="mt-2.5 w-full rounded-lg border border-slate-800 bg-slate-950 px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:border-indigo-500 focus:outline-none"
          />
          <div className="mt-2 max-h-56 divide-y divide-slate-800/70 overflow-y-auto rounded-lg border border-slate-800 bg-slate-950/60">
            {filteredSessions.map((s) => {
              const active = s.id === selectedSessionId;
              return (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => setSelectedSessionId(s.id)}
                  className={`w-full p-2.5 text-left transition-colors hover:bg-slate-800/40 ${
                    active ? 'bg-indigo-500/15 ring-1 ring-inset ring-indigo-500/40' : ''
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <AgentBadge agent={s.agent} compact />
                    <span className="font-mono text-[10px] text-slate-500">
                      {s.turn_count}t · {formatRelativeTime(s.updated_at)}
                    </span>
                  </div>
                  <div className="mt-1 line-clamp-1 text-xs font-medium text-slate-100">
                    {s.title}
                  </div>
                  <div className="mt-0.5 truncate font-mono text-[10px] text-slate-500">
                    {shortenWorkspace(s.workspace)}
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        <div>
          <h2 className="text-sm font-semibold text-white">
            <span>{t('2. Target Agent', '2. 选择目标 Agent')}</span>
          </h2>
          <p className="mt-0.5 text-xs text-slate-400">
            <span>
              {t(
                'Tailor formatting, constraints, and instructions for the receiving coding agent.',
                '针对接手工作的编程 Agent 自动定制交接格式、约束与说明。'
              )}
            </span>
          </p>
          <div className="mt-2.5 grid grid-cols-2 gap-1.5 sm:grid-cols-3">
            {TARGET_AGENTS.map((agent) => {
              const meta = AGENT_META[agent];
              const selected = targetAgent === agent;
              return (
                <button
                  key={agent}
                  type="button"
                  onClick={() => setTargetAgent(agent)}
                  className={`flex items-center gap-2 rounded-lg border px-2.5 py-2 text-left text-xs transition-colors ${
                    selected
                      ? 'border-indigo-500 bg-indigo-500/15 text-white'
                      : 'border-slate-800 bg-slate-950/70 text-slate-400 hover:border-slate-700 hover:text-slate-200'
                  }`}
                >
                  <span className={`h-2 w-2 rounded-full ${meta?.dotClass ?? 'bg-slate-400'}`} />
                  <span translate="no" className="truncate font-medium">
                    {meta?.label ?? agent}
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        <div className="space-y-3 border-t border-slate-800 pt-3">
          <h2 className="text-sm font-semibold text-white">
            <span>{t('3. Brief Options', '3. 简报选项')}</span>
          </h2>

          <div className="flex flex-wrap gap-4 text-xs text-slate-300">
            <label className="inline-flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={includeMemories}
                onChange={(e) => setIncludeMemories(e.target.checked)}
                className="rounded border-slate-700 bg-slate-950 text-indigo-500"
              />
              <span>{t('Include central memories', '附带关联中央记忆')}</span>
            </label>
            <label className="inline-flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={includeToolSummary}
                onChange={(e) => setIncludeToolSummary(e.target.checked)}
                className="rounded border-slate-700 bg-slate-950 text-indigo-500"
              />
              <span>{t('Include tool summary', '附带工具操作摘要')}</span>
            </label>
          </div>

          <div>
            <div className="mb-1 flex items-center justify-between text-xs text-slate-400">
              <span>{t('Target Token Budget', '目标 Token 预算')}</span>
              <span translate="no" className="font-mono text-slate-200">
                {`${maxTokens} tok`}
              </span>
            </div>
            <input
              type="range"
              min={400}
              max={3000}
              step={100}
              value={maxTokens}
              onChange={(e) => setMaxTokens(Number(e.target.value))}
              className="w-full accent-indigo-500"
            />
          </div>

          <div>
            <label className="mb-1 block text-xs text-slate-400">
              <span>{t('Custom Handoff Instructions (optional)', '补充交接说明（可选）')}</span>
            </label>
            <textarea
              rows={2}
              value={customInstructions}
              onChange={(e) => setCustomInstructions(e.target.value)}
              placeholder={t(
                'e.g. Focus on completing backend endpoints and running pytest...',
                '例如：请重点完成剩余后端接口并运行 pytest 验证...'
              )}
              className="w-full rounded-lg border border-slate-800 bg-slate-950 p-2.5 text-xs text-slate-200 placeholder-slate-600 focus:border-indigo-500 focus:outline-none"
            />
          </div>

          <button
            type="button"
            onClick={handleGenerate}
            disabled={!selectedSessionId || generating}
            className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-xs font-semibold text-white transition-colors hover:bg-indigo-500 disabled:opacity-50"
          >
            <Sparkles className="h-3.5 w-3.5" />
            <span>
              {generating
                ? t('Generating Brief...', '正在生成交接简报...')
                : t('Generate Handoff Brief', '生成跨 Agent 交接简报')}
            </span>
          </button>
        </div>
      </div>

      {/* Right Preview Column */}
      <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40 lg:col-span-7">
        {error && (
          <div className="m-4 rounded-lg border border-rose-500/40 bg-rose-950/30 px-4 py-2 text-xs text-rose-300">
            {error}
          </div>
        )}

        {!brief ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 p-10 text-center">
            <Layers className="h-8 w-8 text-slate-600" />
            <p className="text-sm font-medium text-slate-300">No handoff generated yet</p>
            <p className="max-w-sm text-xs text-slate-500">
              Pick a session and target agent on the left to build a redacted, ready-to-paste cross-agent brief.
            </p>
          </div>
        ) : (
          <>
            <div className="border-b border-slate-800 bg-slate-900/70 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <AgentBadge agent={brief.source_agent} />
                  <ArrowRight className="h-3.5 w-3.5 text-slate-500" />
                  <AgentBadge agent={brief.target_agent} />
                  <span className="rounded border border-slate-700 bg-slate-800/80 px-2 py-0.5 font-mono text-[11px] tabular-nums text-slate-300">
                    ~{brief.token_estimate} tokens
                  </span>
                  {brief.redacted && (
                    <span className="inline-flex items-center gap-1 rounded border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-300">
                      <ShieldCheck className="h-3 w-3" />
                      <span>{t('Secrets Redacted', '已脱敏密钥')}</span>
                    </span>
                  )}
                </div>

                <div className="flex items-center gap-2">
                  <div className="inline-flex rounded-lg border border-slate-800 bg-slate-950 p-0.5">
                    <button
                      type="button"
                      onClick={() => setPreviewTab('rendered')}
                      className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                        previewTab === 'rendered'
                          ? 'bg-slate-800 text-white'
                          : 'text-slate-400 hover:text-slate-200'
                      }`}
                    >
                      <span>{t('Preview', '渲染预览')}</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => setPreviewTab('raw')}
                      className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                        previewTab === 'raw'
                          ? 'bg-slate-800 text-white'
                          : 'text-slate-400 hover:text-slate-200'
                      }`}
                    >
                      <span>{t('Raw Markdown', 'Markdown 原文')}</span>
                    </button>
                  </div>

                  <button
                    type="button"
                    onClick={handleDownload}
                    title={t('Download .md', '导出 .md')}
                    className="inline-flex items-center gap-1 rounded-lg border border-slate-700/80 bg-slate-800/80 px-2.5 py-1.5 text-xs text-slate-300 hover:text-white"
                  >
                    <Download className="h-3.5 w-3.5" />
                    <span translate="no">.md</span>
                  </button>

                  <CopyButton
                    key={`copy-${locale}`}
                    text={brief.markdown}
                    label={t('Copy Brief', '复制简报')}
                    copiedLabel={t('Brief Copied!', '已复制！')}
                    size="sm"
                  />
                </div>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto p-5">
              {previewTab === 'rendered' ? (
                <div className="space-y-4">
                  <div className="rounded-xl border border-slate-800 bg-slate-950/60 p-4">
                    <MarkdownContent content={brief.markdown} />
                  </div>

                  {brief.sections.relevant_memories.length > 0 && (
                    <div className="rounded-xl border border-slate-800/80 bg-slate-900/50 p-3.5">
                      <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                        <FileText className="h-3.5 w-3.5 text-indigo-400" />
                        <span>Injected Central Memories ({brief.sections.relevant_memories.length})</span>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {brief.sections.relevant_memories.map((m) => (
                          <span
                            key={m.id}
                            className="rounded-md border border-slate-800 bg-slate-950 px-2.5 py-1 text-xs text-slate-300"
                          >
                            <span className="mr-1.5 font-mono text-[10px] uppercase text-indigo-400">
                              {m.category}
                            </span>
                            {m.title}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <pre className="rounded-xl border border-slate-800 bg-slate-950 p-4 font-mono text-xs leading-relaxed text-slate-200 whitespace-pre-wrap">
                  {brief.markdown}
                </pre>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
