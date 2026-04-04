"""
Agentic Search & Entity Extraction Backend
==========================================
FastAPI backend orchestrating a 3-step pipeline with live SSE streaming:
  1. Search   – DuckDuckGo over-fetch (12 URLs) with CAPTCHA-wall exclusions
  2. Scrape   – Jina AI Reader API (JS rendering, bot-protection bypass)
  3. Extract  – Gemini 2.5 Flash with structured JSON + count enforcement
"""

import os
import asyncio
import logging
import json
import time
from typing import Optional, AsyncGenerator

import httpx

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from services import (
    search_web,
    fetch_with_retry,
    scrape_urls_async,
    format_scraped_results,
    filter_clean_urls,
)

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
    description="Search → Scrape → Extract pipeline powered by DuckDuckGo, Jina AI, and Gemini",
    version="2.0.0",
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
            "The EXACT SOURCE URL where this entity's information was found. "
            "Must match a URL from the SOURCE headers in the context — never fabricate."
        )
    )
    category: Optional[str] = Field(
        default=None,
        description="Category label (e.g. 'Restaurant', 'Database', 'Framework', 'Concept').",
    )
    ranking_rationale: str = Field(
        description=(
            "A concise 1-2 sentence explanation of why this entity was chosen and why it "
            "deserves its rank, citing specific signals from the provided text "
            "(e.g., 'Mentioned across 3 different local blogs,' "
            "'Highly recommended on Reddit for its wood-fired crust,' or "
            "'Listed as the #1 choice on the Chamber of Commerce site')."
        )
    )


class ExtractionResult(BaseModel):
    query: str = Field(description="The original user query.")
    entities: list[Entity] = Field(description="Extracted entities from the scraped sources.")
    total_sources_scraped: int = Field(description="Number of URLs successfully scraped.")


# ---------------------------------------------------------------------------
# LLM Extraction
# ---------------------------------------------------------------------------
MAX_TOTAL_CHARS = 24000  # Increased — Gemini 2.5 Flash handles large context well


def build_prompt(query: str, context: str) -> str:
    """
    Build the Gemini extraction prompt.

    Args:
        query:   The user's original search query.
        context: Pre-formatted labeled string from scrape_urls() / format_scraped_results().
    """
    # Truncate context if it exceeds the cap
    if len(context) > MAX_TOTAL_CHARS:
        context = context[:MAX_TOTAL_CHARS] + "\n[...truncated for context limit...]"

    return f"""You are an expert information extraction system. Your task is to analyze web content and extract structured entities.

USER QUERY: "{query}"

SCRAPED WEB SOURCES:
{context}

EXTRACTION RULES:
1. Analyze the provided context from multiple web pages. You MUST extract enough entities to satisfy the user's query (e.g., if they ask for "top 5", you must return exactly 5 distinct entities; if they ask for "top 10", return exactly 10). Do not duplicate entities. If there are more than requested available, pick the best and most relevant ones.
2. For EACH entity, the source_url MUST exactly match a URL that appears after "SOURCE:" in the context above — never invent or modify URLs.
3. key_features must be concrete and specific (e.g. actual dish names, opening hours, price range, cuisine type) — not generic statements like "high quality" or "great service".
4. For each entity you extract, you must provide a ranking_rationale. This should be a concise 1-2 sentence explanation of why this entity was chosen and why it deserves its rank, citing specific signals from the provided text (e.g., 'Mentioned across 3 different local blogs,' 'Highly recommended on Reddit for its wood-fired crust,' or 'Listed as the #1 choice on the Chamber of Commerce site').
5. Return ONLY valid JSON matching the required schema. No commentary outside the JSON.
"""


async def extract_entities(query: str, context: str) -> ExtractionResult:
    """Call Gemini 2.5 Flash with structured JSON output to extract entities."""
    prompt = build_prompt(query, context)
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
    logger.info(f"[EXTRACT] Gemini response length={len(raw)} chars")
    data = json.loads(raw)
    return ExtractionResult(**data)


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------
def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "scraper": "jina-ai", "model": "gemini-2.5-flash"}


@app.get("/api/research/stream")
async def research_stream(
    query: str = Query(..., min_length=3, description="Natural language research topic"),
):
    """
    SSE streaming endpoint. Emits typed events:
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
            loop = asyncio.get_event_loop()

            # ── Step 1: Search (over-fetch 12 URLs) ────────────────────────
            t0 = time.monotonic()
            raw_urls = await loop.run_in_executor(None, search_web, query)
            urls = filter_clean_urls(raw_urls)
            search_ms = int((time.monotonic() - t0) * 1000)

            if not urls:
                await queue.put(("error", {"message": "DuckDuckGo returned no results. Try a different query."}))
                return

            await queue.put(("search_done", {"urls": urls, "elapsed_ms": search_ms}))

            # ── Step 2: Scrape via Jina (concurrent, per-URL SSE events) ────
            t1 = time.monotonic()

            async def scrape_and_emit(url: str, client: httpx.AsyncClient):
                t = time.monotonic()
                result = await fetch_with_retry(client, url)
                elapsed = int((time.monotonic() - t) * 1000)
                status = "ok" if result else "skip"
                chars = len(result["content"]) if result else 0
                await queue.put(("scrape_url_done", {
                    "url": url,
                    "status": status,
                    "chars": chars,
                    "elapsed_ms": elapsed,
                }))
                return result

            async with httpx.AsyncClient() as http_client:
                raw_results = await asyncio.gather(
                    *[scrape_and_emit(u, http_client) for u in urls]
                )
            scraped_results = [r for r in raw_results if r is not None]

            scrape_ms = int((time.monotonic() - t1) * 1000)

            if not scraped_results:
                await queue.put(("error", {"message": "Jina AI could not extract content from any URL. Try a different query."}))
                return

            # Convert to labeled string for LLM
            context = format_scraped_results(scraped_results)
            await queue.put(("scrape_done", {"scraped_count": len(scraped_results), "elapsed_ms": scrape_ms}))

            # ── Step 3: Extract ─────────────────────────────────────────────
            t2 = time.monotonic()
            result = await extract_entities(query, context)
            extract_ms = int((time.monotonic() - t2) * 1000)
            result.total_sources_scraped = len(scraped_results)
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


# Blocking endpoint — useful for curl testing
@app.get("/api/research", response_model=ExtractionResult)
async def research(query: str = Query(..., min_length=3)):
    loop = asyncio.get_event_loop()

    raw_urls = await loop.run_in_executor(None, search_web, query)
    urls = filter_clean_urls(raw_urls)
    if not urls:
        raise HTTPException(status_code=502, detail="DuckDuckGo returned no results.")

    scraped = await scrape_urls_async(urls)
    if not scraped:
        raise HTTPException(status_code=502, detail="Jina AI could not extract content from any URL.")

    context = format_scraped_results(scraped)
    result = await extract_entities(query, context)
    result.total_sources_scraped = len(scraped)
    result.query = query
    return result


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
