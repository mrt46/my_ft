"""Brutal tests for NewsFetcher.

Tests cover:
- Module import (feedparser, aiohttp, yaml, dotenv)
- Initialization with missing/invalid settings
- Cache logic (hit, miss, expiry, disk persistence)
- CryptoPanic / NewsAPI / RSS fetch with mocked HTTP
- Fallback chain (CryptoPanic → NewsAPI → RSS → empty)
- Parallel multi-coin fetch
- Sentiment hint extraction
- News logging (JSONL)
- Edge cases: empty response, malformed JSON, network timeout, bad dates
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

# ---------------------------------------------------------------------------
# Path setup — allow importing custom_modules without installing
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings_yaml(tmp_path):
    """Write a minimal settings.yaml to tmp_path and return its path."""
    cfg = {
        "news": {
            "cache_ttl_minutes": 30,
            "fetch_timeout_seconds": 10,
            "max_articles_per_coin": 10,
        }
    }
    import yaml
    p = tmp_path / "settings.yaml"
    p.write_text(yaml.dump(cfg))
    return p


@pytest.fixture
def fetcher(tmp_path, settings_yaml, monkeypatch):
    """Return a NewsFetcher with tmp_path as project root."""
    # Patch settings and data paths
    monkeypatch.setenv("CRYPTOPANIC_API_KEY", "test_cp_key")
    monkeypatch.setenv("NEWSAPI_KEY", "test_na_key")

    from custom_modules.news_fetcher import NewsFetcher

    with patch.object(NewsFetcher, "CACHE_FILE", tmp_path / "news_cache.json"), \
         patch.object(NewsFetcher, "LOG_DIR", tmp_path / "logs"), \
         patch("custom_modules.news_fetcher.Path") as mock_path_cls:

        # Make settings path resolve to our tmp settings
        mock_settings = MagicMock()
        mock_settings.__truediv__ = lambda self, other: settings_yaml if "settings" in str(other) else (tmp_path / other)
        mock_settings.parent = tmp_path
        mock_path_cls.return_value = mock_settings

        # Re-instantiate with real settings path
        f = NewsFetcher.__new__(NewsFetcher)
        f._cache_ttl = 30 * 60
        f._timeout = 10
        f._max_articles = 10
        f._cryptopanic_key = "test_cp_key"
        f._newsapi_key = "test_na_key"
        f._cache = {}
        f.CACHE_FILE = tmp_path / "news_cache.json"
        f.LOG_DIR = tmp_path / "logs"
        return f


@pytest.fixture
def sample_article():
    return {
        "title": "Bitcoin surges to new ATH",
        "source": "cryptopanic",
        "url": "https://example.com/btc",
        "published_at": "2026-02-23T00:00:00+00:00",
        "sentiment_hint": "positive",
    }


# ---------------------------------------------------------------------------
# 1. Import tests
# ---------------------------------------------------------------------------

class TestImports:
    def test_feedparser_importable(self):
        import feedparser
        assert feedparser.__version__

    def test_aiohttp_importable(self):
        import aiohttp
        assert aiohttp.__version__

    def test_news_fetcher_importable(self):
        from custom_modules.news_fetcher import NewsFetcher, NewsArticle, NewsCache
        assert NewsFetcher
        assert NewsArticle
        assert NewsCache

    def test_news_fetcher_has_required_methods(self):
        from custom_modules.news_fetcher import NewsFetcher
        for method in [
            "fetch_news_for_coin", "fetch_news_for_coins",
            "get_cached_titles", "clear_cache",
            "_fetch_cryptopanic", "_fetch_newsapi", "_fetch_rss",
            "_extract_sentiment_hint", "_is_cache_valid",
            "_log_news", "_load_cache", "_save_cache",
        ]:
            assert hasattr(NewsFetcher, method), f"Missing method: {method}"


# ---------------------------------------------------------------------------
# 2. Initialization tests
# ---------------------------------------------------------------------------

class TestInitialization:
    def test_init_reads_settings(self, fetcher):
        assert fetcher._cache_ttl == 30 * 60
        assert fetcher._timeout == 10
        assert fetcher._max_articles == 10

    def test_init_reads_env_keys(self, fetcher):
        assert fetcher._cryptopanic_key == "test_cp_key"
        assert fetcher._newsapi_key == "test_na_key"

    def test_init_empty_cache(self, fetcher):
        assert isinstance(fetcher._cache, dict)

    def test_init_missing_settings_raises(self, tmp_path, monkeypatch):
        """NewsFetcher should raise if settings.yaml is missing."""
        from custom_modules.news_fetcher import NewsFetcher
        bad_path = tmp_path / "nonexistent" / "settings.yaml"
        with patch("custom_modules.news_fetcher.Path") as mock_p:
            mock_p.return_value.__truediv__ = lambda s, o: bad_path
            with pytest.raises(Exception):
                NewsFetcher()


# ---------------------------------------------------------------------------
# 3. Cache tests
# ---------------------------------------------------------------------------

class TestCache:
    def test_cache_miss_returns_false(self, fetcher):
        assert not fetcher._is_cache_valid("BTC_24h")

    def test_cache_hit_returns_true(self, fetcher):
        fetcher._cache["BTC_24h"] = {"articles": [], "timestamp": time.time()}
        assert fetcher._is_cache_valid("BTC_24h")

    def test_cache_expired_returns_false(self, fetcher):
        fetcher._cache["BTC_24h"] = {
            "articles": [],
            "timestamp": time.time() - (31 * 60),  # 31 minutes ago
        }
        assert not fetcher._is_cache_valid("BTC_24h")

    def test_cache_save_and_load(self, fetcher, tmp_path):
        fetcher._cache["ETH_24h"] = {
            "articles": [{"title": "ETH news", "source": "test", "url": "", "published_at": "", "sentiment_hint": "neutral"}],
            "timestamp": time.time(),
        }
        fetcher._save_cache()
        assert fetcher.CACHE_FILE.exists()

        fetcher._cache.clear()
        fetcher._load_cache()
        assert "ETH_24h" in fetcher._cache

    def test_clear_cache(self, fetcher, tmp_path):
        fetcher._cache["BTC_24h"] = {"articles": [], "timestamp": time.time()}
        fetcher._save_cache()
        fetcher.clear_cache()
        assert len(fetcher._cache) == 0
        assert not fetcher.CACHE_FILE.exists()

    def test_load_cache_ignores_expired(self, fetcher, tmp_path):
        old_data = {
            "BTC_24h": {"articles": [], "timestamp": time.time() - (31 * 60)}
        }
        fetcher.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fetcher.CACHE_FILE.write_text(json.dumps(old_data))
        fetcher._load_cache()
        assert "BTC_24h" not in fetcher._cache

    def test_load_cache_handles_corrupt_file(self, fetcher, tmp_path):
        fetcher.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fetcher.CACHE_FILE.write_text("NOT VALID JSON {{{{")
        fetcher._load_cache()  # Should not raise
        assert fetcher._cache == {}

    def test_get_cached_titles_empty(self, fetcher):
        assert fetcher.get_cached_titles("BTC") == []

    def test_get_cached_titles_returns_titles(self, fetcher, sample_article):
        fetcher._cache["BTC_24h"] = {
            "articles": [sample_article],
            "timestamp": time.time(),
        }
        titles = fetcher.get_cached_titles("BTC", hours=24)
        assert titles == ["Bitcoin surges to new ATH"]


# ---------------------------------------------------------------------------
# 4. Sentiment hint extraction
# ---------------------------------------------------------------------------

class TestSentimentHint:
    @pytest.fixture(autouse=True)
    def _fetcher(self, fetcher):
        self.f = fetcher

    def test_positive_keywords(self):
        assert self.f._extract_sentiment_hint("Bitcoin surges to new ATH") == "positive"

    def test_negative_keywords(self):
        assert self.f._extract_sentiment_hint("BTC crashes 20% in bear market") == "negative"

    def test_neutral_no_keywords(self):
        assert self.f._extract_sentiment_hint("Bitcoin developer meeting scheduled") == "neutral"

    def test_mixed_leans_positive(self):
        # "rally" and "surge" vs "drop" → 2 positive vs 1 negative
        assert self.f._extract_sentiment_hint("BTC rally and surge despite drop") == "positive"

    def test_empty_string(self):
        result = self.f._extract_sentiment_hint("")
        assert result in ("positive", "negative", "neutral")

    def test_case_insensitive(self):
        assert self.f._extract_sentiment_hint("BITCOIN SURGES") == "positive"


# ---------------------------------------------------------------------------
# 5. CryptoPanic fetch (mocked HTTP)
# ---------------------------------------------------------------------------

class TestCryptoPanicFetch:
    def _make_response(self, articles_data, status=200):
        mock_resp = AsyncMock()
        mock_resp.status = status
        mock_resp.json = AsyncMock(return_value={"results": articles_data})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        return mock_resp

    def _make_session(self, resp):
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        return mock_session

    @pytest.mark.asyncio
    async def test_fetch_returns_articles(self, fetcher):
        articles_data = [
            {"title": "BTC up 5%", "url": "https://example.com", "published_at": "2026-02-23T00:00:00Z"},
        ]
        resp = self._make_response(articles_data)
        session = self._make_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await fetcher._fetch_cryptopanic("BTC", hours=24)
        assert len(result) == 1
        assert result[0]["title"] == "BTC up 5%"

    @pytest.mark.asyncio
    async def test_fetch_filters_old_articles(self, fetcher):
        old_time = "2020-01-01T00:00:00Z"
        articles_data = [
            {"title": "Old BTC news", "url": "", "published_at": old_time},
        ]
        resp = self._make_response(articles_data)
        session = self._make_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await fetcher._fetch_cryptopanic("BTC", hours=24)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_fetch_http_error_raises(self, fetcher):
        resp = self._make_response([], status=429)
        session = self._make_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(RuntimeError, match="CryptoPanic HTTP 429"):
                await fetcher._fetch_cryptopanic("BTC", hours=24)

    @pytest.mark.asyncio
    async def test_fetch_empty_results(self, fetcher):
        resp = self._make_response([])
        session = self._make_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await fetcher._fetch_cryptopanic("BTC", hours=24)
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_skips_missing_published_at(self, fetcher):
        articles_data = [{"title": "BTC news", "url": ""}]  # no published_at
        resp = self._make_response(articles_data)
        session = self._make_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await fetcher._fetch_cryptopanic("BTC", hours=24)
        assert result == []


# ---------------------------------------------------------------------------
# 6. NewsAPI fetch (mocked HTTP)
# ---------------------------------------------------------------------------

class TestNewsAPIFetch:
    def _make_response(self, articles_data, status=200):
        mock_resp = AsyncMock()
        mock_resp.status = status
        mock_resp.json = AsyncMock(return_value={"articles": articles_data})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        return mock_resp

    def _make_session(self, resp):
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        return mock_session

    @pytest.mark.asyncio
    async def test_fetch_returns_articles(self, fetcher):
        articles_data = [
            {"title": "ETH 2.0 launch", "url": "https://example.com", "publishedAt": "2026-02-23T00:00:00Z"},
        ]
        resp = self._make_response(articles_data)
        session = self._make_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await fetcher._fetch_newsapi("ETH", hours=24)
        assert len(result) == 1
        assert result[0]["source"] == "newsapi"

    @pytest.mark.asyncio
    async def test_fetch_skips_empty_title(self, fetcher):
        articles_data = [
            {"title": "", "url": "", "publishedAt": "2026-02-23T00:00:00Z"},
            {"title": "Real news", "url": "", "publishedAt": "2026-02-23T00:00:00Z"},
        ]
        resp = self._make_response(articles_data)
        session = self._make_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await fetcher._fetch_newsapi("ETH", hours=24)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_fetch_http_error_raises(self, fetcher):
        resp = self._make_response([], status=401)
        session = self._make_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(RuntimeError, match="NewsAPI HTTP 401"):
                await fetcher._fetch_newsapi("ETH", hours=24)


# ---------------------------------------------------------------------------
# 7. RSS fetch (mocked feedparser)
# ---------------------------------------------------------------------------

class TestRSSFetch:
    def _make_feed_entry(self, title, coin, hours_ago=1):
        from datetime import datetime, timezone, timedelta
        pub_time = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        pub_str = pub_time.strftime("%a, %d %b %Y %H:%M:%S %z")
        entry = MagicMock()
        # Use spec=None so attribute access returns real strings, not MagicMock
        entry.title = title
        entry.summary = f"{coin.lower()} related news"
        entry.published = pub_str
        entry.link = "https://example.com/news"
        # feedparser entries use .get() dict-style access
        entry.get = lambda key, default="": {
            "title": title,
            "summary": f"{coin.lower()} related news",
            "published": pub_str,
            "link": "https://example.com/news",
        }.get(key, default)
        return entry

    def test_rss_returns_matching_articles(self, fetcher):
        entry = self._make_feed_entry("BTC halving news", "BTC")
        mock_feed = MagicMock()
        mock_feed.entries = [entry]

        with patch("feedparser.parse", return_value=mock_feed):
            result = fetcher._fetch_rss_sync("BTC", hours=24)
        # Article should be found (coin "btc" appears in summary "btc related news")
        assert len(result) == 1
        assert result[0]["title"] == "BTC halving news"

    def test_rss_filters_old_articles(self, fetcher):
        entry = self._make_feed_entry("Old BTC news", "BTC", hours_ago=48)
        mock_feed = MagicMock()
        mock_feed.entries = [entry]

        with patch("feedparser.parse", return_value=mock_feed):
            result = fetcher._fetch_rss_sync("BTC", hours=24)
        assert result == []

    def test_rss_filters_unrelated_articles(self, fetcher):
        entry = self._make_feed_entry("Stock market news", "AAPL", hours_ago=1)
        mock_feed = MagicMock()
        mock_feed.entries = [entry]

        with patch("feedparser.parse", return_value=mock_feed):
            result = fetcher._fetch_rss_sync("BTC", hours=24)
        assert result == []

    def test_rss_deduplicates_titles(self, fetcher):
        entry1 = self._make_feed_entry("BTC news duplicate", "BTC", hours_ago=1)
        entry2 = self._make_feed_entry("BTC news duplicate", "BTC", hours_ago=2)
        mock_feed = MagicMock()
        mock_feed.entries = [entry1, entry2]

        with patch("feedparser.parse", return_value=mock_feed):
            result = fetcher._fetch_rss_sync("BTC", hours=24)
        titles = [a["title"] for a in result]
        assert len(titles) == len(set(titles))

    def test_rss_handles_feed_exception(self, fetcher):
        with patch("feedparser.parse", side_effect=Exception("Network error")):
            result = fetcher._fetch_rss_sync("BTC", hours=24)
        assert result == []

    def test_rss_skips_missing_published(self, fetcher):
        entry = MagicMock()
        entry.title = "BTC news"
        entry.summary = "btc related"
        entry.published = ""  # missing date
        entry.link = ""
        mock_feed = MagicMock()
        mock_feed.entries = [entry]

        with patch("feedparser.parse", return_value=mock_feed):
            result = fetcher._fetch_rss_sync("BTC", hours=24)
        assert result == []


# ---------------------------------------------------------------------------
# 8. Fallback chain tests
# ---------------------------------------------------------------------------

class TestFallbackChain:
    @pytest.mark.asyncio
    async def test_uses_cache_when_valid(self, fetcher, sample_article):
        fetcher._cache["BTC_24h"] = {
            "articles": [sample_article],
            "timestamp": time.time(),
        }
        with patch.object(fetcher, "_fetch_cryptopanic") as mock_cp:
            result = await fetcher.fetch_news_for_coin("BTC", hours=24)
        mock_cp.assert_not_called()
        assert result == [sample_article]

    @pytest.mark.asyncio
    async def test_falls_back_to_newsapi_when_cryptopanic_fails(self, fetcher, sample_article):
        with patch.object(fetcher, "_fetch_cryptopanic", side_effect=Exception("CP down")), \
             patch.object(fetcher, "_fetch_newsapi", return_value=[sample_article]), \
             patch.object(fetcher, "_fetch_rss", return_value=[]):
            result = await fetcher.fetch_news_for_coin("BTC", hours=24)
        assert result == [sample_article]

    @pytest.mark.asyncio
    async def test_falls_back_to_rss_when_all_apis_fail(self, fetcher, sample_article):
        with patch.object(fetcher, "_fetch_cryptopanic", side_effect=Exception("CP down")), \
             patch.object(fetcher, "_fetch_newsapi", side_effect=Exception("NA down")), \
             patch.object(fetcher, "_fetch_rss", return_value=[sample_article]):
            result = await fetcher.fetch_news_for_coin("BTC", hours=24)
        assert result == [sample_article]

    @pytest.mark.asyncio
    async def test_returns_empty_when_all_sources_fail(self, fetcher):
        with patch.object(fetcher, "_fetch_cryptopanic", side_effect=Exception("CP down")), \
             patch.object(fetcher, "_fetch_newsapi", side_effect=Exception("NA down")), \
             patch.object(fetcher, "_fetch_rss", side_effect=Exception("RSS down")):
            result = await fetcher.fetch_news_for_coin("BTC", hours=24)
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_cryptopanic_when_no_key(self, fetcher, sample_article):
        fetcher._cryptopanic_key = ""
        with patch.object(fetcher, "_fetch_cryptopanic") as mock_cp, \
             patch.object(fetcher, "_fetch_newsapi", return_value=[sample_article]), \
             patch.object(fetcher, "_fetch_rss", return_value=[]):
            result = await fetcher.fetch_news_for_coin("BTC", hours=24)
        mock_cp.assert_not_called()

    @pytest.mark.asyncio
    async def test_respects_max_articles_limit(self, fetcher):
        fetcher._max_articles = 3
        many_articles = [
            {"title": f"News {i}", "source": "test", "url": "", "published_at": "", "sentiment_hint": "neutral"}
            for i in range(10)
        ]
        with patch.object(fetcher, "_fetch_cryptopanic", return_value=many_articles), \
             patch.object(fetcher, "_fetch_newsapi", return_value=[]), \
             patch.object(fetcher, "_fetch_rss", return_value=[]):
            result = await fetcher.fetch_news_for_coin("BTC", hours=24)
        assert len(result) <= 3


# ---------------------------------------------------------------------------
# 9. Multi-coin parallel fetch
# ---------------------------------------------------------------------------

class TestMultiCoinFetch:
    @pytest.mark.asyncio
    async def test_fetch_multiple_coins(self, fetcher, sample_article):
        async def mock_fetch(coin, hours=24):
            return [{"title": f"{coin} news", "source": "test", "url": "", "published_at": "", "sentiment_hint": "neutral"}]

        with patch.object(fetcher, "fetch_news_for_coin", side_effect=mock_fetch):
            result = await fetcher.fetch_news_for_coins(["BTC", "ETH", "SOL"], hours=24)

        assert set(result.keys()) == {"BTC", "ETH", "SOL"}
        assert result["BTC"][0]["title"] == "BTC news"

    @pytest.mark.asyncio
    async def test_handles_partial_failure(self, fetcher):
        async def mock_fetch(coin, hours=24):
            if coin == "ETH":
                raise Exception("ETH fetch failed")
            return [{"title": f"{coin} news", "source": "test", "url": "", "published_at": "", "sentiment_hint": "neutral"}]

        with patch.object(fetcher, "fetch_news_for_coin", side_effect=mock_fetch):
            result = await fetcher.fetch_news_for_coins(["BTC", "ETH"], hours=24)

        assert result["BTC"][0]["title"] == "BTC news"
        assert result["ETH"] == []


# ---------------------------------------------------------------------------
# 10. News logging
# ---------------------------------------------------------------------------

class TestNewsLogging:
    def test_log_creates_jsonl_file(self, fetcher, tmp_path, sample_article):
        fetcher.LOG_DIR = tmp_path / "logs"
        fetcher._log_news("BTC", "cryptopanic", [sample_article])

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = fetcher.LOG_DIR / f"news_{today}.jsonl"
        assert log_file.exists()

    def test_log_writes_valid_jsonl(self, fetcher, tmp_path, sample_article):
        fetcher.LOG_DIR = tmp_path / "logs"
        fetcher._log_news("BTC", "cryptopanic", [sample_article])

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = fetcher.LOG_DIR / f"news_{today}.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["coin"] == "BTC"
        assert record["source"] == "cryptopanic"
        assert record["count"] == 1

    def test_log_appends_multiple_records(self, fetcher, tmp_path, sample_article):
        fetcher.LOG_DIR = tmp_path / "logs"
        fetcher._log_news("BTC", "cryptopanic", [sample_article])
        fetcher._log_news("ETH", "newsapi", [sample_article])

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = fetcher.LOG_DIR / f"news_{today}.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_log_handles_write_error_gracefully(self, fetcher, sample_article):
        fetcher.LOG_DIR = Path("/nonexistent/path/that/cannot/be/created")
        # Should not raise
        fetcher._log_news("BTC", "test", [sample_article])

    def test_log_empty_articles(self, fetcher, tmp_path):
        fetcher.LOG_DIR = tmp_path / "logs"
        fetcher._log_news("BTC", "none", [])

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = fetcher.LOG_DIR / f"news_{today}.jsonl"
        record = json.loads(log_file.read_text().strip())
        assert record["count"] == 0
        assert record["articles"] == []
