const EXAMPLE_QUERIES = [
  'open source database tools',
  'vector search frameworks 2024',
  'LLM inference optimization',
];

function SearchIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.35-4.35" />
    </svg>
  );
}

function ArrowIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 12h14M12 5l7 7-7 7" />
    </svg>
  );
}

export default function SearchBar({ value, onChange, onSubmit, isLoading }) {
  const handleSubmit = (e) => {
    e.preventDefault();
    onSubmit(value);
  };

  return (
    <div className="search-container">
      <form className="search-form" onSubmit={handleSubmit}>
        <div className="search-input-wrapper">
          <span className="search-icon">
            <SearchIcon />
          </span>
          <input
            id="search-input"
            type="text"
            className="search-input"
            placeholder='e.g. "open source vector databases"'
            value={value}
            onChange={(e) => onChange(e.target.value)}
            disabled={isLoading}
            autoComplete="off"
            spellCheck={false}
          />
        </div>
        <button
          id="search-btn"
          type="submit"
          className="search-btn"
          disabled={isLoading || !value.trim()}
        >
          {isLoading ? (
            <>
              <span className="step-spinner" style={{ width: 14, height: 14 }} />
              Researching…
            </>
          ) : (
            <>
              Research
              <ArrowIcon />
            </>
          )}
        </button>
      </form>

      <div className="example-queries">
        <span className="example-label">Try:</span>
        {EXAMPLE_QUERIES.map((q) => (
          <button
            key={q}
            className="example-chip"
            onClick={() => {
              onChange(q);
              onSubmit(q);
            }}
            disabled={isLoading}
          >
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}
