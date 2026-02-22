"""Unit tests for custom_modules.sentiment_analyzer."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_modules.sentiment_analyzer import SentimentAnalyzer, LLMScore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def analyzer(tmp_path):
    settings = tmp_path / "config" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text(
        "sentiment:\n"
        "  llm_timeout_seconds: 5\n"
        "  min_confidence: 0.6\n"
        "  min_llms_required: 2\n"
        "  weight_deepseek: 0.35\n"
        "  weight_gpt4o: 0.35\n"
        "  weight_gemini: 0.30\n"
    )

    a = SentimentAnalyzer.__new__(SentimentAnalyzer)
    a._timeout = 5
    a._min_confidence = 0.6
    a._min_llms = 2
    a._weights = {"deepseek": 0.35, "gpt4o": 0.35, "gemini": 0.30}
    a._openai_key = "test"
    a._deepseek_key = "test"
    a._gemini_key = "test"
    a.SENTIMENT_FILE = tmp_path / "sentiment_scores.json"
    return a


def _score(provider: str, sentiment: float = 0.5, confidence: float = 0.8) -> LLMScore:
    return LLMScore(
        provider=provider,
        sentiment=sentiment,
        confidence=confidence,
        reasoning="test",
    )


# ---------------------------------------------------------------------------
# _parse_llm_response
# ---------------------------------------------------------------------------

class TestParseLlmResponse:
    def test_parses_clean_json(self, analyzer):
        content = '{"sentiment": 0.7, "confidence": 0.8, "reasoning": "bullish"}'
        result = analyzer._parse_llm_response(content)
        assert result["sentiment"] == 0.7
        assert result["confidence"] == 0.8

    def test_parses_markdown_fenced_json(self, analyzer):
        content = "```json\n{\"sentiment\": -0.3, \"confidence\": 0.9, \"reasoning\": \"ok\"}\n```"
        result = analyzer._parse_llm_response(content)
        assert result["sentiment"] == -0.3

    def test_clamps_sentiment_above_1(self, analyzer):
        content = '{"sentiment": 2.0, "confidence": 0.5, "reasoning": "x"}'
        result = analyzer._parse_llm_response(content)
        assert result["sentiment"] == 1.0

    def test_clamps_sentiment_below_neg1(self, analyzer):
        content = '{"sentiment": -5.0, "confidence": 0.5, "reasoning": "x"}'
        result = analyzer._parse_llm_response(content)
        assert result["sentiment"] == -1.0

    def test_raises_on_no_json(self, analyzer):
        with pytest.raises(ValueError):
            analyzer._parse_llm_response("No JSON here at all")


# ---------------------------------------------------------------------------
# _aggregate
# ---------------------------------------------------------------------------

class TestAggregate:
    def test_weighted_average(self, analyzer):
        scores = [
            _score("deepseek", 0.8, 0.9),
            _score("gpt4o", 0.6, 0.8),
            _score("gemini", 0.7, 0.7),
        ]
        result = analyzer._aggregate("BTC", scores, {})
        # Should be weighted avg near 0.7
        assert 0.5 < result["sentiment"] < 0.9
        assert result["usable"] is True

    def test_not_usable_when_too_few_llms(self, analyzer):
        scores = [_score("deepseek", 0.5, 0.9)]  # only 1 < min_llms=2
        result = analyzer._aggregate("ETH", scores, {})
        assert result["usable"] is False

    def test_not_usable_when_low_confidence(self, analyzer):
        scores = [
            _score("deepseek", 0.5, 0.3),
            _score("gpt4o", 0.5, 0.3),
        ]
        result = analyzer._aggregate("SOL", scores, {})
        assert result["usable"] is False

    def test_empty_scores_returns_unusable(self, analyzer):
        result = analyzer._aggregate("XRP", [], {})
        assert result["sentiment"] == 0.0
        assert result["usable"] is False

    def test_agreement_is_high_when_identical_scores(self, analyzer):
        scores = [
            _score("deepseek", 0.7, 0.9),
            _score("gpt4o", 0.7, 0.9),
            _score("gemini", 0.7, 0.9),
        ]
        result = analyzer._aggregate("BTC", scores, {})
        assert result["agreement"] > 0.9

    def test_agreement_is_low_on_divergence(self, analyzer):
        scores = [
            _score("deepseek", 1.0, 0.9),
            _score("gpt4o", -1.0, 0.9),
        ]
        result = analyzer._aggregate("ADA", scores, {})
        assert result["agreement"] < 0.5


# ---------------------------------------------------------------------------
# get_sentiment (async, mocked LLMs)
# ---------------------------------------------------------------------------

class TestGetSentiment:
    @pytest.mark.asyncio
    async def test_all_llms_succeed(self, analyzer):
        analyzer._call_deepseek = AsyncMock(return_value=_score("deepseek", 0.6, 0.85))
        analyzer._call_gpt4o = AsyncMock(return_value=_score("gpt4o", 0.7, 0.90))
        analyzer._call_gemini = AsyncMock(return_value=_score("gemini", 0.65, 0.80))

        result = await analyzer.get_sentiment(["BTC rises"], "BTC")
        assert result["usable"] is True
        assert "deepseek" in result["individual_scores"]

    @pytest.mark.asyncio
    async def test_one_llm_fails_still_usable(self, analyzer):
        analyzer._call_deepseek = AsyncMock(side_effect=TimeoutError("timeout"))
        analyzer._call_gpt4o = AsyncMock(return_value=_score("gpt4o", 0.5, 0.8))
        analyzer._call_gemini = AsyncMock(return_value=_score("gemini", 0.6, 0.75))

        result = await analyzer.get_sentiment(["news"], "ETH")
        assert result["usable"] is True  # 2/3 OK

    @pytest.mark.asyncio
    async def test_two_llms_fail_not_usable(self, analyzer):
        analyzer._call_deepseek = AsyncMock(side_effect=Exception("fail"))
        analyzer._call_gpt4o = AsyncMock(side_effect=Exception("fail"))
        analyzer._call_gemini = AsyncMock(return_value=_score("gemini", 0.5, 0.9))

        result = await analyzer.get_sentiment(["news"], "SOL")
        assert result["usable"] is False  # only 1/3 OK


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    @pytest.mark.asyncio
    async def test_saves_result(self, analyzer):
        analyzer._call_deepseek = AsyncMock(return_value=_score("deepseek", 0.4, 0.8))
        analyzer._call_gpt4o = AsyncMock(return_value=_score("gpt4o", 0.5, 0.85))
        analyzer._call_gemini = AsyncMock(return_value=_score("gemini", 0.45, 0.9))

        await analyzer.get_sentiment(["news"], "MATIC")
        assert analyzer.SENTIMENT_FILE.exists()
        data = json.loads(analyzer.SENTIMENT_FILE.read_text())
        assert "MATIC" in data
