# Grid Bot v3 — Yapılacaklar Listesi

Son güncelleme: 2026-02-22

---

## ✅ Tamamlananlar

- [x] `api_wrapper.py` — Resilient API wrapper (retry, cache, health)
- [x] `capital_manager.py` — Balance allocation
- [x] `bnb_manager.py` — BNB auto-buy
- [x] `grid_analyzer.py` — Technical analysis (S/R, Fibonacci, tier system)
- [x] `sentiment_analyzer.py` — 3-LLM ensemble (DeepSeek, GPT-4o, Gemini)
- [x] `grid_fusion.py` — Grid + Sentiment merge → final_grid.json
- [x] `screener.py` — Daily opportunity scanner
- [x] `hybrid_exit.py` — EMA + ladder exits
- [x] `telegram_bot.py` — Custom interactive bot (pasif, referans olarak tutuluyor)
- [x] `risk_manager.py` — Circuit breaker
- [x] `DynamicGridStrategy.py` — Freqtrade strategy
- [x] `main.py` — Orchestrator
- [x] Freqtrade Telegram entegrasyonu (`allow_custom_messages: true`)
- [x] Grid seviyelerini Telegram'a bildirme (`_send_grid_telegram`)
- [x] Mevcut değişiklikler commit edildi (`dd4b592`)

---

## 🔄 Devam Eden / Yapılacaklar

### 1. %3 Günlük Kar Hedefi — Sabitleme
- [ ] `DynamicGridStrategy.py`: `custom_exit` minimum kar eşiğini `0.005` → `0.01` yükselt
- [ ] `config.json`: `minimal_roi` override ekle: `{"0": 0.03, "1440": 0.02, "2880": 0.01}`
- [ ] `settings.yaml`: `daily_profit_target_pct: 3.0` zaten var — doğrula

### 2. Freqtrade Telegram Entegrasyonu (custom_modules/telegram_bot.py özellikleri)
- [ ] `DynamicGridStrategy.py`: `bot_loop_start()` hook ekle
  - Startup'ta grid özeti bildirimi
  - Günlük P&L özeti (00:05 UTC)
- [ ] `custom_exit` içine detaylı TP bildirimi ekle (kar %, hedef fiyat, hold süresi)
- [ ] `adjust_trade_position` içine DCA bildirimi ekle (kaçıncı DCA, seviye, eklenen miktar)
- [ ] Screener önerisi formatını TP bildirimine entegre et

### 3. Test Suite Genişletme
- [ ] `tests/test_grid_analyzer.py`: Adaptif grid, tier sistemi, S/R merge testleri
- [ ] `tests/test_sentiment.py`: 3-LLM ensemble, fallback, confidence < 0.6 testleri
- [ ] `tests/test_grid_fusion.py`: Sentiment shift, neutral fallback testleri
- [ ] `tests/test_telegram_bot.py`: `send_alert_sync`, `send_screener_proposal` mock testleri
- [ ] `tests/test_dynamic_grid_strategy.py`: Yeni dosya — entry/exit/DCA sinyal testleri

### 4. Adaptif Grid Doğrulama
- [ ] Test: Fiyat düşünce grid alt sınırı aşağı kayıyor mu?
- [ ] Test: Sentiment negatifse grid seviyeleri aşağı shift oluyor mu?
- [ ] Test: `final_grid.json` her 2 saatte güncelleniyor mu?
- [ ] Test: Grid cache TTL 300s doğru çalışıyor mu?

### 5. Sentiment Analiz Doğrulama
- [ ] Test: 3 LLM'den en az 2'si başarılı olduğunda ensemble çalışıyor mu?
- [ ] Test: Confidence < 0.6 olduğunda sentiment ignore ediliyor mu?
- [ ] Test: LLM timeout (30s) sonrası fallback neutral sentiment dönüyor mu?
- [ ] Test: Haber cache TTL 30 dakika doğru çalışıyor mu?

### 6. Raporlama ve Geri Besleme
- [ ] `main.py`: Günlük P&L raporu gerçek trade verisiyle doldur
- [ ] `status.json`: Grid analiz sonuçlarını (tier, seviye sayısı) ekle
- [ ] `status.json`: Sentiment skorlarını ekle
- [ ] Telegram `/report` komutu: status.json'u AI analizi için formatla

---

## 🚀 Gelecek Geliştirmeler (v4)

- [ ] Binance API key entegrasyonu (şu an dry_run)
- [ ] Gerçek BNB auto-buy aktivasyonu
- [ ] Screener: İlk 30 gün manuel onay → sonra auto-buy (score > 80)
- [ ] Redis cache entegrasyonu (opsiyonel)
- [ ] Freqtrade WebSocket entegrasyonu
- [ ] Multi-timeframe grid analizi (1h, 4h, 1d)
- [ ] Backtesting ile strateji optimizasyonu

---

## 📋 Bilinen Sorunlar

| Sorun | Dosya | Öncelik |
|-------|-------|---------|
| `custom_stake_amount` `current_profit` argümanı eksik hatası | `DynamicGridStrategy.py` | Düşük (bot çalışıyor) |
| `logs/api_errors.log` 103K satır — rotate edilmeli | `main.py` | Orta |
| `data/exit_plans.json` boş | `hybrid_exit.py` | Orta |
| Custom telegram bot pasif (token conflict riski) | `telegram_bot.py` | Düşük |

---

## 🔑 Kritik Kurallar (Değiştirme!)

1. `freqtrade/freqtrade/rpc/telegram.py` — **ASLA dokunma** (upstream, güncellemeyle ezilir)
2. Tüm Telegram bildirimleri `dp.send_msg()` veya `send_alert_sync()` üzerinden
3. API key'ler sadece `.env` dosyasında
4. `dry_run: true` — canlıya geçmeden önce en az 7 gün test
5. Her trade öncesi bakiye kontrolü
