import { useCallback, useEffect, useState } from 'react';
import {
  BookOpen,
  CheckCircle2,
  Clock,
  ExternalLink,
  FileCode2,
  FolderSync,
  Pencil,
  Plus,
  Save,
  Search,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import { apiClient } from '../api/client';
import type {
  CreateMemoryPayload,
  MemoryCard,
  MemoryCategory,
  NativeMemoryImportStatus,
} from '../api/types';
import { AgentBadge } from '../components/AgentBadge';
import { MarkdownContent } from '../components/MarkdownContent';
import { formatRelativeTime } from '../utils/format';

interface MemoryHubProps {
  onOpenSession: (sessionId: string, turnIndex?: number | null) => void;
  refreshSignal: number;
}

const CATEGORY_META: Record<
  MemoryCategory,
  { label: string; badgeClass: string }
> = {
  preference: {
    label: 'Preference',
    badgeClass: 'border-sky-500/30 bg-sky-500/15 text-sky-300',
  },
  decision: {
    label: 'Decision',
    badgeClass: 'border-indigo-500/30 bg-indigo-500/15 text-indigo-300',
  },
  pitfall: {
    label: 'Pitfall',
    badgeClass: 'border-rose-500/30 bg-rose-500/15 text-rose-300',
  },
  fact: {
    label: 'Fact',
    badgeClass: 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300',
  },
};

function OriginDistillBadge({ card }: { card: MemoryCard }) {
  if (card.origin === 'manual') {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-emerald-500/30 bg-emerald-500/15 px-2 py-0.5 text-[11px] font-medium text-emerald-300">
        Manual
      </span>
    );
  }
  if (card.origin === 'native_import') {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-cyan-500/30 bg-cyan-500/15 px-2 py-0.5 text-[11px] font-medium text-cyan-300">
        Native Import · {card.distill_status}
      </span>
    );
  }
  if (card.distill_status === 'llm_distilled') {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-violet-500/30 bg-violet-500/15 px-2 py-0.5 text-[11px] font-medium text-violet-300">
        <Sparkles className="h-3 w-3" />
        LLM Distilled
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded border border-amber-500/30 bg-amber-500/15 px-2 py-0.5 text-[11px] font-medium text-amber-300">
      Heuristic
    </span>
  );
}

export function MemoryHub({ onOpenSession, refreshSignal }: MemoryHubProps) {
  const [memories, setMemories] = useState<MemoryCard[]>([]);
  const [nativeImports, setNativeImports] = useState<NativeMemoryImportStatus[]>([]);
  const [countsByCategory, setCountsByCategory] = useState<Record<MemoryCategory, number>>({
    preference: 0,
    decision: 0,
    pitfall: 0,
    fact: 0,
  });

  const [categoryFilter, setCategoryFilter] = useState<MemoryCategory | ''>('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'draft' | 'heuristic' | 'llm_distilled' | 'manual' | 'native_import'>('all');
  const [searchText, setSearchText] = useState('');

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editMode, setEditMode] = useState(false);
  const [draftTitle, setDraftTitle] = useState('');
  const [draftCategory, setDraftCategory] = useState<MemoryCategory>('decision');
  const [draftContent, setDraftContent] = useState('');
  const [draftTags, setDraftTags] = useState('');
  const [draftWorkspace, setDraftWorkspace] = useState('');

  const [creating, setCreating] = useState(false);
  const [newForm, setNewForm] = useState<CreateMemoryPayload>({
    title: '',
    category: 'decision',
    content: '',
    tags: [],
    workspace: 'E:/AAAAA project test/omnimex',
    reviewed: true,
  });
  const [newTagsText, setNewTagsText] = useState('');

  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [banner, setBanner] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const loadMemories = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiClient.listMemories({
        category: categoryFilter || undefined,
        distill_status:
          statusFilter === 'heuristic' || statusFilter === 'llm_distilled'
            ? statusFilter
            : undefined,
        origin:
          statusFilter === 'manual' || statusFilter === 'native_import'
            ? statusFilter
            : undefined,
        reviewed: statusFilter === 'draft' ? false : undefined,
        q: searchText || undefined,
      });
      setMemories(res.memories);
      setNativeImports(res.native_imports);
      setCountsByCategory(res.counts_by_category);

      setSelectedId((prev) => {
        if (res.memories.length === 0) return null;
        if (prev && res.memories.some((m) => m.id === prev)) return prev;
        return res.memories[0].id;
      });
    } catch (err) {
      setBanner({
        kind: 'err',
        text: err instanceof Error ? err.message : 'Failed to fetch memories',
      });
    } finally {
      setLoading(false);
    }
  }, [categoryFilter, statusFilter, searchText]);

  useEffect(() => {
    void loadMemories();
  }, [loadMemories, refreshSignal]);

  const selectedMemory = memories.find((m) => m.id === selectedId) ?? null;

  useEffect(() => {
    if (!selectedMemory) {
      setEditMode(false);
      return;
    }
    setDraftTitle(selectedMemory.title);
    setDraftCategory(selectedMemory.category);
    setDraftContent(selectedMemory.content);
    setDraftTags(selectedMemory.tags.join(', '));
    setDraftWorkspace(selectedMemory.workspace ?? '');
    setEditMode(false);
  }, [selectedMemory?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSaveEdit = async () => {
    if (!selectedMemory) return;
    try {
      const updated = await apiClient.updateMemory(selectedMemory.id, {
        title: draftTitle.trim(),
        category: draftCategory,
        content: draftContent,
        tags: draftTags
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean),
        workspace: draftWorkspace.trim() || null,
      });
      setEditMode(false);
      setBanner({ kind: 'ok', text: `Saved ${updated.file_path ?? updated.title}` });
      await loadMemories();
    } catch (err) {
      setBanner({ kind: 'err', text: err instanceof Error ? err.message : 'Failed to save memory' });
    }
  };

  const handleApprove = async (card: MemoryCard) => {
    try {
      await apiClient.updateMemory(card.id, { reviewed: !card.reviewed });
      setBanner({
        kind: 'ok',
        text: card.reviewed ? 'Marked as unreviewed draft.' : 'Approved and marked reviewed.',
      });
      await loadMemories();
    } catch (err) {
      setBanner({ kind: 'err', text: err instanceof Error ? err.message : 'Failed to update status' });
    }
  };

  const handleDelete = async (card: MemoryCard) => {
    try {
      await apiClient.deleteMemory(card.id);
      setBanner({ kind: 'ok', text: `Deleted "${card.title}"` });
      if (selectedId === card.id) setSelectedId(null);
      await loadMemories();
    } catch (err) {
      setBanner({ kind: 'err', text: err instanceof Error ? err.message : 'Failed to delete memory' });
    }
  };

  const handleSync = async () => {
    setSyncing(true);
    try {
      const res = await apiClient.syncMemories({ import_native: true });
      setBanner({
        kind: 'ok',
        text: `Synced ${res.synced_files} Markdown files · Imported ${res.imported_native} native memor${
          res.imported_native === 1 ? 'y' : 'ies'
        }.`,
      });
      await loadMemories();
    } catch (err) {
      setBanner({ kind: 'err', text: err instanceof Error ? err.message : 'Memory sync failed' });
    } finally {
      setSyncing(false);
    }
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newForm.title.trim() || !newForm.content.trim()) return;
    try {
      const created = await apiClient.createMemory({
        ...newForm,
        title: newForm.title.trim(),
        tags: newTagsText
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean),
        workspace: newForm.workspace?.trim() || null,
      });
      setCreating(false);
      setNewForm({
        title: '',
        category: 'decision',
        content: '',
        tags: [],
        workspace: 'E:/AAAAA project test/omnimex',
        reviewed: true,
      });
      setNewTagsText('');
      setSelectedId(created.id);
      setBanner({ kind: 'ok', text: `Created central memory ${created.file_path ?? created.title}` });
      await loadMemories();
    } catch (err) {
      setBanner({ kind: 'err', text: err instanceof Error ? err.message : 'Failed to create memory' });
    }
  };

  return (
    <div className="flex h-full flex-col gap-4">
      {/* Top Toolbar + Native Import Strip */}
      <div className="flex flex-col gap-3 rounded-xl border border-slate-800 bg-slate-900/60 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => setCategoryFilter('')}
              className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
                categoryFilter === ''
                  ? 'border-indigo-500/60 bg-indigo-500/20 text-indigo-200'
                  : 'border-slate-800 bg-slate-950 text-slate-400 hover:text-white'
              }`}
            >
              All ({Object.values(countsByCategory).reduce((a, b) => a + b, 0)})
            </button>
            {(['preference', 'decision', 'pitfall', 'fact'] as MemoryCategory[]).map((cat) => (
              <button
                key={cat}
                type="button"
                onClick={() => setCategoryFilter(cat === categoryFilter ? '' : cat)}
                className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
                  categoryFilter === cat
                    ? CATEGORY_META[cat].badgeClass
                    : 'border-slate-800 bg-slate-950 text-slate-400 hover:text-white'
                }`}
              >
                {CATEGORY_META[cat].label} ({countsByCategory[cat] ?? 0})
              </button>
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
              <input
                type="text"
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                placeholder="Filter memories..."
                className="rounded-lg border border-slate-800 bg-slate-950 py-1.5 pr-3 pl-8 text-xs text-slate-200 placeholder-slate-500 focus:border-indigo-500 focus:outline-none"
              />
            </div>

            <select
              value={statusFilter}
              onChange={(e) =>
                setStatusFilter(e.target.value as typeof statusFilter)
              }
              aria-label="Filter by distill or review status"
              className="rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1.5 text-xs text-slate-300 focus:border-indigo-500 focus:outline-none"
            >
              <option value="all">All statuses</option>
              <option value="draft">Needs Review (Drafts)</option>
              <option value="heuristic">Heuristic</option>
              <option value="llm_distilled">LLM Distilled</option>
              <option value="manual">Manual</option>
              <option value="native_import">Native Imports</option>
            </select>

            <button
              type="button"
              onClick={handleSync}
              disabled={syncing}
              className="inline-flex items-center gap-1.5 rounded-lg border border-cyan-500/40 bg-cyan-500/10 px-3 py-1.5 text-xs font-medium text-cyan-200 hover:bg-cyan-500/20 disabled:opacity-50"
            >
              <FolderSync className={`h-3.5 w-3.5 ${syncing ? 'animate-spin' : ''}`} />
              {syncing ? 'Syncing...' : 'Sync & Import Native'}
            </button>

            <button
              type="button"
              onClick={() => setCreating(true)}
              className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-indigo-500"
            >
              <Plus className="h-3.5 w-3.5" />
              New Memory
            </button>
          </div>
        </div>

        {/* Native Memory Import Status */}
        <div className="flex flex-wrap items-center gap-2 border-t border-slate-800/80 pt-3">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
            Native Memory Sync:
          </span>
          {nativeImports.map((item) => (
            <div
              key={item.agent}
              title={item.source_dir}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/70 px-2.5 py-1 text-xs"
            >
              <AgentBadge agent={item.agent} compact />
              <span className="font-mono text-[11px] text-slate-400">
                {item.imported_count}/{item.discovered_files} imported
              </span>
              <span
                className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase ${
                  item.status === 'synced'
                    ? 'bg-emerald-500/15 text-emerald-300'
                    : item.status === 'pending'
                      ? 'bg-amber-500/15 text-amber-300'
                      : 'bg-slate-800 text-slate-400'
                }`}
              >
                {item.status}
              </span>
            </div>
          ))}
        </div>
      </div>

      {banner && (
        <div
          className={`flex items-center justify-between rounded-lg border px-4 py-2 text-xs ${
            banner.kind === 'ok'
              ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-300'
              : 'border-rose-500/40 bg-rose-950/30 text-rose-300'
          }`}
        >
          <span>{banner.text}</span>
          <button type="button" onClick={() => setBanner(null)} className="opacity-70 hover:opacity-100">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {/* Split Browser & Editor */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-12">
        {/* Left List */}
        <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/50 lg:col-span-5">
          <div className="border-b border-slate-800 px-3.5 py-2.5 text-xs font-medium text-slate-400">
            Central Markdown Memories ({memories.length})
          </div>
          <div className="flex-1 divide-y divide-slate-800/70 overflow-y-auto">
            {loading ? (
              <div className="p-6 text-center text-xs text-slate-500">Loading memories...</div>
            ) : memories.length === 0 ? (
              <div className="p-8 text-center text-xs text-slate-500">
                No memories match your current filters.
              </div>
            ) : (
              memories.map((card) => {
                const selected = card.id === selectedId;
                return (
                  <button
                    key={card.id}
                    type="button"
                    onClick={() => setSelectedId(card.id)}
                    className={`w-full p-3.5 text-left transition-colors hover:bg-slate-800/40 ${
                      selected ? 'bg-indigo-500/10 ring-1 ring-inset ring-indigo-500/40' : ''
                    }`}
                  >
                    <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
                      <span
                        className={`rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${
                          CATEGORY_META[card.category].badgeClass
                        }`}
                      >
                        {CATEGORY_META[card.category].label}
                      </span>
                      <OriginDistillBadge card={card} />
                      {!card.reviewed ? (
                        <span className="inline-flex items-center gap-1 rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-300">
                          <Clock className="h-2.5 w-2.5" />
                          Draft Review
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-[10px] text-emerald-400">
                          <CheckCircle2 className="h-3 w-3" />
                          Verified
                        </span>
                      )}
                    </div>
                    <div className="line-clamp-1 text-sm font-medium text-slate-100">
                      {card.title}
                    </div>
                    <div className="mt-1.5 flex items-center justify-between text-[11px] text-slate-500">
                      <span className="truncate font-mono">{card.file_path ?? `${card.category}s/${card.id}.md`}</span>
                      <span>{formatRelativeTime(card.updated_at)}</span>
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>

        {/* Right Detail / Editor */}
        <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40 lg:col-span-7">
          {!selectedMemory ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 p-10 text-center">
              <BookOpen className="h-8 w-8 text-slate-600" />
              <p className="text-sm font-medium text-slate-300">Select a memory card</p>
              <p className="max-w-sm text-xs text-slate-500">
                Browse, review heuristic drafts, inspect session provenance, or edit central Markdown knowledge files.
              </p>
            </div>
          ) : (
            <>
              <div className="border-b border-slate-800 bg-slate-900/70 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="space-y-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        className={`rounded border px-2 py-0.5 text-[11px] font-semibold uppercase ${
                          CATEGORY_META[selectedMemory.category].badgeClass
                        }`}
                      >
                        {CATEGORY_META[selectedMemory.category].label}
                      </span>
                      <OriginDistillBadge card={selectedMemory} />
                      {selectedMemory.confidence != null && (
                        <span className="font-mono text-[11px] text-slate-400">
                          {Math.round(selectedMemory.confidence * 100)}% conf
                        </span>
                      )}
                    </div>

                    {editMode ? (
                      <input
                        type="text"
                        value={draftTitle}
                        onChange={(e) => setDraftTitle(e.target.value)}
                        className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm font-semibold text-white focus:border-indigo-500 focus:outline-none"
                      />
                    ) : (
                      <h2 className="text-base font-semibold text-white">
                        {selectedMemory.title}
                      </h2>
                    )}

                    <div className="flex flex-wrap items-center gap-3 font-mono text-[11px] text-slate-400">
                      <span className="inline-flex items-center gap-1">
                        <FileCode2 className="h-3.5 w-3.5 text-slate-500" />
                        data/memories/{selectedMemory.file_path ?? `${selectedMemory.category}s/${selectedMemory.id}.md`}
                      </span>
                      {selectedMemory.source_session_id && (
                        <button
                          type="button"
                          onClick={() =>
                            onOpenSession(
                              selectedMemory.source_session_id!,
                              selectedMemory.source_turn_index ?? null
                            )
                          }
                          className="inline-flex items-center gap-1.5 rounded border border-indigo-500/30 bg-indigo-500/10 px-2 py-0.5 text-indigo-300 hover:bg-indigo-500/20"
                        >
                          {selectedMemory.source_agent && (
                            <AgentBadge agent={selectedMemory.source_agent} compact />
                          )}
                          <span>
                            Source: {selectedMemory.source_session_id}
                            {selectedMemory.source_turn_index != null
                              ? ` #${selectedMemory.source_turn_index}`
                              : ''}
                          </span>
                          <ExternalLink className="h-3 w-3" />
                        </button>
                      )}
                      {selectedMemory.native_source_path && (
                        <span className="rounded border border-cyan-500/30 bg-cyan-500/10 px-2 py-0.5 text-cyan-300">
                          Imported: {selectedMemory.native_source_path}
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={() => handleApprove(selectedMemory)}
                      className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
                        selectedMemory.reviewed
                          ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25'
                          : 'border-amber-500/50 bg-amber-500/15 text-amber-200 hover:bg-amber-500/25'
                      }`}
                    >
                      <CheckCircle2 className="h-3.5 w-3.5" />
                      {selectedMemory.reviewed ? 'Reviewed' : 'Approve Draft'}
                    </button>

                    {editMode ? (
                      <>
                        <button
                          type="button"
                          onClick={handleSaveEdit}
                          className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-indigo-500"
                        >
                          <Save className="h-3.5 w-3.5" />
                          Save
                        </button>
                        <button
                          type="button"
                          onClick={() => setEditMode(false)}
                          className="rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setEditMode(true)}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800/70 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-800"
                      >
                        <Pencil className="h-3.5 w-3.5" />
                        Edit
                      </button>
                    )}

                    <button
                      type="button"
                      onClick={() => handleDelete(selectedMemory)}
                      title="Delete memory"
                      className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-1.5 text-rose-300 hover:bg-rose-500/20"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
              </div>

              <div className="flex-1 overflow-y-auto p-4">
                {editMode ? (
                  <div className="space-y-3">
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                      <div>
                        <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                          Category
                        </label>
                        <select
                          value={draftCategory}
                          onChange={(e) => setDraftCategory(e.target.value as MemoryCategory)}
                          className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-slate-200 focus:border-indigo-500 focus:outline-none"
                        >
                          <option value="preference">preference</option>
                          <option value="decision">decision</option>
                          <option value="pitfall">pitfall</option>
                          <option value="fact">fact</option>
                        </select>
                      </div>
                      <div>
                        <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                          Tags (comma separated)
                        </label>
                        <input
                          type="text"
                          value={draftTags}
                          onChange={(e) => setDraftTags(e.target.value)}
                          className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-slate-200 focus:border-indigo-500 focus:outline-none"
                        />
                      </div>
                      <div>
                        <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                          Workspace
                        </label>
                        <input
                          type="text"
                          value={draftWorkspace}
                          onChange={(e) => setDraftWorkspace(e.target.value)}
                          className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-xs text-slate-200 focus:border-indigo-500 focus:outline-none"
                        />
                      </div>
                    </div>

                    <div>
                      <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                        Markdown Body
                      </label>
                      <textarea
                        rows={12}
                        value={draftContent}
                        onChange={(e) => setDraftContent(e.target.value)}
                        className="w-full rounded-lg border border-slate-700 bg-slate-950 p-3 font-mono text-xs leading-relaxed text-slate-100 focus:border-indigo-500 focus:outline-none"
                      />
                    </div>
                  </div>
                ) : (
                  <div className="space-y-4">
                    <MarkdownContent content={selectedMemory.content} />
                    {selectedMemory.tags.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 border-t border-slate-800/80 pt-3">
                        {selectedMemory.tags.map((tag) => (
                          <span
                            key={tag}
                            className="rounded-md border border-slate-800 bg-slate-950 px-2 py-0.5 font-mono text-[11px] text-slate-400"
                          >
                            #{tag}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Create Memory Modal */}
      {creating && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm">
          <form
            onSubmit={handleCreate}
            className="w-full max-w-lg space-y-4 rounded-2xl border border-slate-800 bg-slate-900 p-5 shadow-2xl"
          >
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-white">New Central Markdown Memory</h3>
              <button
                type="button"
                onClick={() => setCreating(false)}
                className="text-slate-400 hover:text-white"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="space-y-3">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-300">Title</label>
                <input
                  required
                  type="text"
                  value={newForm.title}
                  onChange={(e) => setNewForm((f) => ({ ...f, title: e.target.value }))}
                  placeholder="e.g. Always run pytest via .venv on Windows"
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-white focus:border-indigo-500 focus:outline-none"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="mb-1 block text-xs font-medium text-slate-300">Category</label>
                  <select
                    value={newForm.category}
                    onChange={(e) =>
                      setNewForm((f) => ({ ...f, category: e.target.value as MemoryCategory }))
                    }
                    className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-white focus:border-indigo-500 focus:outline-none"
                  >
                    <option value="preference">preference</option>
                    <option value="decision">decision</option>
                    <option value="pitfall">pitfall</option>
                    <option value="fact">fact</option>
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-slate-300">
                    Tags (comma separated)
                  </label>
                  <input
                    type="text"
                    value={newTagsText}
                    onChange={(e) => setNewTagsText(e.target.value)}
                    placeholder="windows, testing"
                    className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-white focus:border-indigo-500 focus:outline-none"
                  />
                </div>
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-slate-300">Workspace</label>
                <input
                  type="text"
                  value={newForm.workspace ?? ''}
                  onChange={(e) => setNewForm((f) => ({ ...f, workspace: e.target.value }))}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs text-white focus:border-indigo-500 focus:outline-none"
                />
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-slate-300">
                  Markdown Content
                </label>
                <textarea
                  required
                  rows={6}
                  value={newForm.content}
                  onChange={(e) => setNewForm((f) => ({ ...f, content: e.target.value }))}
                  placeholder="## Rule&#10;Write crisp, actionable guidance..."
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 p-3 font-mono text-xs text-white focus:border-indigo-500 focus:outline-none"
                />
              </div>
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={() => setCreating(false)}
                className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
              >
                Cancel
              </button>
              <button
                type="submit"
                className="rounded-lg bg-indigo-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-indigo-500"
              >
                Create Memory
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
