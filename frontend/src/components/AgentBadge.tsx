import { AGENT_META } from '../api/mockData';
import type { AgentId } from '../api/types';

interface AgentBadgeProps {
  agent: AgentId;
  compact?: boolean;
}

export function AgentBadge({ agent, compact = false }: AgentBadgeProps) {
  const meta = AGENT_META[agent] ?? {
    label: agent,
    short: agent.toUpperCase(),
    colorClass: 'bg-slate-700/40 text-slate-300 border-slate-600/40',
    dotClass: 'bg-slate-400',
    borderClass: 'border-slate-600/40',
  };

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium tracking-tight ${meta.colorClass}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${meta.dotClass}`} />
      <span>{compact ? meta.short : meta.label}</span>
    </span>
  );
}
