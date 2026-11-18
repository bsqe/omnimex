import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';

export type Locale = 'en' | 'zh';

const LOCALE_STORAGE_KEY = 'omnimex.locale';

interface LocaleContextValue {
  locale: Locale;
  isZh: boolean;
  setLocale: (next: Locale) => void;
  toggleLocale: () => void;
  t: (en: string, zh: string) => string;
}

const DEFAULT_VALUE: LocaleContextValue = {
  locale: 'en',
  isZh: false,
  setLocale: () => {},
  toggleLocale: () => {},
  t: (en) => en,
};

const LocaleContext = createContext<LocaleContextValue>(DEFAULT_VALUE);

function isVitestRuntime(): boolean {
  try {
    return Boolean((import.meta as { env?: { VITEST?: unknown } }).env?.VITEST);
  } catch {
    return false;
  }
}

export function resolveInitialLocale(navigatorLanguage?: string, allowBrowserDetect = !isVitestRuntime()): Locale {
  try {
    if (typeof window !== 'undefined' && window.localStorage) {
      const saved = window.localStorage.getItem(LOCALE_STORAGE_KEY);
      if (saved === 'zh' || saved === 'en') {
        return saved;
      }
    }
  } catch {
    // Ignore storage read failures in restricted contexts
  }

  if (allowBrowserDetect) {
    const lang =
      navigatorLanguage ?? (typeof navigator !== 'undefined' ? navigator.language || '' : '');
    if (/^zh\b/i.test(lang)) {
      return 'zh';
    }
  }
  return 'en';
}

export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => resolveInitialLocale());

  useEffect(() => {
    try {
      if (typeof document !== 'undefined') {
        document.documentElement.lang = locale === 'zh' ? 'zh-CN' : 'en';
        document.title =
          locale === 'zh'
            ? 'Omnimex — 跨 Agent 记忆与会话中心'
            : 'Omnimex — Cross-Agent Memory & Session Hub';
      }
      if (typeof window !== 'undefined' && window.localStorage) {
        window.localStorage.setItem(LOCALE_STORAGE_KEY, locale);
      }
    } catch {
      // Ignore storage write failures
    }
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
  }, []);

  const toggleLocale = useCallback(() => {
    setLocaleState((prev) => (prev === 'en' ? 'zh' : 'en'));
  }, []);

  const t = useCallback((en: string, zh: string) => (locale === 'zh' ? zh : en), [locale]);

  return (
    <LocaleContext.Provider
      value={{
        locale,
        isZh: locale === 'zh',
        setLocale,
        toggleLocale,
        t,
      }}
    >
      {children}
    </LocaleContext.Provider>
  );
}

export function useLocale(): LocaleContextValue {
  return useContext(LocaleContext);
}
