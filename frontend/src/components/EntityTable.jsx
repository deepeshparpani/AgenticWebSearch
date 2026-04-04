function ExternalLinkIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </svg>
  );
}

function truncateUrl(url) {
  try {
    const u = new URL(url);
    const path = u.pathname.length > 20 ? u.pathname.slice(0, 20) + '…' : u.pathname;
    return u.hostname + path;
  } catch {
    return url.slice(0, 40) + (url.length > 40 ? '…' : '');
  }
}

export default function EntityTable({ entities }) {
  if (!entities || entities.length === 0) {
    return (
      <div className="table-wrapper" style={{ padding: '48px', textAlign: 'center', color: 'var(--text-muted)' }}>
        No entities were extracted. Try a more specific query.
      </div>
    );
  }

  return (
    <div className="table-wrapper">
      <table className="entity-table">
        <thead>
          <tr>
            <th>Entity</th>
            <th>Description</th>
            <th>Key Features</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {entities.map((entity, idx) => (
            <tr
              key={`${entity.name}-${idx}`}
              style={{ animationDelay: `${idx * 60}ms` }}
            >
              {/* Entity Name + Category */}
              <td className="entity-name-cell">
                <div className="entity-name">{entity.name}</div>
                {entity.category && (
                  <span className="entity-category-badge">{entity.category}</span>
                )}
              </td>

              {/* Description */}
              <td>
                <div className="entity-description">{entity.description}</div>
              </td>

              {/* Key Features */}
              <td>
                <ul className="features-list">
                  {(entity.key_features || []).map((feat, fi) => (
                    <li key={fi} className="feature-tag">{feat}</li>
                  ))}
                </ul>
              </td>

              {/* Source URL */}
              <td>
                <a
                  href={entity.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="source-link"
                  title={entity.source_url}
                >
                  <ExternalLinkIcon />
                  {truncateUrl(entity.source_url)}
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
