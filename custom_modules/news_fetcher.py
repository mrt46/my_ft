"""News Fetcher — aggregate crypto news from multiple sources.

Fetches recent news headlines for specified coins from various
free and low-cost APIs. Results are cached to minimize API usage.

Sources:
    - CryptoPanic (free tier: 100 requests/day)
    - NewsAPI (free tier: 100 requests/day)
    - CoinDesk RSS (unlimited, slower)

Logging:
    - logs/news_YYYYMMDD.jsonl — her çekilen haber batch'i JSONL formatında loglanır

Usage:
    fetcher = NewsFetcher()
    news = await fetcher.fetch_news_for_coin('BTC', hours=24)
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict
from xml.etree import ElementTree as ET

import aiohttp
import feedparser
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------

class NewsArticle(TypedDict):
    """Single news article."""

    title: str
    source: str
    url: str
    published_at: str
    sentiment_hint: str  # 'positive', 'negative', 'neutral'


class NewsCache(TypedDict):
    """Cached news for a coin."""

    articles: list[NewsArticle]
    timestamp: float


# ---------------------------------------------------------------------------
# NewsFetcher
# ---------------------------------------------------------------------------

class NewsFetcher:
    """Fetch crypto news from multiple sources with caching.

    Implements intelligent fallback:
        1. Try CryptoPanic (fast, crypto-focused)
        2. Fallback to NewsAPI (general news)
        3. Fallback to RSS feeds (reliable but slower)

    Caching:
        - TTL: 30 minutes (configurable)
        - Reduces API calls and improves speed

    Logging:
        - logs/news_YYYYMMDD.jsonl — her fetch işlemi JSONL formatında loglanır
        - Her satır: {ts, coin, source, count, articles[]}

    Example:
        fetcher = NewsFetcher()
        articles = await fetcher.fetch_news_for_coin('BTC', hours=24)
        titles = [a['title'] for a in articles]
    """

    CACHE_FILE = Path(__file__).parent.parent / "data" / "news_cache.json"
    LOG_DIR = Path(__file__).parent.parent / "logs"

    # API endpoints
    CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
    NEWSAPI_URL = "https://newsapi.org/v2/everything"

    # RSS feeds
    RSS_FEEDS = {
        "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "cointelegraph": "https://cointelegraph.com/rss",
        "decrypt": "https://decrypt.co/feed",
    }

    def __init__(self) -> None:
        """Initialize with API keys and settings."""
        settings_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(settings_path) as fh:
            cfg = yaml.safe_load(fh)

        news_cfg = cfg.get("news", {})
        self._cache_ttl: int = news_cfg.get("cache_ttl_minutes", 30) * 60
        self._timeout: int = news_cfg.get("fetch_timeout_seconds", 10)
        self._max_articles: int = news_cfg.get("max_articles_per_coin", 10)

        # API keys from environment
        self._cryptopanic_key: str = os.getenv("CRYPTOPANIC_API_KEY", "")
        self._newsapi_key: str = os.getenv("NEWSAPI_KEY", "")

        # In-memory cache
        self._cache: dict[str, NewsCache] = {}
        self._load_cache()

        logger.info(
            f"NewsFetcher initialized — CryptoPanic: {'✓' if self._cryptopanic_key else '✗'}, "
            f"NewsAPI: {'✓' if self._newsapi_key else '✗'}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_news_for_coin(self, coin: str, hours: int = 24) -> list[NewsArticle]:
        """Fetch news articles for a specific coin.

        Args:
            coin: Coin symbol, e.g. 'BTC', 'ETH'.
            hours: How many hours back to fetch (default 24).

        Returns:
            List of NewsArticle dicts, sorted by recency.
        """
        cache_key = f"{coin}_{hours}h"

        # Check cache first
        if self._is_cache_valid(cache_key):
            logger.debug(f"Using cached news for {coin}")
            cached = self._cache[cache_key]["articles"]
            self._log_news(coin, "cache", cached)
            return cached

        # Fetch from sources (with fallback)
        articles: list[NewsArticle] = []
        used_source = "none"

        try:
            # Primary: CryptoPanic
            if self._cryptopanic_key:
                articles = await self._fetch_cryptopanic(coin, hours)
                if articles:
                    used_source = "cryptopanic"
                    logger.info(f"CryptoPanic: {len(articles)} articles for {coin}")
        except Exception as exc:
            logger.warning(f"CryptoPanic failed for {coin}: {exc}")

        # Fallback: NewsAPI
        if not articles and self._newsapi_key:
            try:
                articles = await self._fetch_newsapi(coin, hours)
                if articles:
                    used_source = "newsapi"
                    logger.info(f"NewsAPI: {len(articles)} articles for {coin}")
            except Exception as exc:
                logger.warning(f"NewsAPI failed for {coin}: {exc}")

        # Final fallback: RSS
        if not articles:
            try:
                articles = await self._fetch_rss(coin, hours)
                if articles:
                    used_source = "rss"
                    logger.info(f"RSS: {len(articles)} articles for {coin}")
            except Exception as exc:
                logger.warning(f"RSS failed for {coin}: {exc}")

        # Log fetched articles
        final_articles = articles[: self._max_articles]
        self._log_news(coin, used_source, final_articles)

        # Cache and return
        self._cache[cache_key] = {
            "articles": final_articles,
            "timestamp": time.time(),
        }
        self._save_cache()

        return final_articles

    async def fetch_news_for_coins(
        self, coins: list[str], hours: int = 24
    ) -> dict[str, list[NewsArticle]]:
        """Fetch news for multiple coins in parallel.

        Args:
            coins: List of coin symbols.
            hours: How many hours back to fetch.

        Returns:
            Mapping of coin → articles list.
        """
        tasks = [self.fetch_news_for_coin(coin, hours) for coin in coins]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        news_map: dict[str, list[NewsArticle]] = {}
        for coin, result in zip(coins, results):
            if isinstance(result, Exception):
                logger.error(f"News fetch failed for {coin}: {result}")
                news_map[coin] = []
            else:
                news_map[coin] = result

        return news_map

    def get_cached_titles(self, coin: str, hours: int = 24) -> list[str]:
        """Get just the titles from cached news (for sentiment analysis).

        Args:
            coin: Coin symbol.
            hours: Cache key hours.

        Returns:
            List of article titles.
        """
        cache_key = f"{coin}_{hours}h"
        if cache_key in self._cache:
            return [a["title"] for a in self._cache[cache_key]["articles"]]
        return []

    def clear_cache(self) -> None:
        """Clear the news cache."""
        self._cache.clear()
        if self.CACHE_FILE.exists():
            self.CACHE_FILE.unlink()
        logger.info("News cache cleared")

    def _log_news(self, coin: str, source: str, articles: list[NewsArticle]) -> None:
        """Append a news fetch record to logs/news_YYYYMMDD.jsonl.

        Each line is a self-contained JSON object:
            {ts, coin, source, count, articles[{title, source, url, published_at, sentiment_hint}]}

        Args:
            coin: Coin symbol (e.g. 'BTC').
            source: Which source provided the articles ('cryptopanic', 'newsapi', 'rss', 'cache').
            articles: List of fetched articles.
        """
        try:
            self.LOG_DIR.mkdir(parents=True, exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            log_file = self.LOG_DIR / f"news_{today}.jsonl"

            record = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "coin": coin,
                "source": source,
                "count": len(articles),
                "articles": [
                    {
                        "title": a["title"],
                        "source": a["source"],
                        "url": a["url"],
                        "published_at": a["published_at"],
                        "sentiment_hint": a["sentiment_hint"],
                    }
                    for a in articles
                ],
            }

            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        except Exception as exc:
            logger.warning("Failed to write news log: %s", exc)

    # ------------------------------------------------------------------
    # Internal: CryptoPanic
    # ------------------------------------------------------------------

    async def _fetch_cryptopanic(self, coin: str, hours: int) -> list[NewsArticle]:
        """Fetch from CryptoPanic API."""
        params = {
            "auth_token": self._cryptopanic_key,
            "currencies": coin,
            "kind": "news",
            "public": "true",
        }

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self.CRYPTOPANIC_URL, params=params) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"CryptoPanic HTTP {resp.status}")

                data = await resp.json()
                results = data.get("results", [])

                articles: list[NewsArticle] = []
                cutoff = time.time() - (hours * 3600)

                for item in results:
                    published = item.get("published_at", "")
                    if not published:
                        continue

                    # Parse timestamp
                    try:
                        pub_time = datetime.fromisoformat(published.replace("Z", "+00:00"))
                        pub_timestamp = pub_time.timestamp()
                    except Exception:
                        continue

                    if pub_timestamp < cutoff:
                        continue

                    articles.append(
                        NewsArticle(
                            title=item.get("title", ""),
                            source="cryptopanic",
                            url=item.get("url", ""),
                            published_at=published,
                            sentiment_hint=self._extract_sentiment_hint(
                                item.get("title", "")
                            ),
                        )
                    )

                return articles

    # ------------------------------------------------------------------
    # Internal: NewsAPI
    # ------------------------------------------------------------------

    async def _fetch_newsapi(self, coin: str, hours: int) -> list[NewsArticle]:
        """Fetch from NewsAPI."""
        from_date = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d")

        params = {
            "q": f"{coin} crypto",
            "from": from_date,
            "sortBy": "relevancy",
            "language": "en",
            "apiKey": self._newsapi_key,
            "pageSize": self._max_articles,
        }

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self.NEWSAPI_URL, params=params) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"NewsAPI HTTP {resp.status}")

                data = await resp.json()
                articles_data = data.get("articles", [])

                return [
                    NewsArticle(
                        title=a.get("title", ""),
                        source="newsapi",
                        url=a.get("url", ""),
                        published_at=a.get("publishedAt", ""),
                        sentiment_hint=self._extract_sentiment_hint(
                            a.get("title", "")
                        ),
                    )
                    for a in articles_data
                    if a.get("title")
                ]

    # ------------------------------------------------------------------
    # Internal: RSS Feeds
    # ------------------------------------------------------------------

    async def _fetch_rss(self, coin: str, hours: int) -> list[NewsArticle]:
        """Fetch from RSS feeds (blocking, run in thread)."""
        return await asyncio.to_thread(self._fetch_rss_sync, coin, hours)

    def _fetch_rss_sync(self, coin: str, hours: int) -> list[NewsArticle]:
        """Synchronous RSS fetch."""
        articles: list[NewsArticle] = []
        cutoff = time.time() - (hours * 3600)
        coin_lower = coin.lower()

        for source_name, feed_url in self.RSS_FEEDS.items():
            try:
                feed = feedparser.parse(feed_url)

                for entry in feed.entries[:20]:  # Check first 20 entries
                    title = entry.get("title", "").lower()
                    summary = entry.get("summary", "").lower()

                    # Check if coin mentioned
                    if coin_lower not in title and coin_lower not in summary:
                        continue

                    # Parse date
                    published = entry.get("published", "")
                    if not published:
                        continue

                    try:
                        # Try common date formats
                        for fmt in ["%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"]:
                            try:
                                pub_time = datetime.strptime(published, fmt)
                                pub_timestamp = pub_time.timestamp()
                                break
                            except ValueError:
                                continue
                        else:
                            continue
                    except Exception:
                        continue

                    if pub_timestamp < cutoff:
                        continue

                    articles.append(
                        NewsArticle(
                            title=entry.get("title", ""),
                            source=source_name,
                            url=entry.get("link", ""),
                            published_at=published,
                            sentiment_hint=self._extract_sentiment_hint(
                                entry.get("title", "")
                            ),
                        )
                    )

            except Exception as exc:
                logger.warning(f"RSS fetch failed for {source_name}: {exc}")

        # Sort by date and deduplicate by title
        seen_titles = set()
        unique_articles: list[NewsArticle] = []
        for a in sorted(articles, key=lambda x: x["published_at"], reverse=True):
            title_key = a["title"].lower()[:50]  # First 50 chars
            if title_key not in seen_titles:
                seen_titles.add(title_key)
                unique_articles.append(a)

        return unique_articles

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_sentiment_hint(self, title: str) -> str:
        """Extract rough sentiment hint from title keywords."""
        title_lower = title.lower()

        positive_words = ["surge", "rally", "bull", "gain", "up", "rise", "soar", "moon"]
        negative_words = ["crash", "drop", "bear", "fall", "down", "plunge", "dump", "hack"]

        pos_count = sum(1 for w in positive_words if w in title_lower)
        neg_count = sum(1 for w in negative_words if w in title_lower)

        if pos_count > neg_count:
            return "positive"
        elif neg_count > pos_count:
            return "negative"
        return "neutral"

    def _is_cache_valid(self, key: str) -> bool:
        """Check if cache entry is still valid."""
        if key not in self._cache:
            return False
        age = time.time() - self._cache[key]["timestamp"]
        return age < self._cache_ttl

    def _load_cache(self) -> None:
        """Load cache from disk."""
        try:
            if self.CACHE_FILE.exists():
                with open(self.CACHE_FILE) as f:
                    data = json.load(f)
                    # Filter out expired entries
                    now = time.time()
                    self._cache = {
                        k: v for k, v in data.items()
                        if now - v.get("timestamp", 0) < self._cache_ttl
                    }
        except Exception as exc:
            logger.warning(f"Failed to load news cache: {exc}")
            self._cache = {}

    def _save_cache(self) -> None:
        """Save cache to disk."""
        try:
            self.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(self.CACHE_FILE, "w") as f:
                json.dump(self._cache, f, indent=2)
        except Exception as exc:
            logger.error(f"Failed to save news cache: {exc}")
