"""Sentiment Analyzer — 3-LLM ensemble for market sentiment scoring.

Queries DeepSeek v3, GPT-4o Mini, and Gemini 2.0 Flash in parallel,
then aggregates individual scores into a final consensus sentiment.
Results are saved to ``data/sentiment_scores.json``.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import TypedDict

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------

class LLMScore(TypedDict):
    """Raw score returned by a single LLM."""

    provider: str
    sentiment: float    # -1.0 (very bearish) … +1.0 (very bullish)
    confidence: float   # 0.0 … 1.0
    reasoning: str


class SentimentResult(TypedDict):
    """Aggregated ensemble sentiment for one coin."""

    coin: str
    sentiment: float       # Weighted average, -1.0 … +1.0
    confidence: float      # Mean of individual confidences
    agreement: float       # Std-deviation based consensus metric, 0.0 … 1.0
    individual_scores: dict[str, LLMScore]
    usable: bool           # False if confidence < threshold or < min_llms
    timestamp: float


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """You are a crypto market analyst. Analyse the following recent news about {coin} and provide a sentiment score.

NEWS:
{news_text}

Respond ONLY with valid JSON in this exact format:
{{
  "sentiment": <float between -1.0 and 1.0, where -1.0=very bearish, 0=neutral, 1.0=very bullish>,
  "confidence": <float between 0.0 and 1.0, how confident you are>,
  "reasoning": "<one sentence explanation>"
}}"""


# ---------------------------------------------------------------------------
# SentimentAnalyzer
# ---------------------------------------------------------------------------

class SentimentAnalyzer:
    """3-LLM ensemble sentiment analyzer.

    Calls DeepSeek v3, GPT-4o Mini, and Gemini 2.0 Flash in parallel.
    Falls back gracefully if one LLM fails (minimum 2 required).

    Example:
        analyzer = SentimentAnalyzer()
        result = asyncio.run(analyzer.get_sentiment(['BTC up 5%', 'ETH ETF approved'], 'BTC'))
    """

    SENTIMENT_FILE = Path(__file__).parent.parent / "data" / "sentiment_scores.json"

    def __init__(self) -> None:
        """Load settings and initialise API clients."""
        settings_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(settings_path) as fh:
            cfg = yaml.safe_load(fh)

        s = cfg.get("sentiment", {})
        self._timeout: int = s.get("llm_timeout_seconds", 30)
        self._min_confidence: float = s.get("min_confidence", 0.6)
        self._min_llms: int = s.get("min_llms_required", 2)
        self._weights: dict[str, float] = {
            "deepseek": s.get("weight_deepseek", 0.35),
            "gpt4o": s.get("weight_gpt4o", 0.35),
            "gemini": s.get("weight_gemini", 0.30),
        }

        self._openai_key = os.getenv("OPENAI_API_KEY", "")
        self._deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
        self._gemini_key = os.getenv("GEMINI_API_KEY", "")

        logger.info("SentimentAnalyzer initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_sentiment(self, news_batch: list[str], coin: str) -> SentimentResult:
        """Fetch and aggregate sentiment from all 3 LLMs.

        Args:
            news_batch: List of recent news headlines/snippets for *coin*.
            coin: Coin symbol, e.g. ``'BTC'``.

        Returns:
            SentimentResult with aggregated score and per-LLM breakdown.

        Error handling:
            - LLM timeout: 30 seconds per call.
            - If fewer than ``min_llms`` succeed, ``usable`` is set to False.
            - If mean confidence < ``min_confidence``, ``usable`` is False.
        """
        news_text = "\n".join(f"- {n}" for n in news_batch[:10])
        prompt = _PROMPT_TEMPLATE.format(coin=coin, news_text=news_text)

        tasks = [
            self._call_deepseek(prompt, coin),
            self._call_gpt4o(prompt, coin),
            self._call_gemini(prompt, coin),
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        scores: list[LLMScore] = []
        individual: dict[str, LLMScore] = {}

        providers = ["deepseek", "gpt4o", "gemini"]
        for provider, result in zip(providers, raw_results):
            if isinstance(result, Exception):
                logger.warning(f"LLM {provider} failed for {coin}: {result}")
                continue
            scores.append(result)
            individual[provider] = result

        return self._aggregate(coin, scores, individual)

    def get_sentiment_sync(self, news_batch: list[str], coin: str) -> SentimentResult:
        """Synchronous wrapper around ``get_sentiment``.

        Safe to call from a worker thread — creates a dedicated event loop.

        Args:
            news_batch: News headlines.
            coin: Coin symbol.

        Returns:
            SentimentResult.
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.get_sentiment(news_batch, coin))
        finally:
            loop.close()

    def get_all_sentiment(self, news_map: dict[str, list[str]]) -> dict[str, SentimentResult]:
        """Run sentiment analysis for multiple coins.

        This method is safe to call from a worker thread (e.g. via
        ``asyncio.to_thread``). It creates a dedicated event loop so it
        does not conflict with the main asyncio loop.

        Args:
            news_map: Mapping of coin symbol → news list.

        Returns:
            Mapping of coin symbol → SentimentResult.
        """
        # Create a fresh event loop for this worker thread to avoid
        # "no running event loop" or "loop already running" conflicts.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._get_all_async(news_map))
        finally:
            loop.close()

    async def get_all_sentiment_with_news_fetch(
        self, coins: list[str], hours: int = 24
    ) -> dict[str, SentimentResult]:
        """Fetch news and run sentiment analysis for multiple coins.

        This is the RECOMMENDED method for production use. It automatically
        fetches recent news for each coin and then runs the 3-LLM ensemble.

        Args:
            coins: List of coin symbols, e.g. ['BTC', 'ETH', 'SOL'].
            hours: How many hours back to fetch news (default 24).

        Returns:
            Mapping of coin symbol → SentimentResult.

        Example:
            analyzer = SentimentAnalyzer()
            results = await analyzer.get_all_sentiment_with_news_fetch(
                ['BTC', 'ETH'], hours=24
            )
        """
        # Import here to avoid circular import
        from custom_modules.news_fetcher import NewsFetcher

        fetcher = NewsFetcher()

        # Fetch news for all coins
        logger.info(f"Fetching news for {len(coins)} coins...")
        news_map = await fetcher.fetch_news_for_coins(coins, hours)

        # Convert to title lists for sentiment analysis
        news_title_map = {
            coin: [article["title"] for article in articles]
            for coin, articles in news_map.items()
        }

        # Run sentiment analysis
        return await self._get_all_async(news_title_map)

    def get_all_sentiment_with_news_fetch_sync(
        self, coins: list[str], hours: int = 24
    ) -> dict[str, SentimentResult]:
        """Synchronous wrapper for get_all_sentiment_with_news_fetch.

        Safe to call from worker threads.

        Args:
            coins: List of coin symbols.
            hours: How many hours back to fetch news.

        Returns:
            Mapping of coin symbol → SentimentResult.
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.get_all_sentiment_with_news_fetch(coins, hours)
            )
        finally:
            loop.close()

    # ------------------------------------------------------------------
    # LLM callers
    # ------------------------------------------------------------------

    async def _call_deepseek(self, prompt: str, coin: str) -> LLMScore:
        """Call DeepSeek v3 via OpenAI-compatible API.

        Args:
            prompt: Full prompt text.
            coin: Coin being analysed.

        Returns:
            LLMScore from DeepSeek.

        Raises:
            Exception on timeout or parse error.
        """
        import aiohttp

        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._deepseek_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 200,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=self._timeout)
            ) as resp:
                data = await resp.json()

        # Handle API-level errors (e.g. invalid key, rate limit, quota)
        if "error" in data:
            raise ValueError(f"DeepSeek API error: {data['error']}")
        if "choices" not in data or not data["choices"]:
            raise ValueError(f"DeepSeek unexpected response (no choices): {str(data)[:200]}")

        content = data["choices"][0]["message"]["content"]
        parsed = self._parse_llm_response(content)
        return LLMScore(provider="deepseek", **parsed)

    async def _call_gpt4o(self, prompt: str, coin: str) -> LLMScore:
        """Call GPT-4o Mini via OpenAI API.

        Args:
            prompt: Full prompt text.
            coin: Coin being analysed.

        Returns:
            LLMScore from GPT-4o Mini.

        Raises:
            Exception on timeout or parse error.
        """
        import openai

        client = openai.AsyncOpenAI(api_key=self._openai_key)
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=200,
            ),
            timeout=self._timeout,
        )
        content = response.choices[0].message.content or ""
        parsed = self._parse_llm_response(content)
        return LLMScore(provider="gpt4o", **parsed)

    async def _call_gemini(self, prompt: str, coin: str) -> LLMScore:
        """Call Gemini 2.0 Flash via Google GenAI API (google-genai package).

        Args:
            prompt: Full prompt text.
            coin: Coin being analysed.

        Returns:
            LLMScore from Gemini.

        Raises:
            Exception on timeout or parse error.
        """
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(api_key=self._gemini_key)

        def _sync_call() -> str:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=200,
                ),
            )
            return response.text or ""

        content = await asyncio.wait_for(
            asyncio.to_thread(_sync_call),
            timeout=self._timeout,
        )
        parsed = self._parse_llm_response(content)
        return LLMScore(provider="gemini", **parsed)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _aggregate(
        self,
        coin: str,
        scores: list[LLMScore],
        individual: dict[str, LLMScore],
    ) -> SentimentResult:
        """Compute weighted average and consensus metrics.

        Args:
            coin: Coin symbol.
            scores: Successful LLM results.
            individual: Full per-provider mapping.

        Returns:
            SentimentResult with ``usable`` flag.
        """
        if not scores:
            logger.error(f"All LLMs failed for {coin}")
            return self._empty_result(coin, individual)

        if len(scores) < self._min_llms:
            logger.warning(f"Only {len(scores)} LLMs succeeded for {coin} (min={self._min_llms})")

        # Weighted sentiment
        total_weight = 0.0
        weighted_sentiment = 0.0
        for score in scores:
            w = self._weights.get(score["provider"], 1 / 3)
            weighted_sentiment += score["sentiment"] * w
            total_weight += w

        sentiment = weighted_sentiment / total_weight if total_weight else 0.0

        # Confidence and agreement
        confidences = [s["confidence"] for s in scores]
        mean_confidence = sum(confidences) / len(confidences)
        sentiments = [s["sentiment"] for s in scores]
        std = (sum((x - sentiment) ** 2 for x in sentiments) / len(sentiments)) ** 0.5
        agreement = max(0.0, 1.0 - std)  # 1 = perfect agreement, 0 = no agreement

        usable = len(scores) >= self._min_llms and mean_confidence >= self._min_confidence

        result: SentimentResult = {
            "coin": coin,
            "sentiment": round(sentiment, 4),
            "confidence": round(mean_confidence, 4),
            "agreement": round(agreement, 4),
            "individual_scores": individual,
            "usable": usable,
            "timestamp": time.time(),
        }

        self._save(coin, result)
        logger.info(
            f"Sentiment {coin}: {sentiment:+.3f} (conf={mean_confidence:.2f}, "
            f"agree={agreement:.2f}, usable={usable})"
        )
        return result

    def _empty_result(self, coin: str, individual: dict) -> SentimentResult:
        return {
            "coin": coin,
            "sentiment": 0.0,
            "confidence": 0.0,
            "agreement": 0.0,
            "individual_scores": individual,
            "usable": False,
            "timestamp": time.time(),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_all_async(self, news_map: dict[str, list[str]]) -> dict[str, SentimentResult]:
        tasks = {coin: self.get_sentiment(news, coin) for coin, news in news_map.items()}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        return {
            coin: (r if not isinstance(r, Exception) else self._empty_result(coin, {}))
            for coin, r in zip(tasks.keys(), results)
        }

    def _parse_llm_response(self, content: str) -> dict:
        """Extract JSON from LLM response text.

        Args:
            content: Raw LLM output string.

        Returns:
            Dict with keys ``sentiment``, ``confidence``, ``reasoning``.

        Raises:
            ValueError: When valid JSON with required keys cannot be found.
        """
        import re

        # Extract JSON block (may have markdown fences)
        match = re.search(r"\{.*?\}", content, re.DOTALL)
        if not match:
            raise ValueError(f"No JSON found in LLM response: {content[:100]}")

        data = json.loads(match.group())

        sentiment = float(data.get("sentiment", 0.0))
        confidence = float(data.get("confidence", 0.5))
        reasoning = str(data.get("reasoning", ""))

        # Clamp to valid ranges
        sentiment = max(-1.0, min(1.0, sentiment))
        confidence = max(0.0, min(1.0, confidence))

        return {"sentiment": sentiment, "confidence": confidence, "reasoning": reasoning}

    def _save(self, coin: str, result: SentimentResult) -> None:
        try:
            self.SENTIMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
            existing: dict = {}
            if self.SENTIMENT_FILE.exists():
                existing = json.loads(self.SENTIMENT_FILE.read_text())
            existing[coin] = result
            self.SENTIMENT_FILE.write_text(json.dumps(existing, indent=2))
        except Exception as exc:
            logger.error(f"Failed to save sentiment for {coin}: {exc}")
