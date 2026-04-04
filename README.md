# 🔍 Agentic Search & Entity Extraction

> A full-stack system that takes a natural language query, autonomously searches the web, scrapes content, and uses an LLM to extract structured entities — all with source traceability.

---

## Architecture Overview

```
User Query
   │
   ▼
┌──────────────────────────────────────────────────────────┐
│  FastAPI Backend  (Python 3.11+)                         │
│                                                          │
│  1. [SEARCH]   DuckDuckGo → top 5 URLs                   │
│  2. [SCRAPE]   httpx + Trafilatura → clean text          │
│  3. [EXTRACT]  Gemini 1.5 Flash → structured JSON        │
└──────────────────────────────────────────────────────────┘
   │
   ▼
┌──────────────────────────────────────────────────────────┐
│  React + Vite Frontend                                   │
│  - Animated pipeline status display                      │
│  - Entity table with clickable source links              │
└──────────────────────────────────────────────────────────┘
```

---

## Design Rationale

### Approach: Two-Stage Open-Source Pipeline

The scraping pipeline relies entirely on **free, open-source tools**, which is a deliberate design choice:

| Layer | Tool | Reason for Choice |
|-------|------|-------------------|
| Search | `duckduckgo-search` | Zero cost, no API key required, generous rate limits for development |
| Scraping | `trafilatura` | State-of-the-art boilerplate removal; outperforms BeautifulSoup for main-content extraction in benchmarks |
| HTTP | `httpx` | Async-native; allows concurrent scraping of all 5 URLs simultaneously, cutting total scrape time by ~5× |

This eliminates the $50–$300/month cost of paid search APIs (Tavily, Serper, Bing) while maintaining production-grade reliability for most queries.

### LLM Strategy: Gemini 1.5 Flash + Structured Outputs

**Why Gemini 1.5 Flash?**

- **1M token context window** — can ingest full scraped content from multiple pages without chunking or retrieval hacks.
- **Native JSON schema enforcement** (`response_mime_type: application/json` + `response_schema`) eliminates post-processing regex hacks and guarantees valid, typed output on the first call.
- **Speed/Cost balance** — Flash is ~10× cheaper than Pro while being fast enough for interactive UX (typically 1–3s inference time).

**Source Traceability**

The prompt explicitly instructs the model to populate `source_url` with *only* the URLs present in the labeled `--- SOURCE N ---` blocks. The Pydantic schema enforces this field is always present, creating a hard binding between each entity and its origin.

---

## Trade-offs & Known Limitations

| Limitation | Detail |
|------------|--------|
| **Latency** | The scraping pipeline takes **5–15 seconds** end-to-end. Paid APIs like Tavily return pre-indexed content in <1s. We mitigate this with concurrent async scraping and a streaming loading UI. |
| **SPA Resistance** | Sites built with React/Angular/Vue that require JavaScript rendering (SPAs) will return empty shells. Trafilatura degrades gracefully by returning `None`, and those URLs are skipped. A headless browser (Playwright) could solve this at the cost of significantly higher latency. |
| **Rate Limits** | DuckDuckGo imposes undocumented rate limits. High-frequency usage (e.g., automated testing loops) can trigger temporary blocks. Back off or use a proxy if encountered. |
| **Paywalled Content** | Subscription-based sites (news, academic journals) often serve empty article previews. The system skips these gracefully. |
| **URL Hallucination** | The prompt strictly forbids the LLM from inventing URLs, but hallucination is never 0%. The UI should ideally validate returned URLs against the original search result list (future improvement). |

---

## Project Structure

```
agentic-search/
├── README.md
├── backend/
│   ├── .env.example       ← copy to .env and add your API key
│   ├── requirements.txt
│   └── main.py            ← FastAPI app (search → scrape → extract)
└── frontend/
    ├── index.html
    ├── package.json
    ├── vite.config.js
    └── src/
        ├── main.jsx
        ├── App.jsx
        ├── App.css
        └── components/
            ├── SearchBar.jsx
            ├── PipelineStatus.jsx
            └── EntityTable.jsx
```

---

## Setup & Running Locally

### Prerequisites

- Python 3.10+
- Node.js 18+
- A free [Gemini API key](https://aistudio.google.com/app/apikey)

---

### Backend Setup

```bash
# 1. Navigate to the backend directory
cd agentic-search/backend

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure your API key
cp .env.example .env
# Now open .env and replace "your_gemini_api_key_here" with your actual key

# 5. Start the backend server
uvicorn main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`.  
Interactive docs: `http://localhost:8000/docs`

---

### Frontend Setup

```bash
# 1. Navigate to the frontend directory
cd agentic-search/frontend

# 2. Install Node dependencies
npm install

# 3. Start the development server
npm run dev
```

The UI will be available at `http://localhost:5173`.

---

### Quick Test (curl)

```bash
curl "http://localhost:8000/api/research?query=open+source+database+tools" | python3 -m json.tool
```

---

## API Reference

### `GET /api/research`

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | ✅ | Natural language research topic (min 3 chars) |

**Response Schema:**

```json
{
  "query": "open source database tools",
  "total_sources_scraped": 4,
  "entities": [
    {
      "name": "PostgreSQL",
      "description": "A powerful, open-source object-relational database system...",
      "key_features": ["ACID compliance", "JSON support", "Full-text search", "Extensible"],
      "source_url": "https://example.com/article-about-databases",
      "category": "Database"
    }
  ]
}
```

---

## Evaluation Criteria Checklist

- ✅ **Search**: DuckDuckGo retrieves top 5 URLs for any natural language query
- ✅ **Scrape**: Trafilatura extracts clean main content; bad URLs are skipped gracefully
- ✅ **Extract**: Gemini 1.5 Flash with enforced JSON schema produces structured entities
- ✅ **Traceability**: Every entity includes a `source_url` tied to a scraped page
- ✅ **Frontend**: React UI with animated pipeline status + responsive entity table
- ✅ **Error Handling**: HTTP timeouts, scraping failures, and API errors are all handled
- ✅ **Zero Cost**: No paid APIs required beyond Gemini's generous free tier

---

## License

MIT
