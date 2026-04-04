import { useState, useRef } from 'react';
import './App.css';
import SearchBar from './components/SearchBar';
import PipelineStatus from './components/PipelineStatus';
import EntityTable from './components/EntityTable';

/**
 * SSE event-driven state shape:
 *  stage: null | 'search' | 'scrape' | 'extract' | 'done' | 'error'
 *  urls:  string[]                  — from search_done
 *  scrapeItems: { url, status, chars, elapsed_ms }[]  — from scrape_url_done
 *  timings: { search_ms, scrape_ms, extract_ms, total_ms }
 *  result: ExtractionResult | null
 *  error:  string | null
 */
const INITIAL = {
  stage: null,
  urls: [],
  scrapeItems: [],
  timings: null,
  result: null,
  error: null,
};

export default function App() {
  const [query, setQuery] = useState('');
  const [state, setState] = useState(INITIAL);
  const esRef = useRef(null);

  const handleSearch = (searchQuery) => {
    if (!searchQuery.trim()) return;

    // Close any previous SSE stream
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }

    // Reset to initial state, begin pipeline
    setState({ ...INITIAL, stage: 'search' });

    const url = `http://localhost:8000/api/research/stream?query=${encodeURIComponent(searchQuery)}`;
    const es = new EventSource(url);
    esRef.current = es;

    es.addEventListener('search_done', (e) => {
      const { urls, elapsed_ms } = JSON.parse(e.data);
      setState((prev) => ({
        ...prev,
        stage: 'scrape',
        urls,
        timings: { search_ms: elapsed_ms },
      }));
    });

    es.addEventListener('scrape_url_done', (e) => {
      const item = JSON.parse(e.data);
      setState((prev) => ({
        ...prev,
        scrapeItems: [...prev.scrapeItems, item],
      }));
    });

    es.addEventListener('scrape_done', (e) => {
      const { elapsed_ms } = JSON.parse(e.data);
      setState((prev) => ({
        ...prev,
        stage: 'extract',
        timings: { ...prev.timings, scrape_ms: elapsed_ms },
      }));
    });

    es.addEventListener('extract_done', (e) => {
      const { elapsed_ms } = JSON.parse(e.data);
      setState((prev) => ({
        ...prev,
        timings: { ...prev.timings, extract_ms: elapsed_ms },
      }));
    });

    es.addEventListener('done', (e) => {
      const { result, timings } = JSON.parse(e.data);
      setState((prev) => ({
        ...prev,
        stage: 'done',
        result,
        timings,
      }));
      es.close();
    });

    es.addEventListener('error', (e) => {
      // SSE spec fires this both for server errors AND connection close
      try {
        const { message } = JSON.parse(e.data);
        setState((prev) => ({ ...prev, stage: 'error', error: message }));
      } catch {
        if (es.readyState === EventSource.CLOSED) return; // normal close
        setState((prev) => ({
          ...prev,
          stage: 'error',
          error: 'Connection to backend lost. Is the server running?',
        }));
      }
      es.close();
    });
  };

  const isLoading = state.stage && !['done', 'error'].includes(state.stage);

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-badge">
          <span className="dot" />
          Agentic Research Pipeline
        </div>
        <h1 className="app-title">
          Search. Scrape. <span className="gradient-text">Extract.</span>
        </h1>
        <p className="app-subtitle">
          Enter any research topic and our AI agent will autonomously search the web,
          scrape live sources, and extract structured entities with full traceability.
        </p>
      </header>

      <main>
        <section className="search-section">
          <SearchBar
            value={query}
            onChange={setQuery}
            onSubmit={handleSearch}
            isLoading={isLoading}
          />
        </section>

        {(isLoading || state.stage === 'done') && (
          <PipelineStatus
            stage={state.stage}
            urls={state.urls}
            scrapeItems={state.scrapeItems}
            timings={state.timings}
          />
        )}

        {state.stage === 'error' && state.error && (
          <div className="error-container">
            <span className="error-icon">⚠️</span>
            <div>
              <div className="error-title">Pipeline Error</div>
              <div className="error-message">{state.error}</div>
            </div>
          </div>
        )}

        {state.stage === 'done' && state.result && (
          <section className="results-section">
            <div className="results-header">
              <h2 className="results-title">Extracted Entities</h2>
              <div className="results-meta">
                <span className="meta-badge">🔍 "{state.result.query}"</span>
                <span className="meta-badge success">
                  ✓ {state.result.total_sources_scraped} sources scraped
                </span>
                <span className="meta-badge">
                  📦 {state.result.entities.length} entities
                </span>
                {state.timings?.total_ms && (
                  <span className="meta-badge">
                    ⏱ {(state.timings.total_ms / 1000).toFixed(1)}s total
                  </span>
                )}
              </div>
            </div>
            <EntityTable entities={state.result.entities} />
          </section>
        )}

        {!state.stage && (
          <div className="empty-state">
            <div className="empty-state-icon">🔎</div>
            <div className="empty-state-title">No results yet</div>
            <div className="empty-state-subtitle">
              Enter a topic above to start your agentic research session
            </div>
          </div>
        )}
      </main>

      <footer className="app-footer">
        <span className="footer-item">🔍 DuckDuckGo Search</span>
        <span className="footer-item">🕸️ Trafilatura Scraper</span>
        <span className="footer-item">✨ Gemini 2.5 Flash</span>
      </footer>
    </div>
  );
}
