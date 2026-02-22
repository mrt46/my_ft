"""Brutal tests for SentimentAnalyzer.

Tests cover:
- Module import and initialization
- Prompt building (v1 and v2)
- LLM response parsing (valid, malformed, edge cases)
- Aggregation logic (weighted average, confidence, agreement)
- Usability thresholds
- Fallback when LLMs fail (1/3, 2/3, 0/3)
- Telegram notification (mocked)
- Sentiment logging (JSONL)
- get_sentiment / get_all_sentiment end-to-end (mocked LLMs)
- Score clamping (-1.0 to +1.0)
- Confidence clamping (0.0 to 1.0)
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def analyzer(tmp_path, monkeypatch):
    """Return a SentimentAnalyzer with mocked paths and env vars."""
    import yaml

    settings = {
        "sentiment": {
            "llm_timeout_seconds": 5,
            "min_confidence": 0.6,
            "min_llms_required": 2,
            "news_batch_size": 10,
            "news_hours": 24,
            "prompt_version": "v2",
            "weight_deepseek": 0.35,
            "weight_gpt4o": 0.35,
            "weight_gemini": 0.30,
        }
    }
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(yaml.dump(settings))

    monkeypatch.setenv("OPENAI_API_KEY", "test_openai")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test_deepseek")
    monkeypatch.setenv("GEMINI_API_KEY", "test_gemini")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test_chat")

    from custom_modules.sentiment_analyzer import SentimentAnalyzer

    with patch("custom_modules.sentiment_analyzer.Path") as mock_path_cls:
        mock_settings = MagicMock()
        mock_settings.__truediv__ = lambda s, o: settings_file if "settings" in str(o) else (tmp_path / str(o))
        mock_settings.parent = tmp_path
        mock_path_cls.return_value = mock_settings

        a = SentimentAnalyzer.__new__(SentimentAnalyzer)
        a._timeout = 5
        a._min_confidence = 0.6
        a._min_llms = 2
        a._weights = {"deepseek": 0.35, "gpt4o": 0.35, "gemini": 0.30}
        a._prompt_version = "v2"
        a._news_hours = 24
        a._openai_key = "test_openai"
        a._deepseek_key = "test_deepseek"
        a._gemini_key = "test_gemini"
        a._tg_token = "test_token"
        a._tg_chat_id = "test_chat"
        a.SENTIMENT_FILE = tmp_path / "sentiment_scores.json"
        a.LOG_DIR = tmp_path / "logs"
        return a


def make_llm_score(provider="deepseek", sentiment=0.5, confidence=0.8,
                   reasoning="Test reasoning", key_events=None, risk_factors=None):
    from custom_modules.sentiment_analyzer import LLMScore
    return LLMScore(
        provider=provider,
        sentiment=sentiment,
        confidence=confidence,
        reasoning=reasoning,
        key_events=key_events or ["Event 1"],
        risk_factors=risk_factors or ["Risk 1"],
    )


# ---------------------------------------------------------------------------
# 1. Import tests
# ---------------------------------------------------------------------------

class TestImports:
    def test_sentiment_analyzer_importable(self):
        from custom_modules.sentiment_analyzer import SentimentAnalyzer, LLMScore, SentimentResult
        assert SentimentAnalyzer
        assert LLMScore
        assert SentimentResult

    def test_has_required_methods(self):
        from custom_modules.sentiment_analyzer import SentimentAnalyzer
        for method in [
            "get_sentiment", "get_sentiment_sync",
            "get_all_sentiment", "get_all_sentiment_with_news_fetch",
            "get_all_sentiment_with_news_fetch_sync",
            "_call_deepseek", "_call_gpt4o", "_call_gemini",
            "_aggregate", "_parse_llm_response",
            "_log_sentiment", "_save", "_empty_result",
            "_format_single_telegram", "_format_summary_telegram",
            "_send_telegram", "_send_telegram_sync",
        ]:
            assert hasattr(SentimentAnalyzer, method), f"Missing: {method}"

    def test_prompt_templates_exist(self):
        from custom_modules.sentiment_analyzer import _PROMPT_V1, _PROMPT_V2
        assert "{coin}" in _PROMPT_V1
        assert "{coin}" in _PROMPT_V2
        assert "{news_text}" in _PROMPT_V1
        assert "{news_text}" in _PROMPT_V2
        assert "{key_events}" not in _PROMPT_V2  # key_events in output, not input
        assert "key_events" in _PROMPT_V2  # mentioned in output format


# ---------------------------------------------------------------------------
# 2. LLM response parsing
# ---------------------------------------------------------------------------

class TestParseResponse:
    @pytest.fixture(autouse=True)
    def _a(self, analyzer):
        self.a = analyzer

    def test_parse_valid_v1_response(self):
        content = '{"sentiment": 0.7, "confidence": 0.8, "reasoning": "Bullish news"}'
        result = self.a._parse_llm_response(content)
        assert result["sentiment"] == 0.7
        assert result["confidence"] == 0.8
        assert result["reasoning"] == "Bullish news"
        assert result["key_events"] == []
        assert result["risk_factors"] == []

    def test_parse_valid_v2_response(self):
        content = json.dumps({
            "sentiment": 0.6,
            "confidence": 0.9,
            "reasoning": "Strong bullish signals",
            "key_events": ["ETF approved", "Whale accumulation"],
            "risk_factors": ["Regulatory risk"],
        })
        result = self.a._parse_llm_response(content)
        assert result["sentiment"] == 0.6
        assert len(result["key_events"]) == 2
        assert len(result["risk_factors"]) == 1

    def test_parse_clamps_sentiment_above_1(self):
        content = '{"sentiment": 2.5, "confidence": 0.8, "reasoning": "test"}'
        result = self.a._parse_llm_response(content)
        assert result["sentiment"] == 1.0

    def test_parse_clamps_sentiment_below_minus_1(self):
        content = '{"sentiment": -3.0, "confidence": 0.8, "reasoning": "test"}'
        result = self.a._parse_llm_response(content)
        assert result["sentiment"] == -1.0

    def test_parse_clamps_confidence_above_1(self):
        content = '{"sentiment": 0.5, "confidence": 1.5, "reasoning": "test"}'
        result = self.a._parse_llm_response(content)
        assert result["confidence"] == 1.0

    def test_parse_clamps_confidence_below_0(self):
        content = '{"sentiment": 0.5, "confidence": -0.5, "reasoning": "test"}'
        result = self.a._parse_llm_response(content)
        assert result["confidence"] == 0.0

    def test_parse_with_markdown_fences(self):
        content = '```json\n{"sentiment": 0.3, "confidence": 0.7, "reasoning": "test"}\n```'
        result = self.a._parse_llm_response(content)
        assert result["sentiment"] == 0.3

    def test_parse_no_json_raises(self):
        with pytest.raises(ValueError, match="No JSON found"):
            self.a._parse_llm_response("This is just plain text with no JSON")

    def test_parse_missing_sentiment_defaults_to_zero(self):
        content = '{"confidence": 0.8, "reasoning": "test"}'
        result = self.a._parse_llm_response(content)
        assert result["sentiment"] == 0.0

    def test_parse_missing_confidence_defaults_to_half(self):
        content = '{"sentiment": 0.5, "reasoning": "test"}'
        result = self.a._parse_llm_response(content)
        assert result["confidence"] == 0.5

    def test_parse_key_events_as_list(self):
        content = '{"sentiment": 0.5, "confidence": 0.8, "reasoning": "test", "key_events": ["A", "B"]}'
        result = self.a._parse_llm_response(content)
        assert result["key_events"] == ["A", "B"]

    def test_parse_handles_extra_text_before_json(self):
        content = 'Here is my analysis:\n{"sentiment": 0.4, "confidence": 0.7, "reasoning": "test"}'
        result = self.a._parse_llm_response(content)
        assert result["sentiment"] == 0.4


# ---------------------------------------------------------------------------
# 3. Aggregation logic
# ---------------------------------------------------------------------------

class TestAggregation:
    @pytest.fixture(autouse=True)
    def _a(self, analyzer):
        self.a = analyzer

    def test_weighted_average_all_three_llms(self):
        scores = [
            make_llm_score("deepseek", sentiment=0.8, confidence=0.9),
            make_llm_score("gpt4o", sentiment=0.6, confidence=0.8),
            make_llm_score("gemini", sentiment=0.4, confidence=0.7),
        ]
        individual = {s["provider"]: s for s in scores}
        result = self.a._aggregate("BTC", scores, individual, news_count=5, send_telegram=False)

        # Expected: (0.8*0.35 + 0.6*0.35 + 0.4*0.30) / 1.0 = 0.61
        expected = (0.8 * 0.35 + 0.6 * 0.35 + 0.4 * 0.30)
        assert abs(result["sentiment"] - expected) < 0.001

    def test_usable_when_enough_llms_and_confidence(self):
        scores = [
            make_llm_score("deepseek", confidence=0.8),
            make_llm_score("gpt4o", confidence=0.7),
        ]
        individual = {s["provider"]: s for s in scores}
        result = self.a._aggregate("BTC", scores, individual, send_telegram=False)
        assert result["usable"] is True

    def test_not_usable_when_low_confidence(self):
        scores = [
            make_llm_score("deepseek", confidence=0.3),
            make_llm_score("gpt4o", confidence=0.4),
        ]
        individual = {s["provider"]: s for s in scores}
        result = self.a._aggregate("BTC", scores, individual, send_telegram=False)
        assert result["usable"] is False

    def test_not_usable_when_too_few_llms(self):
        scores = [make_llm_score("deepseek", confidence=0.9)]  # only 1, min=2
        individual = {s["provider"]: s for s in scores}
        result = self.a._aggregate("BTC", scores, individual, send_telegram=False)
        assert result["usable"] is False

    def test_empty_scores_returns_empty_result(self):
        result = self.a._aggregate("BTC", [], {}, send_telegram=False)
        assert result["sentiment"] == 0.0
        assert result["usable"] is False
        assert result["coin"] == "BTC"

    def test_agreement_perfect_when_all_same(self):
        scores = [
            make_llm_score("deepseek", sentiment=0.5),
            make_llm_score("gpt4o", sentiment=0.5),
            make_llm_score("gemini", sentiment=0.5),
        ]
        individual = {s["provider"]: s for s in scores}
        result = self.a._aggregate("BTC", scores, individual, send_telegram=False)
        assert result["agreement"] == 1.0

    def test_agreement_low_when_all_different(self):
        scores = [
            make_llm_score("deepseek", sentiment=-1.0),
            make_llm_score("gpt4o", sentiment=0.0),
            make_llm_score("gemini", sentiment=1.0),
        ]
        individual = {s["provider"]: s for s in scores}
        result = self.a._aggregate("BTC", scores, individual, send_telegram=False)
        assert result["agreement"] < 0.5

    def test_result_has_all_required_keys(self):
        scores = [make_llm_score("deepseek"), make_llm_score("gpt4o")]
        individual = {s["provider"]: s for s in scores}
        result = self.a._aggregate("BTC", scores, individual, news_count=3, send_telegram=False)
        for key in ["coin", "sentiment", "confidence", "agreement", "individual_scores",
                    "usable", "timestamp", "news_count", "prompt_version"]:
            assert key in result, f"Missing key: {key}"

    def test_news_count_stored_in_result(self):
        scores = [make_llm_score("deepseek"), make_llm_score("gpt4o")]
        individual = {s["provider"]: s for s in scores}
        result = self.a._aggregate("BTC", scores, individual, news_count=7, send_telegram=False)
        assert result["news_count"] == 7

    def test_prompt_version_stored_in_result(self):
        scores = [make_llm_score("deepseek"), make_llm_score("gpt4o")]
        individual = {s["provider"]: s for s in scores}
        result = self.a._aggregate("BTC", scores, individual, send_telegram=False)
        assert result["prompt_version"] == "v2"


# ---------------------------------------------------------------------------
# 4. get_sentiment end-to-end (mocked LLMs)
# ---------------------------------------------------------------------------

class TestGetSentiment:
    def _mock_llm_score(self, provider, sentiment=0.5, confidence=0.8):
        return make_llm_score(provider, sentiment=sentiment, confidence=confidence)

    @pytest.mark.asyncio
    async def test_get_sentiment_all_llms_succeed(self, analyzer):
        ds = self._mock_llm_score("deepseek", 0.7)
        gpt = self._mock_llm_score("gpt4o", 0.6)
        gem = self._mock_llm_score("gemini", 0.5)

        with patch.object(analyzer, "_call_deepseek", return_value=ds), \
             patch.object(analyzer, "_call_gpt4o", return_value=gpt), \
             patch.object(analyzer, "_call_gemini", return_value=gem):
            result = await analyzer.get_sentiment(["BTC up 5%"], "BTC", _send_telegram=False)

        assert result["coin"] == "BTC"
        assert result["usable"] is True
        assert -1.0 <= result["sentiment"] <= 1.0

    @pytest.mark.asyncio
    async def test_get_sentiment_one_llm_fails(self, analyzer):
        ds = self._mock_llm_score("deepseek", 0.7)
        gpt = self._mock_llm_score("gpt4o", 0.6)

        with patch.object(analyzer, "_call_deepseek", return_value=ds), \
             patch.object(analyzer, "_call_gpt4o", return_value=gpt), \
             patch.object(analyzer, "_call_gemini", side_effect=Exception("Gemini down")):
            result = await analyzer.get_sentiment(["BTC news"], "BTC", _send_telegram=False)

        assert result["usable"] is True  # 2/3 LLMs succeeded, min=2

    @pytest.mark.asyncio
    async def test_get_sentiment_two_llms_fail(self, analyzer):
        ds = self._mock_llm_score("deepseek", 0.7, confidence=0.9)

        with patch.object(analyzer, "_call_deepseek", return_value=ds), \
             patch.object(analyzer, "_call_gpt4o", side_effect=Exception("GPT down")), \
             patch.object(analyzer, "_call_gemini", side_effect=Exception("Gemini down")):
            result = await analyzer.get_sentiment(["BTC news"], "BTC", _send_telegram=False)

        assert result["usable"] is False  # only 1/3 LLMs, min=2

    @pytest.mark.asyncio
    async def test_get_sentiment_all_llms_fail(self, analyzer):
        with patch.object(analyzer, "_call_deepseek", side_effect=Exception("DS down")), \
             patch.object(analyzer, "_call_gpt4o", side_effect=Exception("GPT down")), \
             patch.object(analyzer, "_call_gemini", side_effect=Exception("Gem down")):
            result = await analyzer.get_sentiment(["BTC news"], "BTC", _send_telegram=False)

        assert result["usable"] is False
        assert result["sentiment"] == 0.0

    @pytest.mark.asyncio
    async def test_get_sentiment_empty_news(self, analyzer):
        ds = self._mock_llm_score("deepseek", 0.0, confidence=0.2)
        gpt = self._mock_llm_score("gpt4o", 0.0, confidence=0.2)
        gem = self._mock_llm_score("gemini", 0.0, confidence=0.2)

        with patch.object(analyzer, "_call_deepseek", return_value=ds), \
             patch.object(analyzer, "_call_gpt4o", return_value=gpt), \
             patch.object(analyzer, "_call_gemini", return_value=gem):
            result = await analyzer.get_sentiment([], "BTC", _send_telegram=False)

        assert result["coin"] == "BTC"
        # Low confidence → not usable
        assert result["usable"] is False


# ---------------------------------------------------------------------------
# 5. Sentiment logging
# ---------------------------------------------------------------------------

class TestSentimentLogging:
    def _make_result(self, coin="BTC", sentiment=0.5, confidence=0.8, usable=True):
        return {
            "coin": coin,
            "sentiment": sentiment,
            "confidence": confidence,
            "agreement": 0.9,
            "individual_scores": {
                "deepseek": {"sentiment": sentiment, "confidence": confidence,
                             "reasoning": "test", "key_events": [], "risk_factors": []},
            },
            "usable": usable,
            "timestamp": time.time(),
            "news_count": 5,
            "prompt_version": "v2",
        }

    def test_log_creates_jsonl_file(self, analyzer, tmp_path):
        analyzer.LOG_DIR = tmp_path / "logs"
        result = self._make_result()
        analyzer._log_sentiment(result)

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = analyzer.LOG_DIR / f"sentiment_{today}.jsonl"
        assert log_file.exists()

    def test_log_writes_valid_jsonl(self, analyzer, tmp_path):
        analyzer.LOG_DIR = tmp_path / "logs"
        result = self._make_result("ETH", sentiment=0.3)
        analyzer._log_sentiment(result)

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = analyzer.LOG_DIR / f"sentiment_{today}.jsonl"
        record = json.loads(log_file.read_text().strip())
        assert record["coin"] == "ETH"
        assert record["sentiment"] == 0.3
        assert record["prompt_version"] == "v2"

    def test_log_appends_multiple_coins(self, analyzer, tmp_path):
        analyzer.LOG_DIR = tmp_path / "logs"
        analyzer._log_sentiment(self._make_result("BTC"))
        analyzer._log_sentiment(self._make_result("ETH"))

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = analyzer.LOG_DIR / f"sentiment_{today}.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_log_handles_write_error_gracefully(self, analyzer):
        analyzer.LOG_DIR = Path("/nonexistent/path/cannot/create")
        result = self._make_result()
        analyzer._log_sentiment(result)  # Should not raise

    def test_log_includes_individual_scores(self, analyzer, tmp_path):
        analyzer.LOG_DIR = tmp_path / "logs"
        analyzer._log_sentiment(self._make_result())

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = analyzer.LOG_DIR / f"sentiment_{today}.jsonl"
        record = json.loads(log_file.read_text().strip())
        assert "individual" in record
        assert "deepseek" in record["individual"]


# ---------------------------------------------------------------------------
# 6. Save to sentiment_scores.json
# ---------------------------------------------------------------------------

class TestSave:
    def _make_result(self, coin="BTC"):
        return {
            "coin": coin,
            "sentiment": 0.5,
            "confidence": 0.8,
            "agreement": 0.9,
            "individual_scores": {},
            "usable": True,
            "timestamp": time.time(),
            "news_count": 5,
            "prompt_version": "v2",
        }

    def test_save_creates_file(self, analyzer, tmp_path):
        analyzer.SENTIMENT_FILE = tmp_path / "sentiment_scores.json"
        analyzer._save("BTC", self._make_result("BTC"))
        assert analyzer.SENTIMENT_FILE.exists()

    def test_save_multiple_coins(self, analyzer, tmp_path):
        analyzer.SENTIMENT_FILE = tmp_path / "sentiment_scores.json"
        analyzer._save("BTC", self._make_result("BTC"))
        analyzer._save("ETH", self._make_result("ETH"))

        data = json.loads(analyzer.SENTIMENT_FILE.read_text())
        assert "BTC" in data
        assert "ETH" in data

    def test_save_overwrites_existing_coin(self, analyzer, tmp_path):
        analyzer.SENTIMENT_FILE = tmp_path / "sentiment_scores.json"
        r1 = self._make_result("BTC")
        r1["sentiment"] = 0.3
        analyzer._save("BTC", r1)

        r2 = self._make_result("BTC")
        r2["sentiment"] = 0.7
        analyzer._save("BTC", r2)

        data = json.loads(analyzer.SENTIMENT_FILE.read_text())
        assert data["BTC"]["sentiment"] == 0.7


# ---------------------------------------------------------------------------
# 7. Telegram notification
# ---------------------------------------------------------------------------

class TestTelegramNotification:
    @pytest.mark.asyncio
    async def test_send_telegram_skips_when_no_token(self, analyzer):
        analyzer._tg_token = ""
        analyzer._tg_chat_id = ""
        # Should not raise and should not make HTTP calls
        with patch("aiohttp.ClientSession") as mock_session:
            await analyzer._send_telegram("test message")
        mock_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_telegram_posts_to_api(self, analyzer):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await analyzer._send_telegram("test message")

        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        payload = call_kwargs[1]["json"]
        assert payload["text"] == "test message"
        assert payload["chat_id"] == "test_chat"
        # Must NOT include parse_mode (causes Markdown errors)
        assert "parse_mode" not in payload

    @pytest.mark.asyncio
    async def test_send_telegram_handles_http_error(self, analyzer):
        mock_resp = AsyncMock()
        mock_resp.status = 400
        mock_resp.text = AsyncMock(return_value="Bad Request")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await analyzer._send_telegram("test")  # Should not raise

    @pytest.mark.asyncio
    async def test_send_telegram_handles_network_exception(self, analyzer):
        with patch("aiohttp.ClientSession", side_effect=Exception("Network down")):
            await analyzer._send_telegram("test")  # Should not raise


# ---------------------------------------------------------------------------
# 8. Telegram message formatting
# ---------------------------------------------------------------------------

class TestTelegramFormatting:
    def _make_result(self, coin="BTC", sentiment=0.7, confidence=0.85, usable=True):
        return {
            "coin": coin,
            "sentiment": sentiment,
            "confidence": confidence,
            "agreement": 0.9,
            "individual_scores": {
                "deepseek": {
                    "sentiment": sentiment, "confidence": confidence,
                    "reasoning": "Strong bullish signals from ETF news",
                    "key_events": ["ETF approved", "Whale buying"],
                    "risk_factors": ["Regulatory risk"],
                },
            },
            "usable": usable,
            "timestamp": time.time(),
            "news_count": 8,
            "prompt_version": "v2",
        }

    def test_format_single_contains_coin(self, analyzer):
        result = self._make_result("BTC")
        msg = analyzer._format_single_telegram(result)
        assert "BTC" in msg

    def test_format_single_contains_score(self, analyzer):
        result = self._make_result("BTC", sentiment=0.7)
        msg = analyzer._format_single_telegram(result)
        assert "+0.700" in msg or "0.700" in msg

    def test_format_single_contains_key_events(self, analyzer):
        result = self._make_result("BTC")
        msg = analyzer._format_single_telegram(result)
        assert "ETF approved" in msg

    def test_format_single_contains_risk_factors(self, analyzer):
        result = self._make_result("BTC")
        msg = analyzer._format_single_telegram(result)
        assert "Regulatory risk" in msg

    def test_format_single_no_markdown_special_chars(self, analyzer):
        """Message must be plain text — no *, _, ` that could break Telegram."""
        result = self._make_result("BTC")
        msg = analyzer._format_single_telegram(result)
        # These chars should NOT appear (we use plain text)
        for char in ["*", "_", "`", "["]:
            assert char not in msg, f"Found Markdown char '{char}' in message"

    def test_format_summary_contains_all_coins(self, analyzer):
        results = {
            "BTC": self._make_result("BTC", 0.7),
            "ETH": self._make_result("ETH", 0.3),
            "SOL": self._make_result("SOL", -0.2),
        }
        msg = analyzer._format_summary_telegram(results)
        assert "BTC" in msg
        assert "ETH" in msg
        assert "SOL" in msg

    def test_format_summary_sorted_by_sentiment(self, analyzer):
        results = {
            "SOL": self._make_result("SOL", -0.5),
            "BTC": self._make_result("BTC", 0.8),
            "ETH": self._make_result("ETH", 0.2),
        }
        msg = analyzer._format_summary_telegram(results)
        btc_pos = msg.index("BTC")
        eth_pos = msg.index("ETH")
        sol_pos = msg.index("SOL")
        assert btc_pos < eth_pos < sol_pos  # Sorted descending

    def test_format_summary_includes_market_mood(self, analyzer):
        results = {
            "BTC": self._make_result("BTC", 0.7, usable=True),
            "ETH": self._make_result("ETH", 0.5, usable=True),
        }
        msg = analyzer._format_summary_telegram(results)
        assert "Genel Piyasa" in msg


# ---------------------------------------------------------------------------
# 9. get_all_sentiment (batch)
# ---------------------------------------------------------------------------

class TestGetAllSentiment:
    @pytest.mark.asyncio
    async def test_get_all_returns_all_coins(self, analyzer):
        ds = make_llm_score("deepseek", 0.5, 0.8)
        gpt = make_llm_score("gpt4o", 0.6, 0.8)
        gem = make_llm_score("gemini", 0.4, 0.8)

        with patch.object(analyzer, "_call_deepseek", return_value=ds), \
             patch.object(analyzer, "_call_gpt4o", return_value=gpt), \
             patch.object(analyzer, "_call_gemini", return_value=gem), \
             patch.object(analyzer, "_send_telegram", new_callable=AsyncMock):
            results = await analyzer._get_all_async({
                "BTC": ["BTC news"],
                "ETH": ["ETH news"],
            })

        assert set(results.keys()) == {"BTC", "ETH"}

    @pytest.mark.asyncio
    async def test_get_all_handles_partial_failure(self, analyzer):
        call_count = {"n": 0}

        async def mock_deepseek(prompt, coin):
            call_count["n"] += 1
            if coin == "ETH":
                raise Exception("ETH analysis failed")
            return make_llm_score("deepseek", 0.5, 0.8)

        with patch.object(analyzer, "_call_deepseek", side_effect=mock_deepseek), \
             patch.object(analyzer, "_call_gpt4o", side_effect=Exception("GPT down")), \
             patch.object(analyzer, "_call_gemini", side_effect=Exception("Gem down")), \
             patch.object(analyzer, "_send_telegram", new_callable=AsyncMock):
            results = await analyzer._get_all_async({
                "BTC": ["BTC news"],
                "ETH": ["ETH news"],
            })

        assert "BTC" in results
        assert "ETH" in results
        assert results["ETH"]["usable"] is False

    @pytest.mark.asyncio
    async def test_get_all_sends_summary_telegram(self, analyzer):
        ds = make_llm_score("deepseek", 0.5, 0.8)
        gpt = make_llm_score("gpt4o", 0.6, 0.8)
        gem = make_llm_score("gemini", 0.4, 0.8)

        with patch.object(analyzer, "_call_deepseek", return_value=ds), \
             patch.object(analyzer, "_call_gpt4o", return_value=gpt), \
             patch.object(analyzer, "_call_gemini", return_value=gem), \
             patch.object(analyzer, "_send_telegram", new_callable=AsyncMock) as mock_tg:
            await analyzer._get_all_async({"BTC": ["news"], "ETH": ["news"]})

        mock_tg.assert_called_once()  # Summary sent once


# ---------------------------------------------------------------------------
# 10. Sentiment emoji
# ---------------------------------------------------------------------------

class TestSentimentEmoji:
    @pytest.fixture(autouse=True)
    def _a(self, analyzer):
        self.a = analyzer

    def test_very_bullish(self):
        assert self.a._sentiment_emoji(0.7) == "🟢🟢"

    def test_bullish(self):
        assert self.a._sentiment_emoji(0.4) == "🟢"

    def test_slightly_bullish(self):
        assert self.a._sentiment_emoji(0.15) == "🟡"

    def test_neutral(self):
        assert self.a._sentiment_emoji(0.0) == "⚪"

    def test_slightly_bearish(self):
        assert self.a._sentiment_emoji(-0.2) == "🟠"

    def test_bearish(self):
        assert self.a._sentiment_emoji(-0.5) == "🔴"

    def test_very_bearish(self):
        assert self.a._sentiment_emoji(-0.8) == "🔴🔴"
