"""Sentiment Analyzer — 3-LLM ensemble for market sentiment scoring.

Queries DeepSeek v3, GPT-4o Mini, and Gemini 2.0 Flash in parallel,
then aggregates individual scores into a final consensus sentiment.

Results are saved to:
    - data/sentiment_scores.json       — son sentiment sonuçları (coin → SentimentResult)
    - logs/sentiment_YYYYMMDD.jsonl    — günlük sentiment log (her çalışma kaydedilir)

Prompt versiyonları:
    - v1 (temel): Sadece haber listesi + JSON çıktısı
    - v2 (gelişmiş): Bağlam zenginleştirme, skor rehberi, key_events, risk_factors
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
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
    sentiment: float      # -1.0 (very bearish) … +1.0 (very bullish)
    confidence: float     # 0.0 … 1.0
    reasoning: str
    key_events: list[str]   # Ana olaylar (v2 prompt ile dolar)
    risk_factors: list[str] # Risk faktörleri (v2 prompt ile dolar)


class SentimentResult(TypedDict):
    """Aggregated ensemble sentiment for one coin."""

    coin: str
    sentiment: float       # Weighted average, -1.0 … +1.0
    confidence: float      # Mean of individual confidences
    agreement: float       # Std-deviation based consensus metric, 0.0 … 1.0
    individual_scores: dict[str, LLMScore]
    usable: bool           # False if confidence < threshold or < min_llms
    timestamp: float
    news_count: int        # Kaç haber analiz edildi
    prompt_version: str    # "v1" veya "v2"


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

# v1 — Temel prompt (geriye uyumluluk için korunur)
_PROMPT_V1 = """You are a crypto market analyst. Analyse the following recent news about {coin} and provide a sentiment score.

NEWS:
{news_text}

Respond ONLY with valid JSON in this exact format:
{{
  "sentiment": <float between -1.0 and 1.0, where -1.0=very bearish, 0=neutral, 1.0=very bullish>,
  "confidence": <float between 0.0 and 1.0, how confident you are>,
  "reasoning": "<one sentence explanation>"
}}"""

# v2 — Gelişmiş prompt (bağlam zenginleştirme, skor rehberi, key_events)
_PROMPT_V2 = """You are an expert crypto market analyst specializing in short-term price movements.

TASK: Analyze the sentiment of recent news about {coin} for a SHORT-TERM TRADING SIGNAL (next 4-24 hours).

CONTEXT:
- Analysis date: {date_utc} UTC
- News window: last {hours}h

NEWS (sorted by recency, {news_count} articles):
{news_text}

SCORING GUIDE:
  +0.8 to +1.0 : Strong bullish catalyst (ETF approval, major partnership, exchange listing, halving)
  +0.4 to +0.7 : Moderate bullish (positive adoption news, whale accumulation, protocol upgrade)
  +0.1 to +0.3 : Slightly bullish (minor positive news, community growth, minor partnership)
  -0.1 to +0.1 : Neutral (routine updates, no clear direction, mixed signals)
  -0.1 to -0.3 : Slightly bearish (minor negative news, profit-taking, minor regulatory concern)
  -0.4 to -0.7 : Moderate bearish (regulatory crackdown, competitor advantage, large sell-off)
  -0.8 to -1.0 : Strong bearish catalyst (exchange hack, government ban, major fraud, protocol failure)

CONFIDENCE GUIDE:
  0.9-1.0 : Multiple consistent high-impact signals
  0.7-0.8 : Clear signal but limited sources
  0.5-0.6 : Mixed signals or low-quality sources
  0.3-0.4 : Very limited or ambiguous news
  0.0-0.2 : No relevant news found

Respond ONLY with valid JSON (no markdown, no extra text):
{{
  "sentiment": <float -1.0 to 1.0>,
  "confidence": <float 0.0 to 1.0>,
  "reasoning": "<2-3 sentences with specific news references>",
  "key_events": ["<most impactful event 1>", "<event 2>"],
  "risk_factors": ["<main risk 1>", "<risk 2>"]
}}"""

# Aktif prompt versiyonu (settings.yaml'dan override edilebilir)
_DEFAULT_PROMPT_VERSION = "v2"


# ---------------------------------------------------------------------------
# SentimentAnalyzer
# ---------------------------------------------------------------------------

class SentimentAnalyzer:
    """3-LLM ensemble sentiment analyzer.

    Calls DeepSeek v3, GPT-4o Mini, and Gemini 2.0 Flash in parallel.
    Falls back gracefully if one LLM fails (minimum 2 required).

    Prompt versiyonları:
        - v1: Temel prompt (sadece haber listesi)
        - v2: Gelişmiş prompt (bağlam, skor rehberi, key_events, risk_factors)

    Logging:
        - logs/sentiment_YYYYMMDD.jsonl — her analiz sonucu loglanır

    Example:
        analyzer = SentimentAnalyzer()
        result = asyncio.run(analyzer.get_sentiment(['BTC up 5%', 'ETH ETF approved'], 'BTC'))
    """

    SENTIMENT_FILE = Path(__file__).parent.parent / "data" / "sentiment_scores.json"
    LOG_DIR = Path(__file__).parent.parent / "logs"

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
        self._prompt_version: str = s.get("prompt_version", _DEFAULT_PROMPT_VERSION)
        self._news_hours: int = s.get("news_hours", 24)

        self._openai_key = os.getenv("OPENAI_API_KEY", "")
        self._deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
        self._gemini_key = os.getenv("GEMINI_API_KEY", "")

        # Telegram (fire-and-forget via bot API)
        self._tg_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._tg_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

        logger.info(
            "SentimentAnalyzer initialised — prompt=%s, timeout=%ds, min_conf=%.1f, tg=%s",
            self._prompt_version, self._timeout, self._min_confidence,
            "enabled" if self._tg_token else "disabled",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_sentiment(
        self,
        news_batch: list[str],
        coin: str,
        hours: int | None = None,
        _send_telegram: bool = True,
    ) -> SentimentResult:
        """Fetch and aggregate sentiment from all 3 LLMs.

        Args:
            news_batch: List of recent news headlines/snippets for *coin*.
            coin: Coin symbol, e.g. ``'BTC'``.
            hours: News window in hours (used in v2 prompt context). Defaults to settings value.

        Returns:
            SentimentResult with aggregated score and per-LLM breakdown.

        Error handling:
            - LLM timeout: 30 seconds per call.
            - If fewer than ``min_llms`` succeed, ``usable`` is set to False.
            - If mean confidence < ``min_confidence``, ``usable`` is False.
        """
        news_hours = hours or self._news_hours
        news_count = len(news_batch)
        news_text = "\n".join(f"- {n}" for n in news_batch[:10])

        # Build prompt based on configured version
        if self._prompt_version == "v2":
            prompt = _PROMPT_V2.format(
                coin=coin,
                date_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                hours=news_hours,
                news_count=news_count,
                news_text=news_text if news_text else "- No recent news found.",
            )
        else:
            prompt = _PROMPT_V1.format(coin=coin, news_text=news_text)

        logger.debug(
            "[SENTIMENT] %s: sending %d news items to 3 LLMs (prompt=%s)",
            coin, news_count, self._prompt_version,
        )

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

        return self._aggregate(coin, scores, individual, news_count=news_count, send_telegram=_send_telegram)

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
        return LLMScore(provider="deepseek", **parsed)  # type: ignore[misc]

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
        return LLMScore(provider="gpt4o", **parsed)  # type: ignore[misc]

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
        return LLMScore(provider="gemini", **parsed)  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _aggregate(
        self,
        coin: str,
        scores: list[LLMScore],
        individual: dict[str, LLMScore],
        news_count: int = 0,
        send_telegram: bool = False,
    ) -> SentimentResult:
        """Compute weighted average and consensus metrics.

        Args:
            coin: Coin symbol.
            scores: Successful LLM results.
            individual: Full per-provider mapping.
            news_count: Number of news articles that were analysed.

        Returns:
            SentimentResult with ``usable`` flag.
        """
        if not scores:
            logger.error(f"All LLMs failed for {coin}")
            result = self._empty_result(coin, individual, news_count)
            self._log_sentiment(result)
            if send_telegram:
                self._send_telegram_sync(
                    f"SENTIMENT HATA: {coin}\n"
                    f"Tum LLM'ler basarisiz oldu. Sentiment uygulanmayacak."
                )
            return result

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
            "news_count": news_count,
            "prompt_version": self._prompt_version,
        }

        self._save(coin, result)
        self._log_sentiment(result)
        logger.info(
            "[SENTIMENT] %s: %+.3f (conf=%.2f, agree=%.2f, usable=%s, llms=%d/%d, news=%d, prompt=%s)",
            coin, sentiment, mean_confidence, agreement, usable,
            len(scores), 3, news_count, self._prompt_version,
        )
        # Send individual coin result to Telegram (only when called directly, not via _get_all_async)
        if send_telegram:
            detail_msg = self._format_single_telegram(result)
            self._send_telegram_sync(detail_msg)
        return result

    # ------------------------------------------------------------------
    # Telegram notifications
    # ------------------------------------------------------------------

    def _sentiment_emoji(self, score: float) -> str:
        """Return emoji representing sentiment strength."""
        if score >= 0.6:
            return "🟢🟢"
        elif score >= 0.3:
            return "🟢"
        elif score >= 0.1:
            return "🟡"
        elif score > -0.1:
            return "⚪"
        elif score > -0.3:
            return "🟠"
        elif score > -0.6:
            return "🔴"
        else:
            return "🔴🔴"

    def _format_single_telegram(self, result: SentimentResult) -> str:
        """Format a single coin sentiment result for Telegram.

        Args:
            result: SentimentResult to format.

        Returns:
            Plain-text message string (no Markdown special chars).
        """
        coin = result["coin"]
        score = result["sentiment"]
        conf = result["confidence"]
        agree = result["agreement"]
        usable = result["usable"]
        news_count = result.get("news_count", 0)
        individual = result.get("individual_scores", {})

        emoji = self._sentiment_emoji(score)
        usable_tag = "KULLANILABILIR" if usable else "DUSUK GUVEN"

        # Individual LLM scores
        llm_lines = []
        for provider, sc in individual.items():
            s = sc.get("sentiment", 0.0)
            c = sc.get("confidence", 0.0)
            llm_lines.append(f"  {provider:8s}: {s:+.2f} (conf={c:.2f})")

        # Key events from first available LLM
        key_events: list[str] = []
        risk_factors: list[str] = []
        for sc in individual.values():
            if sc.get("key_events"):
                key_events = sc["key_events"][:2]
            if sc.get("risk_factors"):
                risk_factors = sc["risk_factors"][:2]
            if key_events:
                break

        lines = [
            f"{emoji} SENTIMENT: {coin}",
            f"========================",
            f"Skor    : {score:+.3f}  [{usable_tag}]",
            f"Guven   : {conf:.2f}  Uzlasma: {agree:.2f}",
            f"Haberler: {news_count} adet  Prompt: {result.get('prompt_version','v1')}",
            f"",
            f"LLM Skorlari:",
        ]
        lines.extend(llm_lines)

        if key_events:
            lines.append("")
            lines.append("Onemli Olaylar:")
            for ev in key_events:
                lines.append(f"  + {ev[:80]}")

        if risk_factors:
            lines.append("")
            lines.append("Risk Faktorleri:")
            for rf in risk_factors:
                lines.append(f"  - {rf[:80]}")

        # Reasoning from first LLM
        for sc in individual.values():
            reasoning = sc.get("reasoning", "")
            if reasoning:
                lines.append("")
                lines.append(f"Analiz: {reasoning[:200]}")
                break

        lines.append(f"========================")
        lines.append(datetime.now(timezone.utc).strftime("%H:%M UTC"))
        return "\n".join(lines)

    def _format_summary_telegram(self, results: dict[str, SentimentResult]) -> str:
        """Format a multi-coin sentiment summary for Telegram.

        Args:
            results: Mapping of coin → SentimentResult.

        Returns:
            Plain-text summary message.
        """
        lines = [
            f"SENTIMENT ANALIZ OZETI",
            f"========================",
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"Analiz edilen: {len(results)} coin",
            f"",
        ]

        # Sort by sentiment score descending
        sorted_results = sorted(
            results.items(),
            key=lambda x: x[1].get("sentiment", 0.0),
            reverse=True,
        )

        for coin, r in sorted_results:
            score = r.get("sentiment", 0.0)
            conf = r.get("confidence", 0.0)
            usable = r.get("usable", False)
            emoji = self._sentiment_emoji(score)
            usable_tag = "ok" if usable else "!"
            lines.append(
                f"{emoji} {coin:8s}: {score:+.3f}  conf={conf:.2f}  [{usable_tag}]"
            )

        # Overall market mood
        usable_scores = [
            r["sentiment"] for r in results.values() if r.get("usable", False)
        ]
        if usable_scores:
            avg = sum(usable_scores) / len(usable_scores)
            mood_emoji = self._sentiment_emoji(avg)
            lines.append("")
            lines.append(f"Genel Piyasa: {mood_emoji} {avg:+.3f}")

        lines.append(f"========================")
        return "\n".join(lines)

    async def _send_telegram(self, message: str) -> None:
        """Send a message to Telegram via Bot API (fire-and-forget).

        Uses aiohttp directly to avoid dependency on telegram-bot library.
        Silently skips if token/chat_id not configured.

        Args:
            message: Plain-text message to send.
        """
        if not self._tg_token or not self._tg_chat_id:
            logger.debug("[TG] Telegram not configured — skipping sentiment notification")
            return

        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
            # Do NOT send parse_mode — plain text avoids all Markdown escaping issues
            payload = {
                "chat_id": self._tg_chat_id,
                "text": message,
            }
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("[TG] Telegram send failed: HTTP %d — %s", resp.status, body[:200])
                    else:
                        logger.info("[TG] Sentiment notification sent for message len=%d", len(message))
        except Exception as exc:
            logger.warning("[TG] Failed to send sentiment to Telegram: %s", exc)

    def _send_telegram_sync(self, message: str) -> None:
        """Synchronous wrapper for _send_telegram.

        Safe to call from worker threads. Creates a temporary event loop.

        Args:
            message: Plain-text message to send.
        """
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._send_telegram(message))
            loop.close()
        except Exception as exc:
            logger.warning("[TG] _send_telegram_sync failed: %s", exc)

    def _empty_result(self, coin: str, individual: dict, news_count: int = 0) -> SentimentResult:
        return {
            "coin": coin,
            "sentiment": 0.0,
            "confidence": 0.0,
            "agreement": 0.0,
            "individual_scores": individual,
            "usable": False,
            "timestamp": time.time(),
            "news_count": news_count,
            "prompt_version": self._prompt_version,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_all_async(self, news_map: dict[str, list[str]]) -> dict[str, SentimentResult]:
        # _send_telegram=False: individual results suppressed; summary sent below
        tasks = {coin: self.get_sentiment(news, coin, _send_telegram=False) for coin, news in news_map.items()}
        raw_results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        final: dict[str, SentimentResult] = {
            coin: (
                r if not isinstance(r, Exception)
                else self._empty_result(coin, {}, news_count=len(news_map.get(coin, [])))
            )
            for coin, r in zip(tasks.keys(), raw_results)
        }

        # Send summary report to Telegram after all coins are analysed
        if len(final) > 1:
            summary_msg = self._format_summary_telegram(final)
            await self._send_telegram(summary_msg)
        elif len(final) == 1:
            # Single coin — send detailed report
            result = next(iter(final.values()))
            detail_msg = self._format_single_telegram(result)
            await self._send_telegram(detail_msg)

        return final

    def _log_sentiment(self, result: SentimentResult) -> None:
        """Append a sentiment result to logs/sentiment_YYYYMMDD.jsonl.

        Each line is a self-contained JSON object with key metrics.
        Full individual_scores are included for post-analysis.

        Args:
            result: Completed SentimentResult to log.
        """
        try:
            self.LOG_DIR.mkdir(parents=True, exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            log_file = self.LOG_DIR / f"sentiment_{today}.jsonl"

            # Build compact log record (avoid huge nested objects)
            individual_compact = {
                provider: {
                    "sentiment": score.get("sentiment", 0.0),
                    "confidence": score.get("confidence", 0.0),
                    "reasoning": score.get("reasoning", ""),
                    "key_events": score.get("key_events", []),
                    "risk_factors": score.get("risk_factors", []),
                }
                for provider, score in result.get("individual_scores", {}).items()
            }

            record = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "coin": result["coin"],
                "sentiment": result["sentiment"],
                "confidence": result["confidence"],
                "agreement": result["agreement"],
                "usable": result["usable"],
                "llm_count": len(result.get("individual_scores", {})),
                "news_count": result.get("news_count", 0),
                "prompt_version": result.get("prompt_version", "v1"),
                "individual": individual_compact,
            }

            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        except Exception as exc:
            logger.warning("Failed to write sentiment log: %s", exc)

    def _parse_llm_response(self, content: str) -> dict:
        """Extract JSON from LLM response text.

        Supports both v1 (sentiment, confidence, reasoning) and
        v2 (+ key_events, risk_factors) prompt outputs.

        Args:
            content: Raw LLM output string.

        Returns:
            Dict with keys: ``sentiment``, ``confidence``, ``reasoning``,
            ``key_events``, ``risk_factors``.

        Raises:
            ValueError: When valid JSON with required keys cannot be found.
        """
        import re

        # Extract JSON block (may have markdown fences like ```json ... ```)
        # Try to find the outermost { ... } block
        match = re.search(r"\{.*?\}", content, re.DOTALL)
        if not match:
            raise ValueError(f"No JSON found in LLM response: {content[:100]}")

        data = json.loads(match.group())

        sentiment = float(data.get("sentiment", 0.0))
        confidence = float(data.get("confidence", 0.5))
        reasoning = str(data.get("reasoning", ""))

        # v2 fields (optional — empty list if not present)
        key_events: list[str] = [str(e) for e in data.get("key_events", [])]
        risk_factors: list[str] = [str(r) for r in data.get("risk_factors", [])]

        # Clamp to valid ranges
        sentiment = max(-1.0, min(1.0, sentiment))
        confidence = max(0.0, min(1.0, confidence))

        return {
            "sentiment": sentiment,
            "confidence": confidence,
            "reasoning": reasoning,
            "key_events": key_events,
            "risk_factors": risk_factors,
        }

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
