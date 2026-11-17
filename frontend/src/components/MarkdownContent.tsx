import { Fragment, memo, useMemo, useState } from 'react';
import { CopyButton } from './CopyButton';

const HEAVY_CONTENT_CHAR_LIMIT = 8000;
const HEAVY_CONTENT_LINE_LIMIT = 220;

interface MarkdownContentProps {
  content: string;
  className?: string;
}

type Segment =
  | { kind: 'text'; text: string }
  | { kind: 'code'; lang: string; code: string };

function splitFencedCode(input: string): Segment[] {
  const segments: Segment[] = [];
  const regex = /```([a-zA-Z0-9_-]*)\r?\n([\s\S]*?)```/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(input)) !== null) {
    if (match.index > lastIndex) {
      segments.push({ kind: 'text', text: input.slice(lastIndex, match.index) });
    }
    segments.push({
      kind: 'code',
      lang: match[1] || 'text',
      code: match[2].replace(/\n$/, ''),
    });
    lastIndex = regex.lastIndex;
  }

  if (lastIndex < input.length) {
    segments.push({ kind: 'text', text: input.slice(lastIndex) });
  }
  return segments;
}

function renderInline(line: string) {
  // Split inline code (`...`) and bold (**...**) cleanly without unsafe HTML injection
  const parts = line.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
  return parts.map((part, i) => {
    if (part.startsWith('`') && part.endsWith('`') && part.length >= 2) {
      return (
        <code
          key={i}
          className="rounded bg-slate-800/90 px-1.5 py-0.5 font-mono text-[0.85em] text-indigo-200 ring-1 ring-slate-700/60"
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    if (part.startsWith('**') && part.endsWith('**') && part.length >= 4) {
      return (
        <strong key={i} className="font-semibold text-slate-100">
          {part.slice(2, -2)}
        </strong>
      );
    }
    return <Fragment key={i}>{part}</Fragment>;
  });
}

function renderTextBlock(text: string, blockKey: number) {
  const lines = text.split(/\r?\n/);
  return (
    <div key={blockKey} className="space-y-1.5 text-sm leading-relaxed text-slate-200">
      {lines.map((line, idx) => {
        const trimmed = line.trim();
        if (!trimmed) return <div key={idx} className="h-1.5" />;

        if (trimmed.startsWith('### ')) {
          return (
            <h4 key={idx} className="pt-2 text-xs font-semibold uppercase tracking-wider text-slate-300">
              {renderInline(trimmed.slice(4))}
            </h4>
          );
        }
        if (trimmed.startsWith('## ')) {
          return (
            <h3 key={idx} className="pt-2 text-sm font-semibold text-slate-100">
              {renderInline(trimmed.slice(3))}
            </h3>
          );
        }
        if (trimmed.startsWith('# ')) {
          return (
            <h2 key={idx} className="pt-1 text-base font-semibold text-white">
              {renderInline(trimmed.slice(2))}
            </h2>
          );
        }
        if (trimmed.startsWith('- [ ] ') || trimmed.startsWith('- [x] ')) {
          const checked = trimmed.startsWith('- [x] ');
          return (
            <div key={idx} className="flex items-start gap-2 pl-1">
              <span
                className={`mt-1 inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border text-[10px] ${
                  checked
                    ? 'border-emerald-500/60 bg-emerald-500/20 text-emerald-300'
                    : 'border-slate-600 bg-slate-900 text-transparent'
                }`}
              >
                ✓
              </span>
              <span>{renderInline(trimmed.slice(6))}</span>
            </div>
          );
        }
        if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
          return (
            <div key={idx} className="flex items-start gap-2 pl-1">
              <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-indigo-400/80" />
              <span>{renderInline(trimmed.slice(2))}</span>
            </div>
          );
        }
        return <p key={idx}>{renderInline(line)}</p>;
      })}
    </div>
  );
}

export const MarkdownContent = memo(function MarkdownContent({
  content,
  className = '',
}: MarkdownContentProps) {
  const [forceRich, setForceRich] = useState(false);

  const isHeavy = useMemo(() => {
    if (content.length > HEAVY_CONTENT_CHAR_LIMIT) return true;
    let lines = 1;
    for (let i = 0; i < content.length; i++) {
      if (content.charCodeAt(i) === 10) {
        lines += 1;
        if (lines > HEAVY_CONTENT_LINE_LIMIT) return true;
      }
    }
    return false;
  }, [content]);

  const segments = useMemo(
    () => (isHeavy && !forceRich ? [] : splitFencedCode(content)),
    [content, isHeavy, forceRich]
  );

  if (isHeavy && !forceRich) {
    return (
      <div className={`space-y-2 ${className}`}>
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-900/70 px-3 py-1.5 text-[11px] text-slate-400">
          <span>
            Large block ({content.length.toLocaleString()} chars) rendered in fast plain mode
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setForceRich(true)}
              className="rounded border border-slate-700 bg-slate-800 px-2 py-0.5 text-[11px] text-slate-200 hover:border-indigo-500"
            >
              Render rich Markdown
            </button>
            <CopyButton text={content} label="Copy text" />
          </div>
        </div>
        <pre className="max-h-96 overflow-auto rounded-lg border border-slate-800 bg-slate-950/90 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-slate-200">
          {content}
        </pre>
      </div>
    );
  }

  return (
    <div className={`space-y-3 ${className}`}>
      {segments.map((seg, i) => {
        if (seg.kind === 'text') {
          return renderTextBlock(seg.text, i);
        }
        return (
          <div
            key={i}
            className="overflow-hidden rounded-lg border border-slate-800 bg-slate-950/90 shadow-inner"
          >
            <div className="flex items-center justify-between border-b border-slate-800/80 bg-slate-900/70 px-3 py-1.5">
              <span className="font-mono text-[11px] uppercase tracking-wider text-slate-400">
                {seg.lang}
              </span>
              <CopyButton text={seg.code} label="Copy code" />
            </div>
            <pre className="overflow-x-auto p-3 font-mono text-xs leading-relaxed text-slate-200">
              <code>{seg.code}</code>
            </pre>
          </div>
        );
      })}
    </div>
  );
});
