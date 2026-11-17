import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  ArrowRightLeft,
  BookOpen,
  Database,
  Radio,
  RefreshCw,
  Search,
  WifiOff,
} from 'lucide-react';
import { apiClient, type ConnectionMode, type ConnectionState } from './api/client';
import type { OmnimexEvent } from './api/types';
import { AdaptersStats } from './sections/AdaptersStats';
import { HandoffWorkbench } from './sections/HandoffWorkbench';
import { MemoryHub } from './sections/MemoryHub';
import { SearchExplorer } from './sections/SearchExplorer';

type SectionId = 'search' | 'memories' | 'handoff' | 'adapters';

const NAV_ITEMS: Array<{ id: SectionId; label: string; icon: typeof Search }> = [
  { id: 'search', label: 'Search & Explorer', icon: Search },
  { id: 'memories', label: 'Memory Hub', icon: BookOpen },
  { id: 'handoff', label: 'Handoff Workbench', icon: ArrowRightLeft },
  { id: 'adapters', label: 'Adapters & Stats', icon: Activity },
];

export function App() {
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
        evt.type === 'distill.completed'
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
                  <span className="text-sm font-bold tracking-tight text-white">Omnimex</span>
                  <span className="rounded border border-slate-700 bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-300">
                    LOCAL
                  </span>
                </div>
                <p className="text-[11px] text-slate-400">Cross-Agent Memory & Session Hub</p>
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
                    <span>{item.label}</span>
                  </button>
                );
              })}
            </nav>
          </div>

          {/* Right Status + Demo/Live Toggle */}
          <div className="flex flex-wrap items-center gap-2.5">
            {lastEvent && !conn.usingMock && (
              <span className="hidden items-center gap-1.5 rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1 font-mono text-[11px] text-slate-400 xl:inline-flex">
                <Radio className="h-3 w-3 text-emerald-400" />
                {lastEvent.type}
              </span>
            )}

            <div
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
                <>
                  <WifiOff className="h-3.5 w-3.5" />
                  <span>Demo Fallback</span>
                </>
              ) : conn.backendReachable ? (
                <>
                  <span className="h-2 w-2 rounded-full bg-emerald-400" />
                  <span>Live API</span>
                </>
              ) : (
                <>
                  <WifiOff className="h-3.5 w-3.5" />
                  <span>API Offline</span>
                </>
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
                  {m}
                </button>
              ))}
            </div>

            {conn.usingMock && conn.mode === 'auto' && (
              <button
                type="button"
                onClick={() => handleModeChange('auto')}
                title="Retry connecting to local FastAPI backend"
                className="inline-flex items-center gap-1 rounded-lg border border-slate-700 bg-slate-800/80 px-2.5 py-1 text-xs text-slate-300 hover:text-white"
              >
                <RefreshCw className="h-3 w-3" />
                Retry
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
              Checking local Omnimex API...
            </div>
          ) : (
            <>
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
            </>
          )}
        </div>
      </main>
    </div>
  );
}

export default App;
