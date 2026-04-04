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
import httpx
import requests
from ddgs import DDGS

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

JINA_BASE = "https://r.jina.ai/"
JINA_TIMEOUT = 15.0       # seconds per request attempt
JINA_RETRY_BACKOFF = 1.5  # seconds to wait between retry attempts
MAX_CHARS_PER_PAGE = 4000

_JINA_HEADERS = {"Accept": "application/json"}


async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    max_retries: int = 2,
) -> dict | None:
    """
    Fetch a single URL via the Jina AI Reader API with retry logic.

    Jina proxies through a headless browser (JS rendering, bot-protection
    bypass). Intermittent 403s or empty responses indicate Jina is rotating
    its egress IP — a short backoff before retry usually resolves this.

    Retry policy:
      - Up to `max_retries` total attempts per URL.
      - Retries on: non-200 status, empty content, or any network error.
      - 1.5s backoff between attempts (allows Jina IP rotation).
      - No retry after the final attempt.

    Args:
        client:      A shared httpx.AsyncClient instance (connection pooling).
        url:         The target URL to fetch.
        max_retries: Total attempts allowed (default 2).

    Returns:
        {"url": str, "content": str} on success, or None if all attempts fail.
    """
    jina_url = f"{JINA_BASE}{url}"

    for attempt in range(max_retries):
        is_last = attempt == max_retries - 1
        try:
            response = await client.get(
                jina_url,
                headers=_JINA_HEADERS,
                timeout=JINA_TIMEOUT,
            )

            if response.status_code == 200:
                data = response.json()
                content = data.get("data", {}).get("content", "").strip()

                if content:
                    # ✅ Success — return immediately, no further retries needed
                    content = content[:MAX_CHARS_PER_PAGE]
                    logger.info(
                        f"[SCRAPE] OK  {url} ({len(content)} chars, "
                        f"attempt {attempt + 1}/{max_retries})"
                    )
                    return {"url": url, "content": content}

                logger.warning(
                    f"[SCRAPE] Empty content for {url} "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
            else:
                logger.warning(
                    f"[SCRAPE] HTTP {response.status_code} for {url} "
                    f"(attempt {attempt + 1}/{max_retries})"
                )

        except httpx.TimeoutException:
            logger.warning(
                f"[SCRAPE] Timeout after {JINA_TIMEOUT}s for {url} "
                f"(attempt {attempt + 1}/{max_retries})"
            )
        except httpx.RequestError as exc:
            logger.warning(
                f"[SCRAPE] Request error for {url} "
                f"(attempt {attempt + 1}/{max_retries}): {exc}"
            )
        except (KeyError, ValueError) as exc:
            logger.warning(
                f"[SCRAPE] Bad JSON from Jina for {url} "
                f"(attempt {attempt + 1}/{max_retries}): {exc}"
            )

        # Backoff before retry — skip sleep on the very last attempt
        if not is_last:
            logger.info(
                f"[SCRAPE] Backing off {JINA_RETRY_BACKOFF}s before retry → {url}"
            )
            await asyncio.sleep(JINA_RETRY_BACKOFF)

    logger.warning(f"[SCRAPE] All {max_retries} attempts exhausted for {url}")
    return None


async def scrape_urls_async(urls: list[str], max_retries: int = 2) -> list[dict]:
    """
    Concurrently scrape a list of URLs via Jina AI with per-URL retry logic.

    Uses a single shared httpx.AsyncClient for connection pooling across all
    concurrent requests. asyncio.gather runs all URL fetches in parallel —
    retries on individual URLs do not block other URLs.

    Args:
        urls:        List of target URLs to fetch.
        max_retries: Max attempts per URL (passed to fetch_with_retry).

    Returns:
        List of {"url": str, "content": str} dicts for successfully scraped
        URLs. Failed URLs (after all retries) are filtered out.
    """
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[fetch_with_retry(client, url, max_retries) for url in urls],
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

def filter_clean_urls(urls: list[str]) -> list[str]:
    """Forcefully removes major directory sites (and their international variants)."""
    # Notice we removed the '.com' and added dots to prevent accidental substring matches
    bad_domains = [
        ".yelp.", ".tripadvisor.", ".foursquare.", 
        ".zomato.", ".doordash.", ".ubereats.", ".grubhub."
    ]
    
    clean_urls = []
    for url in urls:
        if not any(domain in url for domain in bad_domains):
            clean_urls.append(url)
            
    return clean_urls