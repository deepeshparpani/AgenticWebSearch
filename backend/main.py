"""
Agentic Search & Entity Extraction Backend
==========================================
FastAPI backend that orchestrates a 3-step pipeline with live SSE streaming:
  1. Search   – DuckDuckGo to collect top URLs for a query
  2. Scrape   – Trafilatura to extract clean text from each URL (with per-URL events)
  3. Extract  – Gemini 2.5 Flash with structured JSON output to identify entities
"""

import os
import asyncio
import logging
import json
import time
from typing import Optional, AsyncGenerator

import httpx
import trafilatura
from dotenv import load_dotenv
from ddgs import DDGS
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Config & Logging
# ---------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger("agentic_search")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set. Add it to your .env file.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Agentic Search API",
    description="Search → Scrape → Extract pipeline powered by DuckDuckGo, Trafilatura, and Gemini",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pydantic / JSON Schema Models
# ---------------------------------------------------------------------------
class Entity(BaseModel):
    name: str = Field(description="The canonical name or title of the entity.")
    description: str = Field(description="A concise 1-3 sentence description.")
    key_features: list[str] = Field(description="2-5 specific, notable features or capabilities.")
    source_url: str = Field(
        description=(
            "The EXACT URL from the provided sources. "
            "Must match one of the URLs in the context — never fabricate URLs."
        )
    )
    category: Optional[str] = Field(
        default=None,
        description="Category label (e.g. 'Database', 'Framework', 'Library', 'Concept').",
    )


class ExtractionResult(BaseModel):
    query: str = Field(description="The original user query.")
    entities: list[Entity] = Field(description="Extracted entities from the scraped sources.")
    total_sources_scraped: int = Field(description="Number of URLs successfully scraped.")


# ---------------------------------------------------------------------------
# Pipeline constants
# ---------------------------------------------------------------------------
MAX_URLS = 5
SCRAPE_TIMEOUT = 5
MAX_CHARS_PER_PAGE = 4000
MAX_TOTAL_CHARS = 16000

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# Core pipeline helpers
# ---------------------------------------------------------------------------
def _ddg_search(query: str, max_results: int) -> list[str]:
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
    return [r["href"] for r in results if "href" in r]


async def search_urls(query: str, max_results: int = MAX_URLS) -> list[str]:
    loop = asyncio.get_event_loop()
    urls = await loop.run_in_executor(None, _ddg_search, query, max_results)
    logger.info(f"[SEARCH] Found {len(urls)} URLs: {urls}")
    return urls


async def scrape_url(url: str, http_client: httpx.AsyncClient) -> Optional[str]:
    try:
        response = await http_client.get(url, timeout=SCRAPE_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        text = trafilatura.extract(
            response.text,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )
        return text[:MAX_CHARS_PER_PAGE] if text else None
    except Exception as exc:
        logger.warning(f"[SCRAPE] Failed {url}: {exc}")
        return None


def build_prompt(query: str, scraped: dict[str, str]) -> str:
    sources_block = ""
    total = 0
    for idx, (url, text) in enumerate(scraped.items(), 1):
        chunk = text[:MAX_CHARS_PER_PAGE]
        if total + len(chunk) > MAX_TOTAL_CHARS:
            chunk = chunk[: MAX_TOTAL_CHARS - total]
        sources_block += f"\n\n--- SOURCE {idx} ---\nURL: {url}\n\n{chunk}\n"
        total += len(chunk)
        if total >= MAX_TOTAL_CHARS:
            break

    return f"""You are an expert information extraction system. Analyze the provided web content and extract structured entities relevant to the user's query.

USER QUERY: "{query}"

SCRAPED WEB SOURCES:
{sources_block}

INSTRUCTIONS:
1. Identify all distinct, meaningful entities (tools, projects, companies, frameworks, concepts, etc.) relevant to the query.
2. For EACH entity, include the exact source_url from the SOURCE headers above — never invent URLs.
3. Extract between 5 and 15 high-quality entities.
4. key_features must be concrete and specific, not generic.
5. Return ONLY valid JSON matching the required schema.
"""


async def extract_entities(query: str, scraped: dict[str, str]) -> ExtractionResult:
    prompt = build_prompt(query, scraped)
    logger.info(f"[EXTRACT] Prompt length={len(prompt)} chars")
    loop = asyncio.get_event_loop()

    def _call():
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExtractionResult,
                temperature=0.2,
            ),
        )
        return response.text

    raw = await loop.run_in_executor(None, _call)
    data = json.loads(raw)
    return ExtractionResult(**data)


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------
def sse(event: str, data: dict) -> str:
    """Format a single SSE message."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/research/stream")
async def research_stream(
    query: str = Query(..., min_length=3, description="Natural language research topic"),
):
    """
    SSE streaming endpoint. Emits typed events at each pipeline stage:
      search_done      → { urls, elapsed_ms }
      scrape_url_done  → { url, status: 'ok'|'skip', chars, elapsed_ms }
      scrape_done      → { scraped_count, elapsed_ms }
      extract_done     → { elapsed_ms }
      done             → { result, timings }
      error            → { message }
    """
    queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

    async def pipeline():
        try:
            # ── Step 1: Search ──────────────────────────────────────────────
            t0 = time.monotonic()
            urls = await search_urls(query)
            search_ms = int((time.monotonic() - t0) * 1000)

            if not urls:
                await queue.put(("error", {"message": "DuckDuckGo returned no results. Try a different query."}))
                return

            await queue.put(("search_done", {"urls": urls, "elapsed_ms": search_ms}))

            # ── Step 2: Scrape (concurrent, per-URL events) ─────────────────
            t1 = time.monotonic()
            scraped: dict[str, str] = {}

            async def scrape_and_emit(url: str, client: httpx.AsyncClient):
                t = time.monotonic()
                result = await scrape_url(url, client)
                elapsed = int((time.monotonic() - t) * 1000)
                status = "ok" if result else "skip"
                chars = len(result) if result else 0
                await queue.put(("scrape_url_done", {
                    "url": url,
                    "status": status,
                    "chars": chars,
                    "elapsed_ms": elapsed,
                }))
                return url, result

            async with httpx.AsyncClient(headers=SCRAPE_HEADERS) as http_client:
                pairs = await asyncio.gather(
                    *[scrape_and_emit(url, http_client) for url in urls],
                    return_exceptions=True,
                )

            for item in pairs:
                if isinstance(item, tuple):
                    url, text = item
                    if isinstance(text, str) and text.strip():
                        scraped[url] = text

            scrape_ms = int((time.monotonic() - t1) * 1000)

            if not scraped:
                await queue.put(("error", {"message": "Could not extract content from any URL. Try a different query."}))
                return

            await queue.put(("scrape_done", {"scraped_count": len(scraped), "elapsed_ms": scrape_ms}))

            # ── Step 3: Extract ─────────────────────────────────────────────
            t2 = time.monotonic()
            result = await extract_entities(query, scraped)
            extract_ms = int((time.monotonic() - t2) * 1000)
            result.total_sources_scraped = len(scraped)
            result.query = query

            await queue.put(("extract_done", {"elapsed_ms": extract_ms}))
            await queue.put(("done", {
                "result": result.model_dump(),
                "timings": {
                    "search_ms": search_ms,
                    "scrape_ms": scrape_ms,
                    "extract_ms": extract_ms,
                    "total_ms": search_ms + scrape_ms + extract_ms,
                },
            }))

        except Exception as exc:
            logger.exception("[PIPELINE] Unexpected error")
            await queue.put(("error", {"message": str(exc)}))
        finally:
            await queue.put(None)  # sentinel

    async def generate() -> AsyncGenerator[str, None]:
        asyncio.create_task(pipeline())
        while True:
            item = await queue.get()
            if item is None:
                break
            event, data = item
            yield sse(event, data)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# Keep the original blocking endpoint for curl/testing convenience
@app.get("/api/research", response_model=ExtractionResult)
async def research(query: str = Query(..., min_length=3)):
    urls = await search_urls(query)
    if not urls:
        raise HTTPException(status_code=502, detail="DuckDuckGo returned no results.")
    scraped: dict[str, str] = {}
    async with httpx.AsyncClient(headers=SCRAPE_HEADERS) as http_client:
        results = await asyncio.gather(*[scrape_url(u, http_client) for u in urls], return_exceptions=True)
    for url, text in zip(urls, results):
        if isinstance(text, str) and text.strip():
            scraped[url] = text
    if not scraped:
        raise HTTPException(status_code=502, detail="Could not extract content from any URL.")
    result = await extract_entities(query, scraped)
    result.total_sources_scraped = len(scraped)
    result.query = query
    return result


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
