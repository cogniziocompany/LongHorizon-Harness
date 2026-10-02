import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { UiLanguageProvider } from './i18n';
import './style.css';

/** Keep a render failure visible and recoverable instead of a blank page. */
class CrashBoundary extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="crash-screen" role="alert">
        <h1>The workbench hit an error</h1>
        <p>Your runs are not affected. Reload to reconnect.</p>
        <pre>{String(this.state.error.message || this.state.error)}</pre>
        <button type="button" onClick={() => window.location.reload()}>Reload</button>
      </div>
    );
  }
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode><CrashBoundary><UiLanguageProvider><App /></UiLanguageProvider></CrashBoundary></React.StrictMode>,
);
