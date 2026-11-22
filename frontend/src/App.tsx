import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  ArrowRightLeft,
  BookOpen,
  Database,
  Languages,
  Radio,
  RefreshCw,
  Search,
  WifiOff,
} from 'lucide-react';
import { apiClient, type ConnectionMode, type ConnectionState } from './api/client';
import type { OmnimexEvent } from './api/types';
import { ErrorBoundary } from './components/ErrorBoundary';
import { AdaptersStats } from './sections/AdaptersStats';
import { HandoffWorkbench } from './sections/HandoffWorkbench';
import { MemoryHub } from './sections/MemoryHub';
import { SearchExplorer } from './sections/SearchExplorer';
import { LocaleProvider, useLocale } from './utils/i18n';

type SectionId = 'search' | 'memories' | 'handoff' | 'adapters';

const NAV_ITEMS: Array<{ id: SectionId; labelEn: string; labelZh: string; icon: typeof Search }> = [
  { id: 'search', labelEn: 'Search & Explorer', labelZh: '搜索与会话', icon: Search },
  { id: 'memories', labelEn: 'Memory Hub', labelZh: '记忆中心', icon: BookOpen },
  { id: 'handoff', labelEn: 'Handoff Workbench', labelZh: '跨 Agent 交接', icon: ArrowRightLeft },
  { id: 'adapters', labelEn: 'Adapters & Stats', labelZh: '适配器与统计', icon: Activity },
];

function AppShell() {
  const { locale, isZh, toggleLocale, t } = useLocale();
  const [activeTab, setActiveTab] = useState<SectionId>('search');
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [highlightTurnIndex, setHighlightTurnIndex] = useState<number | null>(null);
  const [handoffSessionId, setHandoffSessionId] = useState<string | null>(null);
  const [refreshSignal, setRefreshSignal] = useState(0);
  const [listRefreshSignal, setListRefreshSignal] = useState(0);
  const [pendingUpdateSessionId, setPendingUpdateSessionId] = useState<string | null>(null);
  const selectedSessionIdRef = useRef(selectedSessionId);
  selectedSessionIdRef.current = selectedSessionId;
  const sseDebounceTimerRef = useRef<number | null>(null);

  const [conn, setConn] = useState<ConnectionState>(() => apiClient.getConnectionState());
  const [initialProbeDone, setInitialProbeDone] = useState(
    () => apiClient.getConnectionState().mode === 'demo'
  );
  const [lastEvent, setLastEvent] = useState<OmnimexEvent | null>(null);

  useEffect(() => {
    let prevUsingMock = apiClient.getConnectionState().usingMock;
    const unsub = apiClient.subscribeConnection((next) => {
      setConn(next);
      if (next.usingMock !== prevUsingMock) {
        prevUsingMock = next.usingMock;
        setRefreshSignal((s) => s + 1);
      }
    });
    void apiClient
      .probeHealth()
      .catch(() => {
        // handled in connection state
      })
      .finally(() => {
        setInitialProbeDone(true);
      });
    return unsub;
  }, []);

  useEffect(() => {
    if (conn.usingMock || !conn.backendReachable) return;
    const scheduleListRefresh = () => {
      if (sseDebounceTimerRef.current !== null) return;
      sseDebounceTimerRef.current = window.setTimeout(() => {
        sseDebounceTimerRef.current = null;
        setListRefreshSignal((s) => s + 1);
      }, 3000);
    };

    const unsub = apiClient.subscribeEvents((evt) => {
      setLastEvent(evt);
      if (evt.type === 'session.ingested') {
        const ingestedId =
          typeof evt.data.session_id === 'string' ? evt.data.session_id : null;
        if (ingestedId && ingestedId === selectedSessionIdRef.current) {
          setPendingUpdateSessionId(ingestedId);
        }
        scheduleListRefresh();
        return;
      }
      if (
        evt.type === 'scan.completed' ||
        evt.type === 'memory.synced' ||
        evt.type === 'distill.completed' ||
        evt.type === 'embedding.completed'
      ) {
        scheduleListRefresh();
      }
    });
    return () => {
      unsub();
      if (sseDebounceTimerRef.current !== null) {
        window.clearTimeout(sseDebounceTimerRef.current);
        sseDebounceTimerRef.current = null;
      }
    };
  }, [conn.usingMock, conn.backendReachable]);

  const handleSelectSession = useCallback(
    (sessionId: string, turnIndex: number | null = null) => {
      setSelectedSessionId(sessionId);
      setHighlightTurnIndex(turnIndex);
      setPendingUpdateSessionId((prev) => (prev === sessionId ? prev : null));
    },
    []
  );

  const handleOpenSessionFromMemory = useCallback(
    (sessionId: string, turnIndex: number | null = null) => {
      setSelectedSessionId(sessionId);
      setHighlightTurnIndex(turnIndex);
      setActiveTab('search');
    },
    []
  );

  const handleCreateHandoff = useCallback((sessionId: string) => {
    setHandoffSessionId(sessionId);
    setActiveTab('handoff');
  }, []);

  const handleModeChange = (mode: ConnectionMode) => {
    apiClient.setMode(mode);
    if (mode !== 'demo') {
      void apiClient.probeHealth().catch(() => {
        // state updated automatically
      });
    }
    setRefreshSignal((s) => s + 1);
  };

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-slate-950 text-slate-100">
      {/* Top Header */}
      <header className="shrink-0 border-b border-slate-800/90 bg-slate-900/80 px-4 py-2.5 backdrop-blur-md lg:px-6">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-3">
          {/* Brand + Nav */}
          <div className="flex flex-wrap items-center gap-6">
            <div className="flex items-center gap-2.5">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-indigo-500/40 bg-indigo-500/15 text-indigo-300 shadow-inner">
                <Database className="h-4 w-4" />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span translate="no" className="text-sm font-bold tracking-tight text-white">
                    Omnimex
                  </span>
                  <span
                    translate="no"
                    className="rounded border border-slate-700 bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-300"
                  >
                    LOCAL
                  </span>
                </div>
                <p className="text-[11px] text-slate-400">
                  <span>{t('Cross-Agent Memory & Session Hub', '跨 Agent 记忆与会话中心')}</span>
                </p>
              </div>
            </div>

            <nav className="flex flex-wrap items-center gap-1 rounded-xl border border-slate-800 bg-slate-950/80 p-1">
              {NAV_ITEMS.map((item) => {
                const Icon = item.icon;
                const active = activeTab === item.id;
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setActiveTab(item.id)}
                    className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-all ${
                      active
                        ? 'bg-indigo-600 text-white shadow-sm'
                        : 'text-slate-400 hover:bg-slate-900 hover:text-slate-200'
                    }`}
                  >
                    <Icon className="h-3.5 w-3.5" />
                    <span>{isZh ? item.labelZh : item.labelEn}</span>
                  </button>
                );
              })}
            </nav>
          </div>

          {/* Right Status + Demo/Live Toggle + Language Switcher */}
          <div className="flex flex-wrap items-center gap-2.5">
            {lastEvent && !conn.usingMock && (
              <span
                translate="no"
                className="hidden items-center gap-1.5 rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1 font-mono text-[11px] text-slate-400 xl:inline-flex"
              >
                <Radio className="h-3 w-3 text-emerald-400" />
                <span>
                  {lastEvent.type === 'embedding.progress' &&
                  typeof lastEvent.data.completed_sessions === 'number' &&
                  typeof lastEvent.data.total_sessions === 'number'
                    ? `${lastEvent.type} (${lastEvent.data.completed_sessions}/${lastEvent.data.total_sessions})`
                    : lastEvent.type}
                </span>
              </span>
            )}

            <div
              key={conn.usingMock ? 'mock' : conn.backendReachable ? 'live' : 'offline'}
              className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium ${
                conn.usingMock
                  ? 'border-amber-500/40 bg-amber-500/10 text-amber-300'
                  : conn.backendReachable
                    ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
                    : 'border-rose-500/40 bg-rose-500/10 text-rose-300'
              }`}
              title={conn.lastError ?? undefined}
            >
              {conn.usingMock ? (
                <span key="mock-pill" className="inline-flex items-center gap-1.5">
                  <WifiOff className="h-3.5 w-3.5" />
                  <span>{t('Demo Fallback', '演示数据模式')}</span>
                </span>
              ) : conn.backendReachable ? (
                <span key="live-pill" className="inline-flex items-center gap-1.5">
                  <span className="h-2 w-2 rounded-full bg-emerald-400" />
                  <span>{t('Live API', '实时接口')}</span>
                </span>
              ) : (
                <span key="offline-pill" className="inline-flex items-center gap-1.5">
                  <WifiOff className="h-3.5 w-3.5" />
                  <span>{t('API Offline', '接口离线')}</span>
                </span>
              )}
            </div>

            <div className="inline-flex items-center rounded-lg border border-slate-800 bg-slate-950 p-0.5 text-[11px]">
              {(['auto', 'live', 'demo'] as ConnectionMode[]).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => handleModeChange(m)}
                  className={`rounded-md px-2 py-1 font-medium capitalize transition-colors ${
                    conn.mode === m
                      ? 'bg-slate-800 text-white'
                      : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  <span>
                    {m === 'auto'
                      ? t('auto', '自动')
                      : m === 'live'
                        ? t('live', '实时')
                        : t('demo', '演示')}
                  </span>
                </button>
              ))}
            </div>

            <button
              type="button"
              onClick={toggleLocale}
              aria-label={t('Switch UI language to Chinese', '切换界面语言为英文')}
              title={t('Switch UI to Chinese (native, no browser translate needed)', '切换为原生英文界面')}
              className="inline-flex items-center gap-1 rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1 text-[11px] font-medium text-slate-300 transition-colors hover:border-indigo-500/50 hover:text-white"
            >
              <Languages className="h-3.5 w-3.5 text-indigo-400" />
              <span translate="no">{locale === 'zh' ? '中文' : 'EN'}</span>
            </button>

            {conn.usingMock && conn.mode === 'auto' && (
              <button
                type="button"
                onClick={() => handleModeChange('auto')}
                title={t('Retry connecting to local FastAPI backend', '重试连接本地后端服务')}
                className="inline-flex items-center gap-1 rounded-lg border border-slate-700 bg-slate-800/80 px-2.5 py-1 text-xs text-slate-300 hover:text-white"
              >
                <RefreshCw className="h-3 w-3" />
                <span>{t('Retry', '重试')}</span>
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Main Content Viewport */}
      <main className="min-h-0 flex-1 overflow-hidden p-4 lg:p-6">
        <div className="mx-auto h-full max-w-[1600px]">
          {!initialProbeDone ? (
            <div className="flex h-full items-center justify-center text-xs text-slate-500">
              <span>{t('Checking local Omnimex API...', '正在检查本地 Omnimex 服务...')}</span>
            </div>
          ) : (
            <ErrorBoundary
              key={`boundary-${activeTab}`}
              isZh={isZh}
              resetKeys={[activeTab, selectedSessionId, refreshSignal]}
              onReset={() => setRefreshSignal((s) => s + 1)}
            >
              {activeTab === 'search' && (
                <SearchExplorer
                  selectedSessionId={selectedSessionId}
                  highlightTurnIndex={highlightTurnIndex}
                  onSelectSession={handleSelectSession}
                  onCreateHandoff={handleCreateHandoff}
                  refreshSignal={refreshSignal}
                  listRefreshSignal={listRefreshSignal}
                  hasPendingSessionUpdate={
                    Boolean(selectedSessionId && pendingUpdateSessionId === selectedSessionId)
                  }
                  onClearPendingSessionUpdate={() => setPendingUpdateSessionId(null)}
                />
              )}
              {activeTab === 'memories' && (
                <MemoryHub
                  onOpenSession={handleOpenSessionFromMemory}
                  refreshSignal={refreshSignal + listRefreshSignal}
                />
              )}
              {activeTab === 'handoff' && (
                <HandoffWorkbench
                  initialSessionId={handoffSessionId ?? selectedSessionId}
                  refreshSignal={refreshSignal}
                />
              )}
              {activeTab === 'adapters' && (
                <AdaptersStats
                  refreshSignal={refreshSignal + listRefreshSignal}
                  onDataChanged={() => setRefreshSignal((s) => s + 1)}
                />
              )}
            </ErrorBoundary>
          )}
        </div>
      </main>
    </div>
  );
}

export function App() {
  return (
    <LocaleProvider>
      <ErrorBoundary fullScreen>
        <AppShell />
      </ErrorBoundary>
    </LocaleProvider>
  );
}

export default App;
