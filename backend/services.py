"""
services.py — Search and Scraping services
===========================================
Provides:
  - search_web()      DuckDuckGo over-fetch with CAPTCHA-wall exclusions
  - scrape_url_single()  Single URL via Jina AI Reader (for SSE live events)
  - scrape_urls()     Batch scrape → combined labeled string for LLM context
"""

import logging
import requests
from ddgs import DDGS

logger = logging.getLogger("agentic_search.services")

# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

# Aggregate review sites that hide behind CAPTCHA walls — excluded by default
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

    Uses an over-fetch default of 12 so that after scraping failures,
    we still have rich context for the LLM to work with.
    Automatically appends site exclusions to skip CAPTCHA-gated aggregators.

    Args:
        query:       Natural language search query.
        max_results: Number of URLs to retrieve (default 12).

    Returns:
        List of result URLs.
    """
    refined_query = f"{query} {_EXCLUDED_SITES}"
    logger.info(f"[SEARCH] Query='{refined_query}' max_results={max_results}")

    with DDGS() as ddgs:
        results = list(ddgs.text(refined_query, max_results=max_results))

    urls = [r["href"] for r in results if "href" in r]
    logger.info(f"[SEARCH] Found {len(urls)} URLs")
    return urls


# ---------------------------------------------------------------------------
# Scraping (Jina AI Reader)
# ---------------------------------------------------------------------------

JINA_BASE = "https://r.jina.ai/"
JINA_TIMEOUT = 15        # seconds per request
MAX_CHARS_PER_PAGE = 4000


def scrape_url_single(url: str) -> dict | None:
    """
    Fetch a single URL via the Jina AI Reader API.

    Jina proxies the request through a headless browser, which:
      - Renders JavaScript-heavy SPAs
      - Bypasses most bot-protection / Cloudflare walls
      - Returns clean markdown content

    Returns:
        {"url": str, "content": str} on success, or None on any failure.
    """
    jina_url = f"{JINA_BASE}{url}"
    try:
        response = requests.get(
            jina_url,
            headers={"Accept": "application/json"},
            timeout=JINA_TIMEOUT,
        )

        if response.status_code != 200:
            logger.warning(f"[SCRAPE] Non-200 from Jina for {url}: HTTP {response.status_code}")
            return None

        data = response.json()
        content = data.get("data", {}).get("content", "").strip()

        if not content:
            logger.warning(f"[SCRAPE] Empty content returned by Jina for {url}")
            return None

        content = content[:MAX_CHARS_PER_PAGE]
        logger.info(f"[SCRAPE] OK  {url} ({len(content)} chars via Jina)")
        return {"url": url, "content": content}

    except requests.exceptions.Timeout:
        logger.warning(f"[SCRAPE] Timeout after {JINA_TIMEOUT}s for {url}")
    except requests.exceptions.RequestException as exc:
        logger.warning(f"[SCRAPE] Request error for {url}: {exc}")
    except (KeyError, ValueError) as exc:
        logger.warning(f"[SCRAPE] Failed to parse Jina response for {url}: {exc}")

    return None


def scrape_urls(urls: list[str]) -> str:
    """
    Scrape a list of URLs via Jina AI and combine the results into a single
    labeled string ready for direct injection into an LLM prompt.

    Each successful page is formatted as:
        SOURCE: <url>
        CONTENT: <markdown text>
        ---

    Failed URLs are skipped silently (logged as warnings).

    Args:
        urls: List of target URLs to fetch.

    Returns:
        Combined labeled string of all successfully scraped content.
        Returns an empty string if every URL fails.
    """
    parts: list[str] = []
    for url in urls:
        result = scrape_url_single(url)
        if result:
            parts.append(
                f"SOURCE: {result['url']}\n"
                f"CONTENT: {result['content']}\n"
                f"---"
            )
    combined = "\n\n".join(parts)
    logger.info(f"[SCRAPE] Combined context: {len(parts)} sources, {len(combined)} chars total")
    return combined


def format_scraped_results(results: list[dict]) -> str:
    """
    Convert a list of {url, content} dicts (from individual scrape_url_single
    calls, e.g. in the SSE pipeline) into the same labeled string format
    that scrape_urls() produces.

    This keeps the SSE per-URL live events working while feeding the LLM
    the same consistent context format.
    """
    parts = [
        f"SOURCE: {r['url']}\nCONTENT: {r['content']}\n---"
        for r in results
    ]
    return "\n\n".join(parts)
