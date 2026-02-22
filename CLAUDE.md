# MASTERBLUEPRINT — Akıllı Grid Trading Bot

Bu dosya Claude Code'un her session'da okuması gereken **kalıcı bağlam**dır.
Projenin amacını, mimarisini ve kararlarını burada bul. Asla bu belgeyle çelişen kod yazma.

---

## 🎯 PROJE AMACI (Neden Yapıyoruz?)

**1000 USDC sermaye ile otomatik kripto para ticareti yapan, insan müdahalesini minimumda tutan bir bot.**

Hedef:
- Aylık **%8–15 getiri** (80–150 USDC)
- **Grid trading** ile düşük riskli sürekli gelir
- **Screener** ile yüksek fırsatlı koin yakalama
- LLM sentiment analizi ile haber destekli karar alma
- Telegram üzerinden tam kontrol ve şeffaflık

**Başarı kriterleri:**
1. Bot 7/24 çalışır, çökmez
2. Her trade Telegram'a bildirilir
3. Stop-loss her zaman aktiftir
4. Günlük P&L raporu gelir
5. Sentiment analizi (%100 çalışır, fallback değil)

---

## 🏗️ MİMARİ — NE NEREDE?

```
my_ft/
├── main.py                          # Ana orkestratör — buradan başla
├── config/
│   ├── settings.yaml                # TÜM sayısal parametreler buraya
│   ├── coins.yaml                   # Grid coin listesi (5 sabit koin)
│   └── api_keys.env                 # (gitignore'd) Alternatif key dosyası
├── custom_modules/
│   ├── grid_analyzer.py             # Teknik analiz (S/R, EMA, RSI)
│   ├── sentiment_analyzer.py        # 3-LLM ensemble (DeepSeek+GPT4o+Gemini)
│   ├── grid_fusion.py               # Grid + Sentiment birleştirme
│   ├── screener.py                  # Günlük Binance tarama
│   ├── telegram_bot.py              # Telegram entegrasyonu
│   ├── capital_manager.py           # Sermaye tahsis mantığı
│   ├── risk_manager.py              # Circuit breaker
│   ├── bnb_manager.py               # BNB otomatik alım
│   ├── news_fetcher.py              # Haber çekme (CryptoPanic / NewsAPI)
│   ├── api_wrapper.py               # Resilient Binance API katmanı
│   └── hybrid_exit.py               # EMA touch + kademeli satış
├── data/                            # Runtime JSON dosyaları (gitignore'd)
├── logs/                            # Log dosyaları (gitignore'd)
└── freqtrade/user_data/
    ├── config.json                  # Freqtrade konfigürasyonu
    └── strategies/DynamicGridStrategy.py
```

---

## 💰 SERMAYE YÖNETİMİ (KURAL — DEĞİŞTİRME)

| Kalem | Değer |
|-------|-------|
| Toplam sermaye | 1000 USDC |
| Grid rezervi (min) | 600 USDC |
| Screener pozisyon başına | 20–100 USDC |
| Stop-loss | -5% (sabit) |
| Max açık pozisyon | 15 |

**Öncelik sırası:** Grid Trading > Screener Opportunities

Screener'da bakiye yetersizse → Telegram bildir → Grid satışı bekle → Sonra aç.

---

## 🔍 SCREENER MANTIĞI

- **Ne zaman:** Her gün 00:00 UTC
- **Tüm Binance USDC pairi** taranır (~200+ koin)
- **Filtre:** volume > 5M + RSI oversold + EMA200 altında
- **Skor:** 0-100 (80+ mükemmel, 60-79 iyi, 40-59 orta)
- **İlk 30 gün:** Manuel Telegram onayı gerekli
- **30 gün sonra:** Skor ≥80 → otomatik al

---

## 🤖 SENTIMENT ANALİZİ (AKTİF OLMALI)

**3 LLM ensemble — paralel çalışır:**

| LLM | Model | Ağırlık | Key |
|-----|-------|---------|-----|
| DeepSeek | deepseek-chat | %35 | DEEPSEEK_API_KEY |
| OpenAI | gpt-4o-mini | %35 | OPENAI_API_KEY |
| Gemini | gemini-2.0-flash | %30 | GEMINI_API_KEY |

- En az 2 LLM başarılı olmalı (`min_llms_required: 2`)
- Confidence < 0.6 → sentiment ignore
- Sonuç `data/sentiment_scores.json`'a kaydedilir
- **BU MODÜL FALLBACK OLMAMALI** — key'ler .env'de olmalı

**Haber kaynakları:** CryptoPanic (birincil) → NewsAPI (yedek)

---

## 📱 TELEGRAM (Kontrol Merkezi)

Tüm kararlar Telegram'dan onaylanır veya gözlemlenir.

| Komut | Açıklama |
|-------|---------|
| `/status` | Portfolio durumu |
| `/pnl` | Günlük/haftalık/aylık P&L |
| `/sat MATIC market` | Manuel satış |
| `/grid` | Grid pozisyonları |
| `/screener` | Manuel screener çalıştır |

**Kritik bildirimler:**
- Her trade (alış/satış)
- Screener fırsat önerisi (✅/❌ butonu)
- Stop-loss tetiklenince
- Circuit breaker devreye girince
- Günlük 00:05 UTC P&L raporu

---

## 🛡️ RİSK YÖNETİMİ (KURAL — DEĞİŞTİRME)

- Günlük kayıp > -%5 → trading durdur (4 saat cooldown)
- Art arda 5 kayıp → circuit breaker
- Max 15 açık pozisyon
- BNB < 1 USDC değerinde → otomatik 5 USDC BNB al

---

## 🔄 GÜNLÜK DÖNGÜ

```
00:00 UTC  → Screener çalışır → Telegram öneri
00:05 UTC  → Günlük P&L raporu
Her 2 saat → Grid analizi + Sentiment + Fusion → Freqtrade güncelle
Her 4 saat → EMA200 güncelle (screener pozisyonları)
Her 30 sn  → Health check (Binance API + WebSocket)
Her 15 dk  → Bakiye kontrolü + BNB check
Realtime   → Trade bildirimleri, alert'ler
```

---

## 🚪 HYBRID EXIT STRATEJİSİ (Screener pozisyonları)

Her screener alımında otomatik kurulur:

| Faz | Tetikleyici | Satış miktarı |
|-----|------------|--------------|
| EMA Touch | EMA200 *0.998 | %40 |
| Ladder 1 | Entry +%15 | %30 |
| Ladder 2 | Entry +%18 | %20 |
| Ladder 3 | Entry +%20 | %10 |
| Stop-loss | Entry -%5 | %100 (tümü) |

---

## ⚙️ GELİŞTİRME KURALLARI

1. **Tüm sayısal değerler** `config/settings.yaml`'da olacak — kod içinde hardcode yok
2. **API key** asla kaynak koda yazılmaz — sadece `.env`
3. **Yeni modül** eklerken `custom_modules/` içine yaz
4. **Her kritik operasyon** Telegram'a bildirilmeli
5. **Sentiment analizi** fallback (boş dict) ile geçiştirilmemeli — key'ler doğru ayarlanmalı
6. `dry_run: true` ile başla, canlıda `false` yap
7. Tüm trade'ler `data/positions.json`'da takip edilir

---

## 📦 GEREKLİ BAĞIMLILIKLAR

```
freqtrade, ccxt, python-telegram-bot
openai, aiohttp, google-genai
python-dotenv, pyyaml, pandas, numpy
```

---

## 🔑 GEREKLİ ENV DEĞİŞKENLERİ

```bash
# Zorunlu
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Sentiment analizi için (hepsi gerekli)
OPENAI_API_KEY=...
DEEPSEEK_API_KEY=...
GEMINI_API_KEY=...

# Haber kaynakları (opsiyonel)
CRYPTOPANIC_API_KEY=...
NEWSAPI_KEY=...
```

---

## ❌ BİLİNEN SORUNLAR / YAPILACAKLAR

- [ ] Sentiment analizi key olmadan fallback'e düşüyor → .env ayarlanınca çözülür
- [ ] `data/` klasörü boş — ilk çalıştırmada oluşur
- [ ] `dry_run: true` — canlıya geçmeden önce test edilmeli

---

*Bu dosya projenin tek gerçek kaynağıdır. Çelişen bilgi varsa bu dosya kazanır.*
