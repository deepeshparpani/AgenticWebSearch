const STAGE_ORDER = ['search', 'scrape', 'extract'];

const STEP_META = {
  search: {
    icon: '🔍',
    name: 'Searching the Web',
    pendingDesc: 'Querying DuckDuckGo for the top 5 relevant URLs…',
  },
  scrape: {
    icon: '🕸️',
    name: 'Scraping Sources',
    pendingDesc: 'Fetching & extracting main content from each URL…',
  },
  extract: {
    icon: '✨',
    name: 'Extracting Entities',
    pendingDesc: 'Sending context to Gemini 2.5 Flash for structured extraction…',
  },
};

function getStatus(stepId, currentStage) {
  if (!currentStage || currentStage === 'error') return 'idle';
  const cur = STAGE_ORDER.indexOf(currentStage);
  const own = STAGE_ORDER.indexOf(stepId);
  if (currentStage === 'done') return 'done';
  if (own < cur) return 'done';
  if (own === cur) return 'active';
  return 'idle';
}

function ms(val) {
  if (val == null) return null;
  return val >= 1000 ? `${(val / 1000).toFixed(1)}s` : `${val}ms`;
}

function truncateHost(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url.slice(0, 30);
  }
}

// ── Search step detail ───────────────────────────────────────────────────────
function SearchDetail({ urls, timing }) {
  if (!urls.length) return null;
  return (
    <div className="step-detail">
      <div className="step-detail-label">
        {urls.length} URL{urls.length !== 1 ? 's' : ''} found
        {timing && <span className="timing-badge">{ms(timing)}</span>}
      </div>
      <ul className="url-list">
        {urls.map((u) => (
          <li key={u} className="url-item">
            <span className="url-dot" />
            <a href={u} target="_blank" rel="noopener noreferrer" className="url-link">
              {truncateHost(u)}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ── Scrape step detail ───────────────────────────────────────────────────────
function ScrapeDetail({ items, allUrls, timing }) {
  if (!items.length && !allUrls.length) return null;

  // Show pending placeholders for URLs not yet scraped
  const seen = new Set(items.map((i) => i.url));
  const pending = allUrls.filter((u) => !seen.has(u));

  return (
    <div className="step-detail">
      <div className="step-detail-label">
        {items.filter((i) => i.status === 'ok').length}/{allUrls.length} scraped successfully
        {timing && <span className="timing-badge">{ms(timing)}</span>}
      </div>
      <ul className="url-list">
        {items.map((item) => (
          <li key={item.url} className="url-item">
            <span className={`scrape-status-icon ${item.status}`}>
              {item.status === 'ok' ? '✓' : '✗'}
            </span>
            <a href={item.url} target="_blank" rel="noopener noreferrer" className="url-link">
              {truncateHost(item.url)}
            </a>
            {item.status === 'ok' && (
              <span className="url-meta">{(item.chars / 1000).toFixed(1)}k chars · {ms(item.elapsed_ms)}</span>
            )}
            {item.status === 'skip' && (
              <span className="url-meta url-meta-skip">blocked / empty</span>
            )}
          </li>
        ))}
        {pending.map((u) => (
          <li key={u} className="url-item url-item-pending">
            <span className="mini-spinner" />
            <span className="url-link url-link-muted">{truncateHost(u)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ── Extract step detail ──────────────────────────────────────────────────────
function ExtractDetail({ timing }) {
  if (!timing) return null;
  return (
    <div className="step-detail">
      <div className="step-detail-label">
        Structured JSON received
        <span className="timing-badge">{ms(timing)}</span>
      </div>
    </div>
  );
}

// ── Progress bar ─────────────────────────────────────────────────────────────
function progressPct(stage, scrapeItems, urlCount) {
  if (!stage) return 0;
  if (stage === 'done') return 100;
  if (stage === 'search') return 5;
  if (stage === 'scrape') {
    // advance proportionally as URLs complete
    const base = 33;
    const scraped = scrapeItems.length;
    const total = urlCount || 1;
    return base + Math.round((scraped / total) * 34);
  }
  if (stage === 'extract') return 70;
  return 0;
}

// ── Main component ────────────────────────────────────────────────────────────
export default function PipelineStatus({ stage, urls = [], scrapeItems = [], timings = {} }) {
  const pct = progressPct(stage, scrapeItems, urls.length);

  return (
    <div className="pipeline-container">
      <div className="pipeline-title">
        <span>⚡</span>
        Agentic Pipeline
        {stage === 'done' && <span className="pipeline-done-badge">Complete</span>}
      </div>

      <div className="pipeline-steps">
        {STAGE_ORDER.map((stepId) => {
          const meta = STEP_META[stepId];
          const status = getStatus(stepId, stage);

          return (
            <div key={stepId} className={`pipeline-step ${status}`}>
              <div className={`step-icon ${status}`}>
                {status === 'done' ? '✓' : meta.icon}
              </div>

              <div className="step-info">
                <div className="step-name-row">
                  <span className="step-name">{meta.name}</span>
                  {status === 'done' && timings?.[`${stepId}_ms`] && (
                    <span className="step-elapsed">{ms(timings[`${stepId}_ms`])}</span>
                  )}
                </div>

                {status === 'idle' && (
                  <div className="step-desc">{meta.pendingDesc}</div>
                )}

                {status === 'active' && stepId !== 'search' && (
                  <div className="step-desc">{meta.pendingDesc}</div>
                )}

                {/* Per-step live detail */}
                {(status === 'active' || status === 'done') && stepId === 'search' && (
                  <SearchDetail urls={urls} timing={status === 'done' ? timings?.search_ms : null} />
                )}
                {(status === 'active' || status === 'done') && stepId === 'scrape' && (
                  <ScrapeDetail
                    items={scrapeItems}
                    allUrls={urls}
                    timing={status === 'done' ? timings?.scrape_ms : null}
                  />
                )}
                {status === 'done' && stepId === 'extract' && (
                  <ExtractDetail timing={timings?.extract_ms} />
                )}
              </div>

              {status === 'active' && <div className="step-spinner" />}
            </div>
          );
        })}
      </div>

      <div className="pipeline-progress">
        <div className="pipeline-progress-bar" style={{ width: `${pct}%` }} />
      </div>

      {stage === 'done' && timings?.total_ms && (
        <div className="pipeline-total-time">
          Total pipeline time: <strong>{ms(timings.total_ms)}</strong>
        </div>
      )}
    </div>
  );
}
