import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertTriangle, RefreshCw, RotateCcw } from 'lucide-react';

interface ErrorBoundaryProps {
  children: ReactNode;
  resetKeys?: unknown[];
  fullScreen?: boolean;
  isZh?: boolean;
  onReset?: () => void;
}

interface ErrorBoundaryState {
  error: Error | null;
}

function keysEqual(a?: unknown[], b?: unknown[]): boolean {
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (!Object.is(a[i], b[i])) return false;
  }
  return true;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    if (typeof console !== 'undefined' && console.error) {
      console.error('[omnimex] UI boundary caught runtime error:', error, info.componentStack);
    }
  }

  componentDidUpdate(prevProps: ErrorBoundaryProps): void {
    if (this.state.error && !keysEqual(prevProps.resetKeys, this.props.resetKeys)) {
      this.setState({ error: null });
    }
  }

  private handleRecover = () => {
    this.setState({ error: null });
    this.props.onReset?.();
  };

  render() {
    const { error } = this.state;
    if (!error) {
      return this.props.children;
    }

    const isZh = Boolean(this.props.isZh);
    const msg = error.message || String(error);
    const looksLikeTranslateDomError =
      /removeChild|insertBefore|NotFoundError/i.test(msg);

    return (
      <div
        className={`flex items-center justify-center p-6 ${
          this.props.fullScreen ? 'min-h-screen bg-slate-950 text-slate-100' : 'h-full'
        }`}
      >
        <div className="w-full max-w-lg rounded-xl border border-rose-500/40 bg-slate-900/90 p-5 shadow-lg">
          <div className="mb-3 flex items-center gap-2.5 text-rose-300">
            <AlertTriangle className="h-5 w-5 shrink-0" />
            <h2 className="text-sm font-semibold">
              {isZh ? '视图渲染遇到异常（已安全拦截，避免白/蓝屏）' : 'View rendering error caught safely'}
            </h2>
          </div>

          <p className="mb-3 rounded-lg border border-slate-800 bg-slate-950/90 px-3 py-2 font-mono text-xs text-rose-200 break-words">
            {msg}
          </p>

          {looksLikeTranslateDomError && (
            <p className="mb-4 text-xs leading-relaxed text-amber-200/90">
              {isZh
                ? '提示：这通常是浏览器“整页翻译”改写了页面 DOM 节点导致的。建议关闭浏览器的自动网页翻译，改用顶栏自带的「中文 / EN」切换按钮。'
                : 'Tip: This typically happens when a browser page-translation extension mutates React DOM nodes. Consider disabling browser auto-translate and using the built-in 中 / EN switcher instead.'}
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={this.handleRecover}
              className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-indigo-500"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              {isZh ? '恢复视图' : 'Recover View'}
            </button>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:text-white"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              {isZh ? '刷新页面' : 'Reload Page'}
            </button>
          </div>
        </div>
      </div>
    );
  }
}
