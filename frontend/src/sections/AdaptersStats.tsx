import { useCallback, useEffect, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Cpu,
  Database,
  FolderSearch,
  HardDrive,
  Play,
  Radio,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  XCircle,
} from 'lucide-react';
import { apiClient } from '../api/client';
import type {
  AdapterHealth,
  DistillerProvider,
  DistillerSettings,
  StatsResponse,
} from '../api/types';
import { AgentBadge } from '../components/AgentBadge';
import { formatBytes, formatRelativeTime } from '../utils/format';
import { useLocale } from '../utils/i18n';

interface AdaptersStatsProps {
  refreshSignal: number;
  onDataChanged: () => void;
}

function StatusPill({ status }: { status: AdapterHealth['status'] }) {
  switch (status) {
    case 'healthy':
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-500/15 px-2 py-0.5 text-[11px] font-medium text-emerald-300">
          <CheckCircle2 className="h-3 w-3" />
          Healthy
        </span>
      );
    case 'degraded':
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-[11px] font-medium text-amber-300">
          <AlertTriangle className="h-3 w-3" />
          Degraded
        </span>
      );
    case 'permission_denied':
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-rose-500/40 bg-rose-500/15 px-2 py-0.5 text-[11px] font-medium text-rose-300">
          <ShieldAlert className="h-3 w-3" />
          Permission Denied
        </span>
      );
    default:
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-slate-700 bg-slate-800/80 px-2 py-0.5 text-[11px] font-medium text-slate-400">
          <XCircle className="h-3 w-3" />
          Not Found
        </span>
      );
  }
}

export function AdaptersStats({ refreshSignal, onDataChanged }: AdaptersStatsProps) {
  const { locale, t } = useLocale();
  const [adapters, setAdapters] = useState<AdapterHealth[]>([]);
  const [watcherRunning, setWatcherRunning] = useState(false);
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [distiller, setDistiller] = useState<DistillerSettings | null>(null);

  // Distiller edit form state
  const [provider, setProvider] = useState<DistillerProvider>('heuristic');
  const [model, setModel] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [autoDistill, setAutoDistill] = useState(true);

  const [scanning, setScanning] = useState(false);
  const [fullRescan, setFullRescan] = useState(false);
  const [backfilling, setBackfilling] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [notice, setNotice] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const loadAll = useCallback(async () => {
    try {
      const [adaptersRes, statsRes, distillerRes] = await Promise.all([
        apiClient.listAdapters(),
        apiClient.getStats(),
        apiClient.getDistillerSettings(),
      ]);
      setAdapters(adaptersRes.adapters);
      setWatcherRunning(adaptersRes.watcher_running);
      setStats(statsRes);
      setDistiller(distillerRes);
      setProvider(distillerRes.provider);
      setModel(distillerRes.model ?? '');
      setBaseUrl(distillerRes.base_url ?? '');
      setAutoDistill(distillerRes.auto_distill_on_scan);
    } catch (err) {
      setNotice({
        kind: 'err',
        text: err instanceof Error ? err.message : 'Failed to load adapter stats',
      });
    }
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll, refreshSignal]);

  const handleScan = async () => {
    setScanning(true);
    setNotice(null);
    try {
      const res = await apiClient.triggerScan({ full_rescan: fullRescan, distill: true });
      setNotice({
        kind: 'ok',
        text: `Scan complete in ${res.duration_ms}ms · ${res.scanned_files} files scanned · ${res.new_sessions} new, ${res.updated_sessions} updated sessions.`,
      });
      await loadAll();
      onDataChanged();
    } catch (err) {
      setNotice({ kind: 'err', text: err instanceof Error ? err.message : 'Scan failed' });
    } finally {
      setScanning(false);
    }
  };

  const handleSaveDistiller = async (e: React.FormEvent) => {
    e.preventDefault();
    setSavingSettings(true);
    setNotice(null);
    try {
      const updated = await apiClient.updateDistillerSettings({
        provider,
        model: provider === 'heuristic' ? null : model.trim() || null,
        base_url: provider === 'heuristic' ? null : baseUrl.trim() || null,
        api_key: apiKey.trim() ? apiKey.trim() : undefined,
        auto_distill_on_scan: autoDistill,
      });
      setDistiller(updated);
      setApiKey('');
      setNotice({
        kind: 'ok',
        text: `Distiller updated to "${updated.provider}" (${
          updated.zero_cost_mode ? 'Zero-cost local mode' : 'External LLM API'
        }).`,
      });
      await loadAll();
    } catch (err) {
      setNotice({
        kind: 'err',
        text: err instanceof Error ? err.message : 'Failed to update distiller settings',
      });
    } finally {
      setSavingSettings(false);
    }
  };

  const handleBackfill = async () => {
    setBackfilling(true);
    setNotice(null);
    try {
      const res = await apiClient.runDistillBackfill({ limit: 25 });
      setNotice({
        kind: 'ok',
        text: `Backfill (${res.provider}): processed ${res.processed_sessions} session(s), created ${res.created_memories} memory card(s).`,
      });
      await loadAll();
      onDataChanged();
    } catch (err) {
      setNotice({
        kind: 'err',
        text: err instanceof Error ? err.message : 'Backfill failed',
      });
    } finally {
      setBackfilling(false);
    }
  };

  const maxTurns = Math.max(1, ...(stats?.by_agent.map((a) => a.turns) ?? [1]));

  return (
    <div className="space-y-5 overflow-y-auto pr-1">
      {notice && (
        <div
          className={`rounded-lg border px-4 py-2.5 text-xs ${
            notice.kind === 'ok'
              ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-300'
              : 'border-rose-500/40 bg-rose-950/30 text-rose-300'
          }`}
        >
          {notice.text}
        </div>
      )}

      {/* Top KPI Cards */}
      {stats && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
            <div className="flex items-center justify-between text-xs text-slate-400">
              <span>Indexed Sessions</span>
              <Activity className="h-4 w-4 text-indigo-400" />
            </div>
            <div className="mt-2 font-mono text-2xl font-semibold tabular-nums text-white">
              {stats.total_sessions}
            </div>
            <div className="mt-1 text-[11px] text-slate-500">
              Across {stats.by_agent.length} active agents
            </div>
          </div>

          <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
            <div className="flex items-center justify-between text-xs text-slate-400">
              <span>Normalized Turns</span>
              <FolderSearch className="h-4 w-4 text-sky-400" />
            </div>
            <div className="mt-2 font-mono text-2xl font-semibold tabular-nums text-white">
              {stats.total_turns}
            </div>
            <div className="mt-1 text-[11px] text-slate-500">
              {stats.index_embeddings_count} ONNX vectors
            </div>
          </div>

          <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
            <div className="flex items-center justify-between text-xs text-slate-400">
              <span>Central Memories</span>
              <Sparkles className="h-4 w-4 text-amber-400" />
            </div>
            <div className="mt-2 font-mono text-2xl font-semibold tabular-nums text-white">
              {stats.total_memories}
            </div>
            <div className="mt-1 text-[11px] text-slate-500">
              {stats.total_native_memories} native imported
            </div>
          </div>

          <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
            <div className="flex items-center justify-between text-xs text-slate-400">
              <span>SQLite Index Size</span>
              <Database className="h-4 w-4 text-emerald-400" />
            </div>
            <div className="mt-2 font-mono text-2xl font-semibold tabular-nums text-white">
              {formatBytes(stats.db_size_bytes)}
            </div>
            <div className="mt-1 text-[11px] text-slate-500">WAL + FTS5 + float32 BLOB</div>
          </div>

          <div className="col-span-2 rounded-xl border border-emerald-500/30 bg-emerald-950/20 p-4 lg:col-span-1">
            <div className="flex items-center justify-between text-xs text-emerald-300">
              <span>Distiller Mode</span>
              <Cpu className="h-4 w-4 text-emerald-400" />
            </div>
            <div className="mt-2 font-mono text-lg font-semibold text-white">
              {distiller?.provider ?? 'heuristic'}
            </div>
            <div className="mt-1 text-[11px] text-emerald-300/80">
              {distiller?.zero_cost_mode ? '$0.00 · Zero-cost local' : 'Remote LLM active'}
            </div>
          </div>
        </div>
      )}

      {/* Middle Row: Per-Agent Breakdown + Distiller Mode & Switch Guide */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        {/* Session & Turn Counts per Agent */}
        <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4 lg:col-span-6">
          <div className="mb-3 flex items-center justify-between">
            <div>
              <h2 className="text-sm font-semibold text-white">
                <span>{t('Session & Turn Counts per Agent', '各 Agent 会话与轮次统计')}</span>
              </h2>
              <p className="text-xs text-slate-400">
                <span>
                  {t(
                    'Indexed conversations and turn volume across detected coding assistants.',
                    '已索引的本地各编码助手历史对话与轮次汇总。'
                  )}
                </span>
              </p>
            </div>
          </div>

          <div className="space-y-3">
            {(stats?.by_agent ?? []).map((item) => {
              const pct = Math.max(4, Math.round((item.turns / maxTurns) * 100));
              return (
                <div key={item.agent} className="space-y-1">
                  <div className="flex items-center justify-between text-xs">
                    <div className="flex items-center gap-2">
                      <AgentBadge agent={item.agent} />
                      {item.native_memories > 0 && (
                        <span className="font-mono text-[10px] text-cyan-400">
                          +{item.native_memories} native mem
                        </span>
                      )}
                    </div>
                    <span className="font-mono text-xs tabular-nums text-slate-300">
                      {item.sessions} sessions · {item.turns} turns
                    </span>
                  </div>
                  <div className="h-2 w-full overflow-hidden rounded-full bg-slate-950">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-sky-400 transition-all"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Distiller Mode Indicator & Switch Instructions */}
        <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4 lg:col-span-6">
          <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-semibold text-white">
                  <span>{t('Memory Distiller Configuration', '记忆蒸馏器配置')}</span>
                </h2>
                {distiller?.zero_cost_mode && (
                  <span className="rounded-full border border-emerald-500/40 bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-300">
                    <span>{t('Zero-Cost Mode', '零费用模式')}</span>
                  </span>
                )}
              </div>
              <p className="mt-0.5 text-xs text-slate-400">
                <span>
                  {t(
                    'Starts in free deterministic heuristic mode. Switch anytime to local Ollama or any OpenAI-compatible API.',
                    '默认启用零费用启发式规则提取，可随时切换本地 Ollama 或 OpenAI 兼容模型。'
                  )}
                </span>
              </p>
            </div>

            <button
              type="button"
              onClick={handleBackfill}
              disabled={backfilling}
              className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-500/40 bg-indigo-500/15 px-3 py-1.5 text-xs font-semibold text-indigo-200 hover:bg-indigo-500/25 disabled:opacity-50"
            >
              <Play className="h-3 w-3" />
              <span key={`backfill-${locale}`}>
                {backfilling
                  ? t('Running Backfill...', '正在回填...')
                  : `${t('Run Backfill', '立即回填')} (${distiller?.pending_sessions_count ?? 0} ${t('pending', '待处理')})`}
              </span>
            </button>
          </div>

          <form onSubmit={handleSaveDistiller} className="space-y-3">
            <div className="grid grid-cols-3 gap-2">
              {(
                [
                  { id: 'heuristic', title: 'Heuristic', sub: '$0 · Zero LLM' },
                  { id: 'ollama', title: 'Ollama', sub: '$0 · Local LLM' },
                  { id: 'openai_compatible', title: 'OpenAI API', sub: 'Custom endpoint' },
                ] as const
              ).map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => {
                    setProvider(opt.id);
                    if (opt.id === 'ollama' && !baseUrl) {
                      setBaseUrl('http://127.0.0.1:11434/v1');
                      if (!model) setModel('qwen2.5:7b-instruct');
                    }
                  }}
                  className={`rounded-lg border p-2.5 text-left transition-colors ${
                    provider === opt.id
                      ? 'border-indigo-500 bg-indigo-500/15 text-white'
                      : 'border-slate-800 bg-slate-950/70 text-slate-400 hover:border-slate-700'
                  }`}
                >
                  <div className="text-xs font-semibold">{opt.title}</div>
                  <div className="mt-0.5 text-[11px] text-slate-400">{opt.sub}</div>
                </button>
              ))}
            </div>

            {provider !== 'heuristic' && (
              <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
                <div>
                  <label className="mb-1 block text-[11px] text-slate-400">Base URL</label>
                  <input
                    type="text"
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                    placeholder="http://127.0.0.1:11434/v1"
                    className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 font-mono text-xs text-slate-200 focus:border-indigo-500 focus:outline-none"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[11px] text-slate-400">Model</label>
                  <input
                    type="text"
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                    placeholder={provider === 'ollama' ? 'qwen2.5:7b-instruct' : 'gpt-4o-mini'}
                    className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 font-mono text-xs text-slate-200 focus:border-indigo-500 focus:outline-none"
                  />
                </div>
                {provider === 'openai_compatible' && (
                  <div className="sm:col-span-2">
                    <label className="mb-1 block text-[11px] text-slate-400">
                      API Key (write-only, never echoed back){' '}
                      {distiller?.api_key_configured && (
                        <span className="text-emerald-400">· Key configured</span>
                      )}
                    </label>
                    <input
                      type="password"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      placeholder="sk-..."
                      className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 font-mono text-xs text-slate-200 focus:border-indigo-500 focus:outline-none"
                    />
                  </div>
                )}
              </div>
            )}

            <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
              <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-slate-300">
                <input
                  type="checkbox"
                  checked={autoDistill}
                  onChange={(e) => setAutoDistill(e.target.checked)}
                  className="rounded border-slate-700 bg-slate-950 text-indigo-500"
                />
                <span>Auto-distill new turns on scan</span>
              </label>

              <button
                type="submit"
                disabled={savingSettings}
                className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-50"
              >
                <span>
                  {savingSettings
                    ? t('Saving...', '保存中...')
                    : t('Save Distiller Config', '保存蒸馏器配置')}
                </span>
              </button>
            </div>

            <div className="rounded-lg border border-slate-800/80 bg-slate-950/70 p-2.5 font-mono text-[11px] text-slate-400">
              <span className="font-semibold text-slate-300">How to switch later via omnimex.toml:</span>{' '}
              set <code>[distiller] provider = "ollama"</code>, <code>model = "qwen2.5:7b-instruct"</code>, then click <strong>Run Backfill</strong> to upgrade heuristic drafts.
            </div>
          </form>
        </div>
      </div>

      {/* Adapter Discovery Health Cards */}
      <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div>
              <h2 className="text-sm font-semibold text-white">
                <span>{t('Adapter Discovery Health', '适配器发现与健康状态')}</span>
              </h2>
              <p className="text-xs text-slate-400">
                <span>
                  {t(
                    'Local transcript & native memory adapters with Windows-safe incremental cursors.',
                    '支持 Windows 文件锁安全拷贝与增量游标的多 Agent 本地日志/原生记忆适配器。'
                  )}
                </span>
              </p>
            </div>
            <span
              key={`watcher-${locale}`}
              className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${
                watcherRunning
                  ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
                  : 'border-slate-700 bg-slate-800 text-slate-400'
              }`}
            >
              <Radio className="h-3 w-3" />
              <span>
                {`${t('Watcher', '实时监听')} ${
                  watcherRunning ? t('Active', '运行中') : t('Idle', '空闲')
                }`}
              </span>
            </span>
          </div>

          <div className="flex items-center gap-3">
            <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-slate-300">
              <input
                type="checkbox"
                checked={fullRescan}
                onChange={(e) => setFullRescan(e.target.checked)}
                className="rounded border-slate-700 bg-slate-950 text-indigo-500"
              />
              <span>{t('Full rescan', '全量重扫')}</span>
            </label>

            <button
              type="button"
              onClick={handleScan}
              disabled={scanning}
              className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${scanning ? 'animate-spin' : ''}`} />
              <span>{scanning ? t('Scanning...', '扫描中...') : t('Scan Now', '立即扫描')}</span>
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {adapters.map((adapter) => (
            <div
              key={adapter.id}
              className="flex flex-col justify-between rounded-xl border border-slate-800 bg-slate-950/60 p-3.5"
            >
              <div>
                <div className="flex items-center justify-between gap-2">
                  <AgentBadge agent={adapter.id} />
                  <StatusPill status={adapter.status} />
                </div>

                <div className="mt-3 grid grid-cols-3 gap-2 rounded-lg border border-slate-800/80 bg-slate-900/50 p-2 text-center">
                  <div>
                    <div className="font-mono text-sm font-semibold tabular-nums text-white">
                      {adapter.discovered_files}
                    </div>
                    <div className="text-[10px] text-slate-500">Files</div>
                  </div>
                  <div>
                    <div className="font-mono text-sm font-semibold tabular-nums text-white">
                      {adapter.session_count}
                    </div>
                    <div className="text-[10px] text-slate-500">Sessions</div>
                  </div>
                  <div>
                    <div className="font-mono text-sm font-semibold tabular-nums text-white">
                      {adapter.turn_count}
                    </div>
                    <div className="text-[10px] text-slate-500">Turns</div>
                  </div>
                </div>

                <div className="mt-2.5 space-y-1">
                  {adapter.source_paths.map((p) => (
                    <div
                      key={p}
                      title={p}
                      className="flex items-center gap-1.5 truncate font-mono text-[10px] text-slate-400"
                    >
                      <HardDrive className="h-3 w-3 shrink-0 text-slate-600" />
                      <span className="truncate">{p}</span>
                    </div>
                  ))}
                </div>

                {adapter.last_error && (
                  <div className="mt-2.5 rounded-lg border border-amber-500/30 bg-amber-950/30 p-2 text-[11px] text-amber-200">
                    {adapter.last_error}
                  </div>
                )}
              </div>

              <div className="mt-3 flex items-center justify-between border-t border-slate-800/70 pt-2 text-[11px] text-slate-500">
                <span>
                  {adapter.supports_native_memories
                    ? `${adapter.native_memory_count} native mem`
                    : 'Transcripts only'}
                </span>
                <span>Scanned {formatRelativeTime(adapter.last_scan_at)}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
