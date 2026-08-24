import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './index.css';

// --- Reload diagnostics ---
// Log every page unload/reload so we can see what triggers the white-screen
// refreshes in the browser console.
window.addEventListener('beforeunload', () => {
  console.warn('[ReloadDiag] beforeunload fired — page is about to reload');
});

// Catch unhandled errors that could crash the React tree and cause a blank page.
window.addEventListener('error', (event) => {
  console.error('[ReloadDiag] Uncaught error:', event.error ?? event.message, event.filename, event.lineno);
});
window.addEventListener('unhandledrejection', (event) => {
  console.error('[ReloadDiag] Unhandled promise rejection:', event.reason);
});

// Log navigation entries so we can see if the reload is a real navigation
// (location.reload / redirect) vs a React re-render.
if (typeof PerformanceObserver !== 'undefined') {
  try {
    const navObserver = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (entry.entryType === 'navigation') {
          const nav = entry as PerformanceNavigationTiming;
          console.warn('[ReloadDiag] Navigation detected:', {
            type: nav.type, // 'reload' | 'navigate' | 'back_forward' | 'prerender'
            startTime: Math.round(nav.startTime),
            duration: Math.round(nav.duration),
            url: nav.name,
          });
        }
      }
    });
    navObserver.observe({ type: 'navigation', buffered: true });
  } catch { /* older browsers */ }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);