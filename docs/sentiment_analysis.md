# Sentiment Analiz Sistemi — Teknik Dokümantasyon

> **Versiyon:** 1.1  
> **Son güncelleme:** 2026-02-22  
> **Modüller:** `custom_modules/sentiment_analyzer.py`, `custom_modules/news_fetcher.py`

---

## İçindekiler

1. [Genel Bakış](#genel-bakış)
2. [Mimari](#mimari)
3. [Haber Kaynakları](#haber-kaynakları)
4. [LLM Prompt'ları](#llm-promptları)
5. [Çıktı Formatları](#çıktı-formatları)
6. [Ağırlıklandırma & Agregasyon](#ağırlıklandırma--agregasyon)
7. [Haber Loglama](#haber-loglama)
8. [Sentiment Loglama](#sentiment-loglama)
9. [Güven Eşikleri & Fallback](#güven-eşikleri--fallback)
10. [Grid Fusion Entegrasyonu](#grid-fusion-entegrasyonu)
11. [Geliştirme Planı](#geliştirme-planı)
12. [Hata Ayıklama Rehberi](#hata-ayıklama-rehberi)

---

## Genel Bakış

Sistem, 3 farklı LLM'i paralel olarak çalıştırarak **ensemble sentiment skoru** üretir.
Tek bir LLM'e güvenmek yerine, 3 modelin ağırlıklı ortalaması alınır; bu sayede:

- Tek model hatasına karşı dayanıklılık
- Daha tutarlı ve güvenilir skorlar
- Bireysel model bias'larının dengelenmesi

```
Haberler (CryptoPanic / NewsAPI / RSS)
        │
        ▼
  NewsFetcher (cache + log)
        │
        ├──► DeepSeek v3  ──┐
        ├──► GPT-4o Mini ───┼──► Agregasyon ──► SentimentResult
        └──► Gemini 2.0  ──┘         │
                                     ▼
                            data/sentiment_scores.json
                            logs/sentiment_YYYYMMDD.jsonl
                            logs/news_YYYYMMDD.jsonl
```

---

## Mimari

### Dosya Yapısı

```
custom_modules/
├── sentiment_analyzer.py   # 3-LLM ensemble, agregasyon, kaydetme
├── news_fetcher.py         # Haber çekme, cache, loglama
└── grid_fusion.py          # Sentiment → grid seviye ayarı

data/
├── sentiment_scores.json   # Son sentiment sonuçları (coin → SentimentResult)
└── news_cache.json         # Haber cache (30 dakika TTL)

logs/
├── sentiment_YYYYMMDD.jsonl  # Günlük sentiment log (her çalışma)
└── news_YYYYMMDD.jsonl       # Günlük haber log (çekilen tüm haberler)
```

### Çalışma Akışı

```
Her 2 saatte bir (grid analizi ile birlikte):
  1. NewsFetcher.fetch_news_for_coins(coins, hours=24)
     → CryptoPanic → NewsAPI → RSS (fallback zinciri)
     → logs/news_YYYYMMDD.jsonl'a yaz
  
  2. SentimentAnalyzer.get_all_sentiment(news_map)
     → DeepSeek + GPT-4o + Gemini paralel çağrı
     → Agregasyon (ağırlıklı ortalama)
     → data/sentiment_scores.json'a kaydet
     → logs/sentiment_YYYYMMDD.jsonl'a yaz
  
  3. GridFusion.merge(base_grid, sentiment_scores)
     → Sentiment skoru grid seviyelerini ±%5 kaydırır
     → data/final_grid.json üretir
```

---

## Haber Kaynakları

### 1. CryptoPanic (Birincil)

- **URL:** `https://cryptopanic.com/api/v1/posts/`
- **Limit:** 100 istek/gün (ücretsiz tier)
- **Avantaj:** Kripto odaklı, hızlı, sentiment_hint içeriyor
- **Env key:** `CRYPTOPANIC_API_KEY`

```python
params = {
    "auth_token": key,
    "currencies": "BTC",   # Coin sembolü
    "kind": "news",
    "public": "true",
}
```

### 2. NewsAPI (Fallback 1)

- **URL:** `https://newsapi.org/v2/everything`
- **Limit:** 100 istek/gün (ücretsiz tier)
- **Avantaj:** Geniş kaynak yelpazesi
- **Env key:** `NEWSAPI_KEY`

```python
params = {
    "q": "BTC crypto",
    "from": "2026-02-21",
    "sortBy": "relevancy",
    "language": "en",
    "pageSize": 10,
}
```

### 3. RSS Feeds (Fallback 2 — API key gerektirmez)

| Kaynak | URL |
|--------|-----|
| CoinDesk | `https://www.coindesk.com/arc/outboundfeeds/rss/` |
| CoinTelegraph | `https://cointelegraph.com/rss` |
| Decrypt | `https://decrypt.co/feed` |

**Filtreleme:** Coin sembolü başlık veya özette geçmeli.

### Cache Stratejisi

- **TTL:** 30 dakika (`settings.yaml → news.cache_ttl_minutes`)
- **Dosya:** `data/news_cache.json`
- **Mantık:** Aynı coin için 30 dakika içinde tekrar istek gelirse cache'den döner

---

## LLM Prompt'ları

### Mevcut Prompt (v1 — Temel)

```
You are a crypto market analyst. Analyse the following recent news about {coin} and provide a sentiment score.

NEWS:
{news_text}

Respond ONLY with valid JSON in this exact format:
{
  "sentiment": <float between -1.0 and 1.0, where -1.0=very bearish, 0=neutral, 1.0=very bullish>,
  "confidence": <float between 0.0 and 1.0, how confident you are>,
  "reasoning": "<one sentence explanation>"
}
```

**Parametreler:**
- `{coin}` → Coin sembolü (örn. `BTC`, `ETH`)
- `{news_text}` → Haber başlıkları, her biri `- ` ile başlayan liste (max 10 haber)

**Model ayarları:**
- `temperature: 0.1` — Deterministik çıktı için düşük sıcaklık
- `max_tokens: 200` — Sadece JSON çıktısı için yeterli

---

### Geliştirilmiş Prompt (v2 — Planlanan)

```
You are an expert crypto market analyst specializing in short-term price movements.

TASK: Analyze the sentiment of recent news about {coin} ({coin_full_name}) for a SHORT-TERM TRADING SIGNAL (next 4-24 hours).

CONTEXT:
- Current price trend: {price_trend}  (e.g. "up 2.3% in last 4h")
- Market cap rank: #{market_cap_rank}
- Analysis date: {date_utc} UTC

NEWS (last {hours}h, sorted by recency):
{news_text}

SCORING GUIDE:
  +0.8 to +1.0 : Strong bullish catalyst (ETF approval, major partnership, exchange listing)
  +0.4 to +0.7 : Moderate bullish (positive earnings, adoption news, whale accumulation)
  +0.1 to +0.3 : Slightly bullish (minor positive news, community growth)
  -0.1 to +0.1 : Neutral (routine updates, no clear direction)
  -0.1 to -0.3 : Slightly bearish (minor negative news, profit-taking)
  -0.4 to -0.7 : Moderate bearish (regulatory concern, competitor news)
  -0.8 to -1.0 : Strong bearish catalyst (hack, ban, major sell-off, fraud)

CONFIDENCE GUIDE:
  0.9-1.0 : Multiple consistent signals, high-impact news
  0.7-0.8 : Clear signal but limited sources
  0.5-0.6 : Mixed signals or low-quality sources
  0.3-0.4 : Very limited or ambiguous news
  0.0-0.2 : No relevant news found

Respond ONLY with valid JSON:
{
  "sentiment": <float -1.0 to 1.0>,
  "confidence": <float 0.0 to 1.0>,
  "reasoning": "<2-3 sentence explanation with specific news references>",
  "key_events": ["<event1>", "<event2>"],
  "risk_factors": ["<risk1>"]
}
```

**v2 Yenilikleri:**
- Coin tam adı ve piyasa sıralaması eklendi (bağlam zenginleştirme)
- Fiyat trendi bilgisi eklendi (teknik + fundamental birleşimi)
- Detaylı skor rehberi (LLM'in kalibrasyonunu iyileştirir)
- `key_events` ve `risk_factors` alanları (daha zengin çıktı)
- `reasoning` 2-3 cümleye genişletildi

---

## Çıktı Formatları

### LLMScore (Tek Model Çıktısı)

```python
class LLMScore(TypedDict):
    provider: str        # "deepseek" | "gpt4o" | "gemini"
    sentiment: float     # -1.0 … +1.0
    confidence: float    # 0.0 … 1.0
    reasoning: str       # Açıklama cümlesi
```

### SentimentResult (Ensemble Çıktısı)

```python
class SentimentResult(TypedDict):
    coin: str                              # "BTC"
    sentiment: float                       # Ağırlıklı ortalama, -1.0 … +1.0
    confidence: float                      # Ortalama güven
    agreement: float                       # 0=anlaşmazlık, 1=tam uyum
    individual_scores: dict[str, LLMScore] # Her LLM'in ham skoru
    usable: bool                           # False → grid'e uygulanmaz
    timestamp: float                       # Unix timestamp
```

### Örnek Çıktı (data/sentiment_scores.json)

```json
{
  "BTC": {
    "coin": "BTC",
    "sentiment": 0.4250,
    "confidence": 0.7833,
    "agreement": 0.8912,
    "individual_scores": {
      "deepseek": {
        "provider": "deepseek",
        "sentiment": 0.45,
        "confidence": 0.80,
        "reasoning": "Bitcoin ETF inflows continue to accelerate, suggesting strong institutional demand."
      },
      "gpt4o": {
        "provider": "gpt4o",
        "sentiment": 0.40,
        "confidence": 0.75,
        "reasoning": "Positive ETF news offset by minor regulatory concerns in Asia."
      },
      "gemini": {
        "provider": "gemini",
        "sentiment": 0.43,
        "confidence": 0.80,
        "reasoning": "Overall bullish sentiment driven by ETF momentum and whale accumulation."
      }
    },
    "usable": true,
    "timestamp": 1740268800.0
  }
}
```

### Sentiment Skoru Yorumlama

| Skor | Anlam | Grid Etkisi |
|------|-------|-------------|
| +0.7 … +1.0 | Güçlü boğa | Grid üst sınırı +%5 genişler |
| +0.3 … +0.7 | Orta boğa | Grid üst sınırı +%2 genişler |
| -0.3 … +0.3 | Nötr | Grid değişmez |
| -0.7 … -0.3 | Orta ayı | Grid alt sınırı -%2 daralır |
| -1.0 … -0.7 | Güçlü ayı | Grid alt sınırı -%5 daralır |

---

## Ağırlıklandırma & Agregasyon

### Model Ağırlıkları (settings.yaml)

```yaml
sentiment:
  weight_deepseek: 0.35   # DeepSeek v3 — kripto haberlerde güçlü
  weight_gpt4o: 0.35      # GPT-4o Mini — genel piyasa anlayışı
  weight_gemini: 0.30     # Gemini 2.0 Flash — hız/maliyet dengesi
```

### Formüller

```python
# Ağırlıklı sentiment
weighted_sentiment = Σ(score_i × weight_i) / Σ(weight_i)

# Agreement (1 = tam uyum, 0 = tam anlaşmazlık)
std = sqrt(Σ(score_i - mean)² / n)
agreement = max(0.0, 1.0 - std)

# Kullanılabilirlik kontrolü
usable = (başarılı_llm_sayısı >= 2) AND (mean_confidence >= 0.6)
```

### Ağırlık Değiştirme Rehberi

Modellerin performansını izleyerek ağırlıkları ayarlayabilirsiniz:

```yaml
# DeepSeek daha iyi tahmin ediyorsa:
weight_deepseek: 0.45
weight_gpt4o: 0.30
weight_gemini: 0.25

# Hız öncelikliyse (Gemini en hızlı):
weight_deepseek: 0.30
weight_gpt4o: 0.30
weight_gemini: 0.40
```

---

## Haber Loglama

Her çekilen haber `logs/news_YYYYMMDD.jsonl` dosyasına JSONL formatında kaydedilir.

### Log Formatı (JSONL — her satır bir JSON)

```json
{"ts": "2026-02-22T14:30:00Z", "coin": "BTC", "source": "cryptopanic", "count": 8, "articles": [{"title": "Bitcoin ETF inflows hit record...", "source": "cryptopanic", "url": "https://...", "published_at": "2026-02-22T13:45:00Z", "sentiment_hint": "positive"}, ...]}
{"ts": "2026-02-22T14:30:01Z", "coin": "ETH", "source": "rss", "count": 3, "articles": [...]}
```

### Neden JSONL?

- Satır bazlı okuma (büyük dosyalarda verimli)
- Her satır bağımsız JSON (parse hatası tek satırı etkiler)
- `grep`, `jq` ile kolay filtreleme
- Günlük rotasyon ile dosya boyutu kontrolü

### Haber Logunu Okuma

```bash
# Bugünkü BTC haberlerini göster
grep '"coin": "BTC"' logs/news_20260222.jsonl | python -m json.tool

# Tüm kaynakları say
grep -o '"source": "[^"]*"' logs/news_20260222.jsonl | sort | uniq -c
```

---

## Sentiment Loglama

Her sentiment analizi sonucu `logs/sentiment_YYYYMMDD.jsonl` dosyasına kaydedilir.

### Log Formatı

```json
{"ts": "2026-02-22T14:31:00Z", "coin": "BTC", "sentiment": 0.425, "confidence": 0.783, "agreement": 0.891, "usable": true, "llm_count": 3, "individual": {"deepseek": 0.45, "gpt4o": 0.40, "gemini": 0.43}, "news_count": 8, "news_source": "cryptopanic"}
{"ts": "2026-02-22T14:31:02Z", "coin": "ETH", "sentiment": -0.12, "confidence": 0.65, "agreement": 0.72, "usable": true, "llm_count": 2, "individual": {"deepseek": -0.15, "gpt4o": -0.09, "gemini": null}, "news_count": 3, "news_source": "rss"}
```

### Sentiment Logunu Analiz Etme

```bash
# Bugünkü tüm sentiment skorlarını göster
cat logs/sentiment_20260222.jsonl | python -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line)
    print(f\"{d['coin']:8s} {d['sentiment']:+.3f} conf={d['confidence']:.2f} {'✓' if d['usable'] else '✗'}\")"

# Düşük güvenli sonuçları bul
grep '"usable": false' logs/sentiment_20260222.jsonl
```

---

## Güven Eşikleri & Fallback

### Eşik Değerleri (settings.yaml)

```yaml
sentiment:
  min_confidence: 0.6      # Bu altı → usable=False, grid'e uygulanmaz
  min_llms_required: 2     # En az 2 LLM başarılı olmalı
  llm_timeout_seconds: 30  # LLM başına timeout
```

### Fallback Senaryoları

| Durum | Davranış |
|-------|----------|
| 1 LLM başarısız | Kalan 2 ile devam (ağırlıklar normalize edilir) |
| 2 LLM başarısız | `usable=False`, grid değişmez |
| 3 LLM başarısız | `sentiment=0.0, usable=False` |
| `confidence < 0.6` | `usable=False`, grid değişmez |
| Haber bulunamadı | Boş liste ile LLM çağrılır, düşük confidence beklenir |
| API timeout (30s) | Exception yakalanır, o LLM atlanır |

### usable=False Durumunda

Grid fusion modülü `usable=False` olan sentiment'i **tamamen yok sayar**:

```python
# grid_fusion.py
if not sentiment_result.get("usable", False):
    logger.info(f"Sentiment for {coin} not usable, keeping base grid")
    return base_grid  # Değiştirilmemiş grid döner
```

---

## Grid Fusion Entegrasyonu

Sentiment skoru, grid seviyelerini şu şekilde etkiler:

```python
# grid_fusion.py — sentiment_shift hesabı
def _apply_sentiment_shift(self, grid: GridConfig, sentiment: float) -> GridConfig:
    """
    Sentiment > +0.3  → upper_bound %2-5 yukarı kaydır
    Sentiment < -0.3  → lower_bound %2-5 aşağı kaydır
    Nötr (-0.3..+0.3) → değişiklik yok
    """
    shift = 0.0
    if sentiment > 0.3:
        shift = min(0.05, sentiment * 0.1)   # max +%5
    elif sentiment < -0.3:
        shift = max(-0.05, sentiment * 0.1)  # max -%5

    if shift == 0.0:
        return grid

    new_grid = grid.copy()
    new_grid["upper_bound"] *= (1 + shift)
    new_grid["lower_bound"] *= (1 + shift)
    new_grid["levels"] = [lv * (1 + shift) for lv in grid["levels"]]
    new_grid["sentiment_score"] = sentiment
    return new_grid
```

### Örnek Etki

```
BTC grid (base):  lower=90,000  upper=100,000  levels=[90k, 92k, 94k, 96k, 98k, 100k]
Sentiment: +0.45 (boğa, usable=True)
Shift: +0.045 (+%4.5)

BTC grid (final): lower=94,050  upper=104,500  levels=[94k, 96.1k, 98.2k, 100.3k, 102.4k, 104.5k]
```

---

## Geliştirme Planı

### ✅ Tamamlanan (v1.0)

- [x] 3-LLM paralel çağrı (DeepSeek, GPT-4o, Gemini)
- [x] Ağırlıklı agregasyon
- [x] `usable` flag ile güven kontrolü
- [x] `data/sentiment_scores.json` kaydetme
- [x] Fallback: 2/3 LLM yeterli
- [x] Haber kaynakları: CryptoPanic, NewsAPI, RSS

### 🔄 Devam Eden (v1.1)

- [x] `logs/news_YYYYMMDD.jsonl` — haber loglama
- [x] `logs/sentiment_YYYYMMDD.jsonl` — sentiment loglama
- [x] Gelişmiş prompt (v2) — bağlam zenginleştirme
- [x] `key_events` ve `risk_factors` alanları

### 📋 Planlanan (v1.2)

- [ ] **Prompt A/B testi:** v1 vs v2 prompt'larını karşılaştır, doğruluk metriği ekle
- [ ] **LLM kalibrasyon skoru:** Her modelin geçmiş tahminlerini gerçek fiyat hareketleriyle karşılaştır, ağırlıkları otomatik güncelle
- [ ] **Haber kalite filtresi:** Spam/tekrar haberleri filtrele (cosine similarity)
- [ ] **Çoklu dil desteği:** Türkçe/Çince haber kaynaklarını ekle
- [ ] **Sosyal medya sentiment:** Twitter/X API veya Reddit (r/CryptoCurrency)
- [ ] **On-chain sentiment:** Whale Alert, büyük transfer bildirimleri

### 📋 Planlanan (v2.0)

- [ ] **Fine-tuned model:** Kripto haberlerine özel fine-tune edilmiş küçük model (Mistral 7B)
- [ ] **Real-time sentiment:** WebSocket ile anlık haber akışı
- [ ] **Sentiment momentum:** Sentiment değişim hızı (delta) grid'e ek sinyal olarak
- [ ] **Cross-coin correlation:** BTC sentiment ETH grid'ini de etkiler
- [ ] **Telegram `/sentiment` komutu:** Anlık sentiment raporu

---

## Hata Ayıklama Rehberi

### Sık Karşılaşılan Sorunlar

#### 1. Tüm LLM'ler başarısız

```
ERROR sentiment_analyzer: All LLMs failed for BTC
```

**Kontrol:**
```bash
# API key'leri kontrol et
grep -E "DEEPSEEK|OPENAI|GEMINI" .env

# Ağ bağlantısını test et
curl -s https://api.deepseek.com/v1/models -H "Authorization: Bearer $DEEPSEEK_API_KEY"
```

#### 2. Düşük confidence (usable=False)

```
WARNING sentiment_analyzer: Sentiment for ETH not usable (conf=0.42)
```

**Sebep:** Haber bulunamadı veya haberler çelişkili.  
**Çözüm:** `logs/news_YYYYMMDD.jsonl` dosyasında ETH haberlerini kontrol et.

#### 3. JSON parse hatası

```
ValueError: No JSON found in LLM response: ...
```

**Sebep:** LLM JSON yerine düz metin döndürdü.  
**Çözüm:** `temperature` değerini 0.1'de tut. Prompt'a `"Respond ONLY with valid JSON"` vurgusunu ekle.

#### 4. Timeout

```
asyncio.TimeoutError: LLM gpt4o timed out after 30s
```

**Çözüm:** `settings.yaml → sentiment.llm_timeout_seconds: 45` olarak artır.

### Log Dosyalarını İzleme

```bash
# Canlı sentiment logunu izle
Get-Content logs\sentiment_20260222.jsonl -Wait -Tail 5

# Son 10 sentiment sonucunu göster
Get-Content logs\sentiment_20260222.jsonl | Select-Object -Last 10

# Haber kaynaklarını özetle
Select-String '"source"' logs\news_20260222.jsonl | Group-Object | Format-Table
```

---

## Konfigürasyon Referansı

### settings.yaml — Sentiment Bölümü

```yaml
sentiment:
  llm_timeout_seconds: 30      # LLM başına max bekleme süresi
  min_confidence: 0.6          # Bu altı → usable=False
  min_llms_required: 2         # En az kaç LLM başarılı olmalı
  news_batch_size: 10          # LLM'e gönderilecek max haber sayısı
  weight_deepseek: 0.35        # DeepSeek ağırlığı
  weight_gpt4o: 0.35           # GPT-4o Mini ağırlığı
  weight_gemini: 0.30          # Gemini 2.0 Flash ağırlığı

news:
  cache_ttl_minutes: 30        # Haber cache süresi
  fetch_timeout_seconds: 10    # Haber API timeout
  max_articles_per_coin: 10    # Coin başına max haber
```

### .env — API Anahtarları

```env
# LLM APIs
DEEPSEEK_API_KEY=sk-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AI...

# Haber APIs
CRYPTOPANIC_API_KEY=...   # https://cryptopanic.com/developers/api/
NEWSAPI_KEY=...           # https://newsapi.org/register
```
