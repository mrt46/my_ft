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


# ---------------------------------------------------------------------------
# Confidence Threshold Tests
# ---------------------------------------------------------------------------

class TestConfidenceThreshold:
    """Verify sentiment is ignored when confidence < 0.6."""

    def test_low_confidence_marks_unusable(self, analyzer):
        """Sentiment with confidence < 0.6 should be marked unusable."""
        scores = [
            _score("deepseek", 0.8, 0.4),  # confidence 0.4 < 0.6
            _score("gpt4o", 0.7, 0.5),     # confidence 0.5 < 0.6
        ]
        result = analyzer._aggregate("BTC", scores, {})
        assert result["usable"] is False

    def test_mixed_confidence_uses_high_confidence_only(self, analyzer):
        """Only high-confidence LLM scores should contribute to ensemble."""
        scores = [
            _score("deepseek", 0.9, 0.8),  # High confidence
            _score("gpt4o", 0.8, 0.85),    # High confidence
            _score("gemini", 0.1, 0.3),    # Low confidence — should be ignored
        ]
        result = analyzer._aggregate("ETH", scores, {})
        # Result should be closer to 0.85 (avg of deepseek+gpt4o) than 0.6 (with gemini)
        assert result["sentiment"] > 0.5

    def test_exactly_min_confidence_is_usable(self, analyzer):
        """Sentiment at exactly min_confidence (0.6) should be usable."""
        scores = [
            _score("deepseek", 0.5, 0.6),  # Exactly at threshold
            _score("gpt4o", 0.5, 0.6),
        ]
        result = analyzer._aggregate("SOL", scores, {})
        assert result["usable"] is True


# ---------------------------------------------------------------------------
# LLM Fallback Tests
# ---------------------------------------------------------------------------

class TestLLMFallback:
    """Verify 2/3 LLM fallback behavior."""

    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_2_llms(self, analyzer):
        """LLM timeout should fall back to remaining 2 LLMs."""
        analyzer._call_deepseek = AsyncMock(side_effect=asyncio.TimeoutError())
        analyzer._call_gpt4o = AsyncMock(return_value=_score("gpt4o", 0.6, 0.8))
        analyzer._call_gemini = AsyncMock(return_value=_score("gemini", 0.7, 0.75))

        result = await analyzer.get_sentiment(["news"], "BTC")
        assert result["usable"] is True
        assert "gpt4o" in result["individual_scores"]
        assert "gemini" in result["individual_scores"]

    @pytest.mark.asyncio
    async def test_all_llms_fail_returns_neutral(self, analyzer):
        """All LLMs failing should return neutral unusable sentiment."""
        analyzer._call_deepseek = AsyncMock(side_effect=Exception("API error"))
        analyzer._call_gpt4o = AsyncMock(side_effect=Exception("API error"))
        analyzer._call_gemini = AsyncMock(side_effect=Exception("API error"))

        result = await analyzer.get_sentiment(["news"], "XRP")
        assert result["usable"] is False
        assert result["sentiment"] == 0.0

    @pytest.mark.asyncio
    async def test_network_error_falls_back(self, analyzer):
        """Network error on one LLM should fall back to others."""
        analyzer._call_deepseek = AsyncMock(side_effect=ConnectionError("network"))
        analyzer._call_gpt4o = AsyncMock(return_value=_score("gpt4o", 0.4, 0.9))
        analyzer._call_gemini = AsyncMock(return_value=_score("gemini", 0.5, 0.85))

        result = await analyzer.get_sentiment(["news"], "ADA")
        assert result["usable"] is True

    @pytest.mark.asyncio
    async def test_individual_scores_recorded(self, analyzer):
        """Individual LLM scores should be recorded in result."""
        analyzer._call_deepseek = AsyncMock(return_value=_score("deepseek", 0.6, 0.85))
        analyzer._call_gpt4o = AsyncMock(return_value=_score("gpt4o", 0.7, 0.90))
        analyzer._call_gemini = AsyncMock(return_value=_score("gemini", 0.65, 0.80))

        result = await analyzer.get_sentiment(["news"], "BNB")
        assert "deepseek" in result["individual_scores"]
        assert "gpt4o" in result["individual_scores"]
        assert "gemini" in result["individual_scores"]


# ---------------------------------------------------------------------------
# Sentiment Score Range Tests
# ---------------------------------------------------------------------------

class TestSentimentScoreRange:
    """Verify sentiment scores are always within [-1, 1]."""

    def test_aggregate_sentiment_in_range(self, analyzer):
        """Aggregated sentiment must be within [-1, 1]."""
        scores = [
            _score("deepseek", 0.9, 0.9),
            _score("gpt4o", 0.8, 0.85),
            _score("gemini", 0.95, 0.8),
        ]
        result = analyzer._aggregate("BTC", scores, {})
        assert -1.0 <= result["sentiment"] <= 1.0

    def test_aggregate_confidence_in_range(self, analyzer):
        """Aggregated confidence must be within [0, 1]."""
        scores = [
            _score("deepseek", 0.5, 0.8),
            _score("gpt4o", 0.6, 0.9),
        ]
        result = analyzer._aggregate("ETH", scores, {})
        assert 0.0 <= result["confidence"] <= 1.0

    def test_agreement_in_range(self, analyzer):
        """Agreement score must be within [0, 1]."""
        scores = [
            _score("deepseek", 0.3, 0.8),
            _score("gpt4o", 0.7, 0.9),
        ]
        result = analyzer._aggregate("SOL", scores, {})
        assert 0.0 <= result["agreement"] <= 1.0