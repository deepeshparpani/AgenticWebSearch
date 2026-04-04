"""
services.py — Scraping service using the Jina AI Reader API
============================================================
Replaces trafilatura with Jina AI's free r.jina.ai reader, which:
  - Bypasses most bot-protection (Cloudflare, etc.)
  - Renders JavaScript-heavy SPAs via headless Chrome internally
  - Returns clean markdown via a simple HTTP GET — no API key required

Usage:
    from services import scrape_urls

    results = scrape_urls(["https://example.com", "https://openai.com"])
    # [{"url": "https://example.com", "content": "..."}, ...]
"""

import logging
import requests

logger = logging.getLogger("agentic_search.services")

JINA_BASE = "https://r.jina.ai/"
JINA_TIMEOUT = 15       # seconds per request
MAX_CHARS_PER_PAGE = 4000


def scrape_url_single(url: str) -> dict | None:
    """
    Fetch a single URL via the Jina AI Reader API.

    Returns a dict {"url": str, "content": str} on success,
    or None if the request fails or yields no content.
    """
    jina_url = f"{JINA_BASE}{url}"
    try:
        response = requests.get(
            jina_url,
            headers={"Accept": "application/json"},
            timeout=JINA_TIMEOUT,
        )

        if response.status_code != 200:
            logger.warning(
                f"[SCRAPE] Non-200 from Jina for {url}: HTTP {response.status_code}"
            )
            return None

        data = response.json()
        content = data.get("data", {}).get("content", "").strip()

        if not content:
            logger.warning(f"[SCRAPE] Empty content returned by Jina for {url}")
            return None

        logger.info(f"[SCRAPE] OK  {url} ({len(content)} chars via Jina)")
        return {"url": url, "content": content[:MAX_CHARS_PER_PAGE]}

    except requests.exceptions.Timeout:
        logger.warning(f"[SCRAPE] Timeout after {JINA_TIMEOUT}s for {url}")
    except requests.exceptions.RequestException as exc:
        logger.warning(f"[SCRAPE] Request error for {url}: {exc}")
    except (KeyError, ValueError) as exc:
        logger.warning(f"[SCRAPE] Failed to parse Jina response for {url}: {exc}")

    return None


def scrape_urls(urls: list[str]) -> list[dict]:
    """
    Scrape a list of URLs via the Jina AI Reader API.

    Args:
        urls: List of target URLs to fetch.

    Returns:
        List of {"url": str, "content": str} dicts for each successfully
        scraped URL. URLs that fail are skipped silently (logged as warnings).
    """
    results: list[dict] = []
    for url in urls:
        result = scrape_url_single(url)
        if result:
            results.append(result)
    return results
