import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  CheckSquare,
  Download,
  FileCode2,
  FolderCheck,
  PackagePlus,
  Pencil,
  Save,
  ShieldCheck,
  Sparkles,
  Square,
  Trash2,
  Wand2,
} from 'lucide-react';
import { apiClient } from '../api/client';
import type {
  MemoryCard,
  NormalizedSession,
  SavedSkillSummary,
  SkillDraft,
  SkillInstallResult,
  SkillInstallTarget,
} from '../api/types';
import { AgentBadge } from '../components/AgentBadge';
import { CopyButton } from '../components/CopyButton';
import { MarkdownContent } from '../components/MarkdownContent';
import { formatRelativeTime, shortenWorkspace } from '../utils/format';
import { useLocale } from '../utils/i18n';

interface SkillForgeProps {
  initialSessionIds: string[];
  initialMemoryIds: string[];
  seedNonce: number;
  refreshSignal: number;
}

const INSTALL_TARGETS: Array<{ id: SkillInstallTarget; label: string; hint: string }> = [
  { id: 'antigravity', label: 'Antigravity', hint: '~/.gemini/antigravity/skills/' },
  { id: 'claude_code', label: 'Claude Code', hint: '~/.claude/skills/' },
  { id: 'codex', label: 'Codex CLI', hint: '~/.codex/skills/' },
  { id: 'cursor', label: 'Cursor', hint: '~/.cursor/skills/' },
  { id: 'gemini_cli', label: 'Gemini CLI', hint: '~/.gemini/skills/' },
  { id: 'windsurf', label: 'Windsurf', hint: '~/.codeium/windsurf/skills/' },
];

export function SkillForge({
  initialSessionIds,
  initialMemoryIds,
  seedNonce,
  refreshSignal,
}: SkillForgeProps) {
  const { t } = useLocale();
  const [sessions, setSessions] = useState<NormalizedSession[]>([]);
  const [memories, setMemories] = useState<MemoryCard[]>([]);
  const [savedSkills, setSavedSkills] = useState<SavedSkillSummary[]>([]);

  const [sourceTab, setSourceTab] = useState<'sessions' | 'memories' | 'saved'>('sessions');
  const [sourceFilter, setSourceFilter] = useState('');
  const [selectedSessionIds, setSelectedSessionIds] = useState<string[]>(initialSessionIds);
  const [selectedMemoryIds, setSelectedMemoryIds] = useState<string[]>(initialMemoryIds);

  const [skillName, setSkillName] = useState('');
  const [skillDesc, setSkillDesc] = useState('');
  const [customInstructions, setCustomInstructions] = useState('');
  const [useLlm, setUseLlm] = useState(false);

  const [draftSlug, setDraftSlug] = useState('');
  const [draftMd, setDraftMd] = useState('');
  const [draftMeta, setDraftMeta] = useState<SkillDraft | null>(null);
  const [viewMode, setViewMode] = useState<'rendered' | 'edit'>('rendered');

  const [installTargets, setInstallTargets] = useState<SkillInstallTarget[]>([
    'antigravity',
    'claude_code',
    'codex',
  ]);
  const [showInstallPanel, setShowInstallPanel] = useState(false);

  const [generating, setGenerating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [installResult, setInstallResult] = useState<SkillInstallResult | null>(null);
  const [notice, setNotice] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const loadWorkspaceData = useCallback(async () => {
    try {
      const [sessRes, memRes, skillRes] = await Promise.all([
        apiClient.listSessions({ limit: 100 }),
        apiClient.listMemories({}),
        apiClient.listSkills(),
      ]);
      setSessions(sessRes.sessions);
      setMemories(memRes.memories);
      setSavedSkills(skillRes.skills);
    } catch (err) {
      setNotice({
        kind: 'err',
        text: err instanceof Error ? err.message : 'Failed to load sessions and memories',
      });
    }
  }, []);

  useEffect(() => {
    void loadWorkspaceData();
  }, [loadWorkspaceData, refreshSignal]);

  const runGenerate = useCallback(
    async (sessIds: string[], memIds: string[]) => {
      if (sessIds.length === 0 && memIds.length === 0) {
        setNotice({
          kind: 'err',
          text: t(
            'Select at least one session or memory card first.',
            '请先勾选至少一条对话会话或记忆卡片。'
          ),
        });
        return;
      }
      setGenerating(true);
      setNotice(null);
      setInstallResult(null);
      try {
        const res = await apiClient.generateSkill({
          session_ids: sessIds,
          memory_ids: memIds,
          name: skillName.trim() || undefined,
          description: skillDesc.trim() || undefined,
          custom_instructions: customInstructions.trim() || undefined,
          use_llm: useLlm,
        });
        setDraftMeta(res);
        setDraftSlug(res.slug);
        setDraftMd(res.skill_md);
      } catch (err) {
        setNotice({
          kind: 'err',
          text: err instanceof Error ? err.message : 'Failed to package skill',
        });
      } finally {
        setGenerating(false);
      }
    },
    [skillName, skillDesc, customInstructions, useLlm, t]
  );

  // When jumped here from SearchExplorer or MemoryHub, preselect & auto-generate draft
  useEffect(() => {
    if (seedNonce <= 0) return;
    setSelectedSessionIds(initialSessionIds);
    setSelectedMemoryIds(initialMemoryIds);
    if (initialMemoryIds.length > 0 && initialSessionIds.length === 0) {
      setSourceTab('memories');
    } else if (initialSessionIds.length > 0) {
      setSourceTab('sessions');
    }
    if (initialSessionIds.length > 0 || initialMemoryIds.length > 0) {
      void runGenerate(initialSessionIds, initialMemoryIds);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedNonce]);

  const filteredSessions = useMemo(() => {
    const q = sourceFilter.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter((s) =>
      `${s.title} ${s.agent} ${s.workspace ?? ''}`.toLowerCase().includes(q)
    );
  }, [sessions, sourceFilter]);

  const filteredMemories = useMemo(() => {
    const q = sourceFilter.trim().toLowerCase();
    if (!q) return memories;
    return memories.filter((m) =>
      `${m.title} ${m.category} ${m.tags.join(' ')} ${m.content}`.toLowerCase().includes(q)
    );
  }, [memories, sourceFilter]);

  const toggleSession = (id: string) => {
    setSelectedSessionIds((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]
    );
  };

  const toggleMemory = (id: string) => {
    setSelectedMemoryIds((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]
    );
  };

  const toggleTarget = (target: SkillInstallTarget) => {
    setInstallTargets((prev) =>
      prev.includes(target) ? prev.filter((tItem) => tItem !== target) : [...prev, target]
    );
  };

  const handleSave = async () => {
    if (!draftSlug.trim() || !draftMd.trim()) return;
    setSaving(true);
    setNotice(null);
    try {
      const saved = await apiClient.saveSkill({
        slug: draftSlug.trim(),
        skill_md: draftMd,
      });
      setDraftSlug(saved.slug);
      setDraftMd(saved.skill_md);
      const listRes = await apiClient.listSkills();
      setSavedSkills(listRes.skills);
      setNotice({
        kind: 'ok',
        text: t(`Saved skill to ${saved.file_path}`, `已保存 Skill 到本地库：${saved.file_path}`),
      });
    } catch (err) {
      setNotice({
        kind: 'err',
        text: err instanceof Error ? err.message : 'Failed to save skill',
      });
    } finally {
      setSaving(false);
    }
  };

  const handleExportZip = async () => {
    if (!draftSlug.trim() || !draftMd.trim()) return;
    setExporting(true);
    setNotice(null);
    try {
      const blob = await apiClient.exportSkillZip(draftSlug.trim(), draftMd);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${draftSlug.trim()}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setNotice({
        kind: 'ok',
        text: t(`Exported ${draftSlug.trim()}.zip`, `已导出技能压缩包 ${draftSlug.trim()}.zip`),
      });
    } catch (err) {
      setNotice({
        kind: 'err',
        text: err instanceof Error ? err.message : 'ZIP export failed',
      });
    } finally {
      setExporting(false);
    }
  };

  const handleInstall = async () => {
    if (!draftSlug.trim() || !draftMd.trim()) return;
    if (installTargets.length === 0) {
      setNotice({
        kind: 'err',
        text: t('Select at least one target Agent.', '请至少选择一个目标 Agent。'),
      });
      return;
    }
    setInstalling(true);
    setNotice(null);
    try {
      const res = await apiClient.installSkill({
        slug: draftSlug.trim(),
        skill_md: draftMd,
        targets: installTargets,
        save_to_archive: true,
      });
      setInstallResult(res);
      const listRes = await apiClient.listSkills();
      setSavedSkills(listRes.skills);
      if (res.errors.length > 0) {
        setNotice({ kind: 'err', text: res.errors.join(' | ') });
      } else {
        setNotice({
          kind: 'ok',
          text: t(
            `Installed ${res.slug} to ${res.installed.length} agent skill directories.`,
            `已成功将 ${res.slug} 安装到 ${res.installed.length} 个 Agent 技能目录。`
          ),
        });
      }
    } catch (err) {
      setNotice({
        kind: 'err',
        text: err instanceof Error ? err.message : 'Installation failed',
      });
    } finally {
      setInstalling(false);
    }
  };

  const handleLoadSaved = (item: SavedSkillSummary) => {
    setDraftSlug(item.slug);
    setDraftMd(item.skill_md);
    setDraftMeta({
      slug: item.slug,
      title: item.name,
      description: item.description,
      skill_md: item.skill_md,
      source_session_ids: [],
      source_memory_ids: [],
      tags: [],
      workspace: null,
      provider: 'saved',
      redacted: true,
      token_estimate: item.token_estimate,
      generated_at: item.updated_at,
    });
    setInstallResult(null);
    setNotice(null);
  };

  const handleDeleteSaved = async (slug: string) => {
    try {
      await apiClient.deleteSkill(slug);
      const listRes = await apiClient.listSkills();
      setSavedSkills(listRes.skills);
      if (draftSlug === slug) {
        setDraftMeta(null);
        setDraftSlug('');
        setDraftMd('');
      }
    } catch (err) {
      setNotice({
        kind: 'err',
        text: err instanceof Error ? err.message : 'Failed to delete saved skill',
      });
    }
  };

  const previewBody = useMemo(() => {
    if (!draftMd) return '';
    return draftMd.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, '').trim();
  }, [draftMd]);

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-4 lg:grid-cols-12">
      {/* Left: Source Picker + Options */}
      <div className="flex min-h-0 flex-col gap-3 overflow-y-auto rounded-xl border border-slate-800 bg-slate-900/50 p-4 lg:col-span-5">
        <div className="shrink-0">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-white">
            <PackagePlus className="h-4 w-4 text-indigo-400" />
            <span>{t('1. Select Experience Sources', '1. 选择要沉淀的经验来源')}</span>
          </h2>
          <p className="mt-1 text-xs text-slate-400">
            <span>
              {t(
                'Combine solved sessions and curated memories into a reusable Agent SKILL.md.',
                '把调通的排坑会话与中央记忆卡片一键提炼并打包成标准的 Agent SKILL.md 技能。'
              )}
            </span>
          </p>
        </div>

        {/* Source Switch Tabs */}
        <div className="flex shrink-0 items-center gap-1 rounded-lg border border-slate-800 bg-slate-950 p-1 text-xs">
          <button
            type="button"
            onClick={() => setSourceTab('sessions')}
            className={`flex-1 rounded-md px-2 py-1.5 font-medium transition-colors ${
              sourceTab === 'sessions'
                ? 'bg-indigo-600 text-white'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <span>{t('Sessions', '历史会话')} ({selectedSessionIds.length})</span>
          </button>
          <button
            type="button"
            onClick={() => setSourceTab('memories')}
            className={`flex-1 rounded-md px-2 py-1.5 font-medium transition-colors ${
              sourceTab === 'memories'
                ? 'bg-indigo-600 text-white'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <span>{t('Memories', '记忆卡片')} ({selectedMemoryIds.length})</span>
          </button>
          <button
            type="button"
            onClick={() => setSourceTab('saved')}
            className={`flex-1 rounded-md px-2 py-1.5 font-medium transition-colors ${
              sourceTab === 'saved'
                ? 'bg-indigo-600 text-white'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <span>{t('Archive', '已打包')} ({savedSkills.length})</span>
          </button>
        </div>

        {sourceTab !== 'saved' && (
          <input
            type="text"
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
            placeholder={
              sourceTab === 'sessions'
                ? t('Filter sessions by title, agent, or workspace...', '按标题、Agent 或工作区筛选会话...')
                : t('Filter memories by title, tag, or content...', '按标题、标签或内容筛选记忆...')
            }
            className="w-full shrink-0 rounded-lg border border-slate-800 bg-slate-950 px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:border-indigo-500 focus:outline-none"
          />
        )}

        {/* Source List Box */}
        <div className="min-h-[190px] max-h-64 shrink-0 space-y-1.5 overflow-y-auto pr-1 lg:max-h-none lg:flex-1">
          {sourceTab === 'sessions' &&
            (filteredSessions.length === 0 ? (
              <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-center text-xs text-slate-500">
                <span>{t('No matching sessions.', '没有匹配的会话。')}</span>
              </div>
            ) : (
              filteredSessions.map((s) => {
                const checked = selectedSessionIds.includes(s.id);
                return (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() => toggleSession(s.id)}
                    className={`flex w-full items-start gap-2.5 rounded-lg border p-2.5 text-left transition-colors ${
                      checked
                        ? 'border-indigo-500/60 bg-indigo-500/15'
                        : 'border-slate-800/80 bg-slate-950/60 hover:border-slate-700'
                    }`}
                  >
                    {checked ? (
                      <CheckSquare className="mt-0.5 h-4 w-4 shrink-0 text-indigo-400" />
                    ) : (
                      <Square className="mt-0.5 h-4 w-4 shrink-0 text-slate-600" />
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <AgentBadge agent={s.agent} compact />
                        <span className="font-mono text-[10px] text-slate-500">
                          {s.turn_count}t · {formatRelativeTime(s.updated_at)}
                        </span>
                      </div>
                      <div className="mt-1 line-clamp-1 text-xs font-medium text-slate-100">
                        {s.title}
                      </div>
                      {s.workspace && (
                        <div className="truncate font-mono text-[10px] text-slate-500">
                          {shortenWorkspace(s.workspace)}
                        </div>
                      )}
                    </div>
                  </button>
                );
              })
            ))}

          {sourceTab === 'memories' &&
            (filteredMemories.length === 0 ? (
              <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-center text-xs text-slate-500">
                <span>{t('No matching memories.', '没有匹配的记忆卡片。')}</span>
              </div>
            ) : (
              filteredMemories.map((m) => {
                const checked = selectedMemoryIds.includes(m.id);
                return (
                  <button
                    key={m.id}
                    type="button"
                    onClick={() => toggleMemory(m.id)}
                    className={`flex w-full items-start gap-2.5 rounded-lg border p-2.5 text-left transition-colors ${
                      checked
                        ? 'border-indigo-500/60 bg-indigo-500/15'
                        : 'border-slate-800/80 bg-slate-950/60 hover:border-slate-700'
                    }`}
                  >
                    {checked ? (
                      <CheckSquare className="mt-0.5 h-4 w-4 shrink-0 text-indigo-400" />
                    ) : (
                      <Square className="mt-0.5 h-4 w-4 shrink-0 text-slate-600" />
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="rounded border border-slate-700 bg-slate-800 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-300">
                          {m.category}
                        </span>
                        <span className="font-mono text-[10px] text-slate-500">
                          {formatRelativeTime(m.updated_at)}
                        </span>
                      </div>
                      <div className="mt-1 line-clamp-1 text-xs font-medium text-slate-100">
                        {m.title}
                      </div>
                      <div className="line-clamp-1 text-[11px] text-slate-400">
                        {m.content}
                      </div>
                    </div>
                  </button>
                );
              })
            ))}

          {sourceTab === 'saved' &&
            (savedSkills.length === 0 ? (
              <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-center text-xs text-slate-500">
                <span>{t('No packaged skills saved yet.', '本地技能归档库暂空，生成后点“保存到本地库”即可查看。')}</span>
              </div>
            ) : (
              savedSkills.map((item) => (
                <div
                  key={item.slug}
                  className={`flex items-center justify-between gap-2 rounded-lg border p-2.5 transition-colors ${
                    draftSlug === item.slug
                      ? 'border-indigo-500/60 bg-indigo-500/15'
                      : 'border-slate-800 bg-slate-950/60 hover:border-slate-700'
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => handleLoadSaved(item)}
                    className="min-w-0 flex-1 text-left"
                  >
                    <div className="flex items-center gap-2">
                      <span translate="no" className="font-mono text-xs font-semibold text-indigo-300">
                        {item.slug}
                      </span>
                      <span className="font-mono text-[10px] text-slate-500">
                        ~{item.token_estimate} tok
                      </span>
                    </div>
                    <div className="mt-0.5 line-clamp-1 text-[11px] text-slate-400">
                      {item.description || item.file_path}
                    </div>
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleDeleteSaved(item.slug)}
                    title={t('Delete skill', '删除此 Skill')}
                    className="rounded border border-rose-500/30 bg-rose-500/10 p-1.5 text-rose-300 hover:bg-rose-500/20"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))
            ))}
        </div>

        {/* Metadata & Options */}
        <div className="shrink-0 space-y-2.5 border-t border-slate-800 pt-3">
          <h3 className="text-xs font-semibold text-slate-200">
            <span>{t('2. Skill Metadata & Synthesis', '2. 技能名称与提炼选项')}</span>
          </h3>

          <input
            type="text"
            value={skillName}
            onChange={(e) => setSkillName(e.target.value)}
            placeholder={t('Skill name / slug (optional, e.g. windows-exe-packaging)', 'Skill 名称 / slug（可选，如 windows-exe-packaging）')}
            className="w-full rounded-lg border border-slate-800 bg-slate-950 px-3 py-1.5 font-mono text-xs text-slate-200 placeholder-slate-500 focus:border-indigo-500 focus:outline-none"
          />

          <input
            type="text"
            value={skillDesc}
            onChange={(e) => setSkillDesc(e.target.value)}
            placeholder={t('When should agents trigger this Skill? (optional)', '触发场景描述（可选：告诉 Agent 什么时候启用它）')}
            className="w-full rounded-lg border border-slate-800 bg-slate-950 px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:border-indigo-500 focus:outline-none"
          />

          <textarea
            rows={2}
            value={customInstructions}
            onChange={(e) => setCustomInstructions(e.target.value)}
            placeholder={t('Extra SOP constraints or verification rules (optional)...', '补充执行步骤或强制规范（可选）...')}
            className="w-full rounded-lg border border-slate-800 bg-slate-950 px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:border-indigo-500 focus:outline-none"
          />

          <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-300">
            <input
              type="checkbox"
              checked={useLlm}
              onChange={(e) => setUseLlm(e.target.checked)}
              className="rounded border-slate-600 bg-slate-950 text-indigo-500"
            />
            <span>
              {t(
                'Polish with active Ollama / OpenAI distiller (falls back to zero-cost rules)',
                '调用已配置的 Ollama / OpenAI 蒸馏器润色（未配置时自动零费用规则生成）'
              )}
            </span>
          </label>

          <button
            type="button"
            disabled={generating || (selectedSessionIds.length === 0 && selectedMemoryIds.length === 0)}
            onClick={() => void runGenerate(selectedSessionIds, selectedMemoryIds)}
            className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-xs font-semibold text-white shadow-sm hover:bg-indigo-500 disabled:opacity-50"
          >
            <Wand2 className="h-4 w-4" />
            <span>
              {generating
                ? t('Synthesizing SKILL.md...', '正在提炼生成 SKILL.md...')
                : t('Synthesize & Package Skill', '提炼并生成 Skill 草稿')}
            </span>
          </button>
        </div>
      </div>

      {/* Right: Preview / Edit + Export / Install */}
      <div className="flex min-h-0 flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40 lg:col-span-7">
        {!draftMd ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 p-10 text-center">
            <PackagePlus className="h-9 w-9 text-slate-600" />
            <p className="text-sm font-medium text-slate-300">
              <span>{t('Package Engineering Experience into SKILL.md', '将排坑与工程经验沉淀为可复用 Skill')}</span>
            </p>
            <p className="max-w-md text-xs text-slate-500">
              <span>
                {t(
                  'Pick solved conversations or central Memory cards on the left to generate a clean, redacted SKILL.md ready for ZIP export or one-click installation into Antigravity, Claude Code, Codex, and Cursor.',
                  '在左侧勾选 1 条或多条已解决问题的对话或中央记忆卡片，点击提炼生成标准 SKILL.md。支持在线微调、导出 .zip 分享包，以及一键写入本机各大 Coding Agent 技能目录。'
                )}
              </span>
            </p>
          </div>
        ) : (
          <>
            {/* Top Toolbar */}
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 bg-slate-900/70 px-4 py-3">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span
                  translate="no"
                  className="inline-flex items-center gap-1 rounded border border-indigo-500/40 bg-indigo-500/15 px-2 py-0.5 font-mono text-indigo-200"
                >
                  <FileCode2 className="h-3.5 w-3.5" />
                  {draftSlug}/SKILL.md
                </span>
                {draftMeta && (
                  <span translate="no" className="font-mono text-[11px] text-slate-400">
                    ~{draftMeta.token_estimate} tokens · {draftMeta.provider}
                  </span>
                )}
                <span className="inline-flex items-center gap-1 rounded border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-300">
                  <ShieldCheck className="h-3 w-3" />
                  <span>{t('Secrets Redacted', '已脱敏密钥')}</span>
                </span>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <div className="inline-flex rounded-lg border border-slate-800 bg-slate-950 p-0.5 text-xs">
                  <button
                    type="button"
                    onClick={() => setViewMode('rendered')}
                    className={`rounded-md px-2.5 py-1 font-medium ${
                      viewMode === 'rendered'
                        ? 'bg-slate-800 text-white'
                        : 'text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    <span>{t('Preview', '渲染预览')}</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setViewMode('edit')}
                    className={`inline-flex items-center gap-1 rounded-md px-2.5 py-1 font-medium ${
                      viewMode === 'edit'
                        ? 'bg-slate-800 text-white'
                        : 'text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    <Pencil className="h-3 w-3" />
                    <span>{t('Edit Raw', '编辑原文')}</span>
                  </button>
                </div>

                <CopyButton text={draftMd} label={t('Copy', '复制')} />

                <button
                  type="button"
                  disabled={saving}
                  onClick={() => void handleSave()}
                  className="inline-flex items-center gap-1 rounded-lg border border-slate-700 bg-slate-800 px-2.5 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-700 disabled:opacity-50"
                >
                  <Save className="h-3.5 w-3.5" />
                  <span>{saving ? t('Saving...', '保存中...') : t('Save', '保存到本地库')}</span>
                </button>

                <button
                  type="button"
                  disabled={exporting}
                  onClick={() => void handleExportZip()}
                  className="inline-flex items-center gap-1 rounded-lg border border-cyan-500/40 bg-cyan-500/15 px-2.5 py-1.5 text-xs font-medium text-cyan-200 hover:bg-cyan-500/25 disabled:opacity-50"
                >
                  <Download className="h-3.5 w-3.5" />
                  <span>{t('Export .zip', '导出 .zip')}</span>
                </button>

                <button
                  type="button"
                  onClick={() => setShowInstallPanel((prev) => !prev)}
                  className="inline-flex items-center gap-1 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500"
                >
                  <FolderCheck className="h-3.5 w-3.5" />
                  <span>{t('Install to Agent', '安装到 Agent')}</span>
                </button>
              </div>
            </div>

            {/* Install Target Selector Drawer */}
            {showInstallPanel && (
              <div className="border-b border-slate-800 bg-slate-950/90 px-4 py-3 text-xs">
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <span className="font-semibold text-slate-200">
                    {t('Select target Agent skill directories (.bak created on overwrite):', '选择要写入的本机 Agent 技能目录（覆盖前自动保留 .bak 备份）：')}
                  </span>
                  <button
                    type="button"
                    disabled={installing || installTargets.length === 0}
                    onClick={() => void handleInstall()}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1 font-semibold text-white hover:bg-indigo-500 disabled:opacity-50"
                  >
                    <Sparkles className="h-3.5 w-3.5" />
                    <span>{installing ? t('Installing...', '写入中...') : t('Confirm Install', '确认写入安装')}</span>
                  </button>
                </div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                  {INSTALL_TARGETS.map((tgt) => {
                    const active = installTargets.includes(tgt.id);
                    return (
                      <button
                        key={tgt.id}
                        type="button"
                        onClick={() => toggleTarget(tgt.id)}
                        className={`flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-left transition-colors ${
                          active
                            ? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-200'
                            : 'border-slate-800 bg-slate-900 text-slate-400 hover:border-slate-700'
                        }`}
                      >
                        {active ? (
                          <CheckSquare className="h-3.5 w-3.5 shrink-0 text-emerald-400" />
                        ) : (
                          <Square className="h-3.5 w-3.5 shrink-0 text-slate-600" />
                        )}
                        <div className="min-w-0">
                          <div translate="no" className="font-medium">
                            {tgt.label}
                          </div>
                          <div translate="no" className="truncate font-mono text-[10px] opacity-70">
                            {tgt.hint}
                          </div>
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {notice && (
              <div
                className={`border-b px-4 py-2 text-xs ${
                  notice.kind === 'ok'
                    ? 'border-emerald-500/30 bg-emerald-950/30 text-emerald-300'
                    : 'border-rose-500/30 bg-rose-950/30 text-rose-300'
                }`}
              >
                {notice.text}
              </div>
            )}

            {installResult && installResult.installed.length > 0 && (
              <div className="border-b border-slate-800 bg-slate-950/60 px-4 py-2 font-mono text-[11px] text-slate-300">
                {installResult.installed.map((entry) => (
                  <div key={`${entry.target}-${entry.path}`} className="truncate">
                    [{entry.target}] {entry.status}: {entry.path}
                    {entry.backup_path ? ` (backup: ${entry.backup_path})` : ''}
                  </div>
                ))}
              </div>
            )}

            {/* Body */}
            <div className="min-h-0 flex-1 overflow-y-auto p-5">
              {viewMode === 'edit' ? (
                <div className="flex h-full flex-col gap-3">
                  <div className="flex items-center gap-2">
                    <label className="text-xs font-medium text-slate-400">
                      <span>{t('Folder Slug:', '目录名 Slug:')}</span>
                    </label>
                    <input
                      type="text"
                      value={draftSlug}
                      onChange={(e) => setDraftSlug(e.target.value)}
                      className="rounded border border-slate-700 bg-slate-950 px-2.5 py-1 font-mono text-xs text-slate-200 focus:border-indigo-500 focus:outline-none"
                    />
                  </div>
                  <textarea
                    value={draftMd}
                    onChange={(e) => setDraftMd(e.target.value)}
                    className="min-h-[420px] w-full flex-1 rounded-lg border border-slate-800 bg-slate-950 p-4 font-mono text-xs leading-relaxed text-slate-200 focus:border-indigo-500 focus:outline-none"
                  />
                </div>
              ) : (
                <div className="space-y-4">
                  <div className="rounded-lg border border-slate-800 bg-slate-950/80 p-3 font-mono text-xs text-slate-300">
                    <div>
                      <span className="text-slate-500">name:</span>{' '}
                      <span className="text-indigo-300">{draftSlug}</span>
                    </div>
                    {draftMeta?.description && (
                      <div className="mt-1">
                        <span className="text-slate-500">description:</span>{' '}
                        <span className="text-emerald-300">{draftMeta.description}</span>
                      </div>
                    )}
                  </div>
                  <MarkdownContent content={previewBody || draftMd} />
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
