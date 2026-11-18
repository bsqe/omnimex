import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.tsx';
import { ErrorBoundary } from './components/ErrorBoundary';
import './index.css';
import { installTranslateDomPatch } from './utils/translateGuard';

installTranslateDomPatch();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary fullScreen>
      <App />
    </ErrorBoundary>
  </StrictMode>
);
