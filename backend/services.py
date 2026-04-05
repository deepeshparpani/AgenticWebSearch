"""
services.py — Search and Scraping services
===========================================
Provides:
  - search_web()          DuckDuckGo over-fetch with CAPTCHA-wall exclusions
  - fetch_with_retry()    Async single-URL fetch via Jina AI with retry + backoff
  - scrape_urls_async()   Async batch scrape via asyncio.gather
  - scrape_urls()         Sync wrapper (blocking endpoint / testing)
  - format_scraped_results()  list[dict] → labeled LLM context string
"""

import asyncio
import logging
import re
import httpx
import requests
from duckduckgo_search import DDGS

logger = logging.getLogger("agentic_search.services")

# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_EXCLUDED_SITES = (
    "-site:yelp.com "
    "-site:tripadvisor.com "
    "-site:foursquare.com "
    "-site:zomato.com "
    "-site:doordash.com"
)


def calculate_target_urls(query: str, default: int = 12, buffer: int = 10) -> int:
    """
    Parse the user's query for an explicit number and calculate how many
    URLs to fetch so there's a massive buffer to survive the Domain Router.

    Examples:
      "top 5 pizza places"  → 5 + 10 = 15
      "best 10 databases"   → 10 + 10 = 20  (clamped to 20)
      "open source tools"   → default = 12

    Args:
        query:   The user's raw search query.
        default: URLs to fetch when no number is detected (default 12).
        buffer:  Extra URLs fetched above the requested count to survive filtering (default 10).

    Returns:
        An integer in [8, 20].
    """
    match = re.search(r'\b(\d+)\b', query)
    if match:
        requested = int(match.group(1))
        target = requested + buffer
    else:
        target = default

    # Clamped higher (8 to 20) to ensure we survive aggressive Bouncer filtering
    clamped = max(8, min(target, 20))
    logger.info(
        f"[SEARCH] calculate_target_urls: query={query!r} "
        f"→ target={target} clamped={clamped}"
    )
    return clamped


def search_web(query: str, max_results: int = 12) -> list[str]:
    """
    Search DuckDuckGo and return up to `max_results` URLs.

    Over-fetches 12 results by default so scraping failures still leave
    enough context for the LLM. Appends site exclusions to skip
    CAPTCHA-gated review aggregators.
    """
    refined_query = f"{query} {_EXCLUDED_SITES}"
    logger.info(f"[SEARCH] Query='{refined_query}' max_results={max_results}")
    with DDGS() as ddgs:
        results = list(ddgs.text(refined_query, max_results=max_results))
    urls = [r["href"] for r in results if "href" in r]
    logger.info(f"[SEARCH] Found {len(urls)} URLs")
    return urls


# ---------------------------------------------------------------------------
# Scraping — Jina AI Reader (async, with retry)
# ---------------------------------------------------------------------------

SCRAPER_POOL = ["https://r.jina.ai/", "https://api.zenrows.com/v1/?url=", ""]
JINA_TIMEOUT = 5.0       # seconds per request attempt
JINA_RETRY_BACKOFF = 1.5  # seconds to wait between retry attempts
MAX_CHARS_PER_PAGE = 4000

_JINA_HEADERS = {"Accept": "application/json"}


def optimize_scraped_text(raw_text: str, query: str, max_chars: int = 4000) -> str:
    """
    Heuristic Context Compression — prioritizes query-relevant sentences.

    Instead of hard-truncating at max_chars (which discards potentially
    relevant content at the bottom of long pages), this function:
      1. Extracts meaningful keywords (4+ letters) from the user's query.
      2. Splits the page into sentence-level chunks (split on ". ").
      3. Ranks chunks: relevant ones (containing any keyword) come first.
      4. Fills up to max_chars, starting with relevant chunks.

    This maximises signal density in the LLM context window — a page that
    mentions the query topic deep in the article body will be compressed
    correctly, not silently truncated.

    Args:
        raw_text: Full text returned by Jina AI.
        query:    The user's original search query (used for keyword extraction).
        max_chars: Character budget for the compressed output.

    Returns:
        Compressed string that fits within max_chars, relevant content first.
    """
    # Extract meaningful keywords (4+ letters avoids noise like 'the', 'in')
    keywords = [w.lower() for w in re.findall(r'\b\w{4,}\b', query)]

    # Normalise whitespace
    cleaned = ' '.join(raw_text.split())

    # Split on sentence boundaries
    chunks = [c.strip() for c in cleaned.split('. ') if c.strip()]

    # Partition by keyword relevance
    relevant_chunks: list[str] = []
    other_chunks: list[str] = []
    for chunk in chunks:
        lower = chunk.lower()
        if any(kw in lower for kw in keywords):
            relevant_chunks.append(chunk)
        else:
            other_chunks.append(chunk)

    # Reassemble: relevant first, then filler up to budget
    compressed = ''
    for chunk in relevant_chunks + other_chunks:
        candidate = (compressed + '. ' + chunk) if compressed else chunk
        if len(candidate) > max_chars:
            break
        compressed = candidate

    logger.debug(
        f"[COMPRESS] {len(raw_text)} → {len(compressed)} chars "
        f"({len(relevant_chunks)} relevant / {len(other_chunks)} other chunks)"
    )
    return compressed


async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    query: str,
    max_retries: int = 2,
) -> dict | None:
    """
    Fetch a single URL via the Jina AI Reader API with retry logic.

    Jina proxies through a headless browser (JS rendering, bot-protection
    bypass). Intermittent 403s or empty responses indicate Jina is rotating
    its egress IP — a short backoff before retry usually resolves this.

    The raw content is passed through optimize_scraped_text to prioritise
    query-relevant sentences before truncating to MAX_CHARS_PER_PAGE.

    Retry policy:
      - Up to `max_retries` total attempts per URL.
      - Retries on: non-200 status, empty content, or any network error.
      - 1.5s backoff between attempts (allows Jina IP rotation).
      - No retry after the final attempt.

    Args:
        client:      A shared httpx.AsyncClient instance (connection pooling).
        url:         The target URL to fetch.
        query:       The user's search query (for context-aware compression).
        max_retries: Total attempts allowed (default 2).

    Returns:
        {"url": str, "content": str} on success, or None if all attempts fail.
    """
    for attempt in range(max_retries):
        is_last = attempt == max_retries - 1
        
        for base_url in SCRAPER_POOL:
            full_url = f"{base_url}{url}"
            headers = _JINA_HEADERS if "jina" in base_url else {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

            try:
                response = await client.get(
                    full_url,
                    headers=headers,
                    timeout=JINA_TIMEOUT,
                )

                if response.status_code == 200:
                    if "jina" in base_url:
                        data = response.json()
                        raw_content = data.get("data", {}).get("content", "").strip()
                    else:
                        raw_content = response.text.strip()

                    if raw_content:
                        # ✅ Success — compress then return, no further retries
                        content = optimize_scraped_text(raw_content, query, MAX_CHARS_PER_PAGE)
                        logger.info(
                            f"[SCRAPE] OK via {base_url}  {url} ({len(content)} chars, "
                            f"attempt {attempt + 1}/{max_retries})"
                        )
                        return {"url": url, "content": content}

                    logger.warning(
                        f"[SCRAPE] Empty content for {url} via {base_url} "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                else:
                    logger.warning(
                        f"[SCRAPE] Failed via {base_url}, rotating... HTTP {response.status_code} for {url} "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    continue

            except (httpx.TimeoutException, httpx.RequestError, KeyError, ValueError) as exc:
                logger.warning(
                    f"[SCRAPE] Failed via {base_url}, rotating... Exception: {type(exc).__name__} for {url} "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                continue

        # Backoff before retry — skip sleep on the very last attempt
        if not is_last:
            logger.info(
                f"[SCRAPE] Backing off {JINA_RETRY_BACKOFF}s before next attempt → {url}"
            )
            await asyncio.sleep(JINA_RETRY_BACKOFF)

    logger.warning(f"[SCRAPE] All {max_retries} attempts and fallbacks exhausted for {url}")
    return None


async def scrape_urls_async(urls: list[str], query: str, max_retries: int = 2) -> list[dict]:
    """
    Concurrently scrape a list of URLs via Jina AI with per-URL retry logic.

    Uses a single shared httpx.AsyncClient for connection pooling across all
    concurrent requests. asyncio.gather runs all URL fetches in parallel —
    retries on individual URLs do not block other URLs. The query is threaded
    through to fetch_with_retry for context-aware content compression.

    Args:
        urls:        List of target URLs to fetch.
        query:       The user's search query (passed to optimize_scraped_text).
        max_retries: Max attempts per URL (passed to fetch_with_retry).

    Returns:
        List of {"url": str, "content": str} dicts for successfully scraped
        URLs. Failed URLs (after all retries) are filtered out.
    """
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[fetch_with_retry(client, url, query, max_retries) for url in urls],
            return_exceptions=True,
        )

    # Filter out None and any unexpected exceptions
    scraped = [r for r in results if isinstance(r, dict)]
    logger.info(
        f"[SCRAPE] Completed: {len(scraped)}/{len(urls)} URLs scraped successfully"
    )
    return scraped


def scrape_urls(urls: list[str]) -> str:
    """
    Sync wrapper around scrape_urls_async for use in blocking contexts.

    Returns the combined labeled SOURCE/CONTENT string for LLM injection.
    """
    scraped = asyncio.run(scrape_urls_async(urls))
    return format_scraped_results(scraped)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_scraped_results(results: list[dict]) -> str:
    """
    Convert a list of {"url", "content"} dicts into a labeled string
    for direct injection into the LLM extraction prompt.

    Format per source:
        SOURCE: <url>
        CONTENT: <markdown text>
        ---
    """
    parts = [
        f"SOURCE: {r['url']}\nCONTENT: {r['content']}\n---"
        for r in results
    ]
    combined = "\n\n".join(parts)
    logger.info(
        f"[SCRAPE] Context string: {len(results)} sources, {len(combined)} chars"
    )
    return combined

def filter_clean_urls(urls: list[str], query: str) -> list[str]:
    """
    Dynamic Domain Router — filters URLs using a universal blocklist plus
    intent-specific blocklists derived from query keywords.

    Two-layer filtering strategy:
      Layer 1 (universal):  Remove social media, login walls, and noisy platforms
                            that apply to every query type.
      Layer 2 (intent):     Detect the query's domain via keyword heuristics and
                            append the relevant directory/paywall blocklist.

    Args:
        urls:  Raw URL list from DuckDuckGo (post search_web()).
        query: The user's original search query (used for intent detection).

    Returns:
        Filtered list containing only URLs that pass both layers.
    """
    # ── Layer 1: Universal blocklist ─────────────────────────────────────────
    bad_domains: list[str] = [
        ".pinterest.",
        ".instagram.",
        ".facebook.",
        ".tiktok.",
        ".linkedin.",
        ".quora.",
        ".medium.",
        ".reddit.com/login",
    ]

    # ── Layer 2: Intent-based routing ────────────────────────────────────────
    q = query.lower()

    if any(kw in q for kw in ("pizza", "food", "restaurant", "eat", "cafe", "dining")):
        bad_domains.extend([
            ".yelp.", ".tripadvisor.", ".foursquare.",
            ".zomato.", ".doordash.", ".ubereats.", ".grubhub.",
        ])
        logger.debug("[FILTER] Intent: Food — extended blocklist applied")

    elif any(kw in q for kw in ("software", "tool", "database", "app", "framework", "library", "api")):
        bad_domains.extend([
            ".g2.", ".capterra.", ".trustradius.", ".sourceforge.",
        ])
        logger.debug("[FILTER] Intent: Tech/Software — extended blocklist applied")

    elif any(kw in q for kw in ("startup", "company", "business", "healthcare", "enterprise", "funding")):
        bad_domains.extend([
            ".crunchbase.", ".pitchbook.", ".zoominfo.", ".glassdoor.",
        ])
        logger.debug("[FILTER] Intent: Business — extended blocklist applied")

    elif any(kw in q for kw in ("news", "article", "latest", "update", "today", "breaking")):
        bad_domains.extend([
            ".wsj.", ".nytimes.", ".bloomberg.", ".msn.", ".yahoo.",
        ])
        logger.debug("[FILTER] Intent: News — extended blocklist applied")

    elif any(kw in q for kw in ("research", "paper", "study", "journal", "academic", "publication")):
        bad_domains.extend([
            ".sciencedirect.", ".jstor.", ".springer.", ".ieee.",
        ])
        logger.debug("[FILTER] Intent: Academia — extended blocklist applied")
    elif any(kw in q for kw in ["things to do", "activity", "activities", "attraction", "visit", "travel", "tour"]):
        bad_domains.extend([".yelp.", ".tripadvisor.", ".foursquare.", ".expedia.", ".viator.", ".getyourguide."])
        logger.debug("[FILTER] Intent: Travel — extended blocklist applied")
    # ── Execute filter ────────────────────────────────────────────────────────
    clean = [u for u in urls if not any(domain in u for domain in bad_domains)]

    logger.info(
        f"[FILTER] {len(clean)}/{len(urls)} URLs passed "
        f"(removed {len(urls) - len(clean)})"
    )
    return clean