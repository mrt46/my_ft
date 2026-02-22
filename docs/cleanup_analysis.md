# 🧹 Freqtrade Cleanup Analizi

## ⚠️ SAĞLANAN INSTRUCTIONS'DAKİ KRİTİK HATALAR

Verilen cleanup instructions'da **3 dosya/klasör yanlışlıkla silinmek üzere işaretlenmiş**.
Bunları silmek sistemi kırar veya önemli referansları yok eder.

---

## 🔴 SİLME — Kritik Tutulacaklar

### 1. `pyproject.toml` ← EN KRİTİK
**Instructions "sil" diyor ama bu dosya ASLA silinmemeli.**

- Freqtrade'in kendi paket tanımıdır
- `freqtrade` CLI komutları bu dosyaya bağımlıdır
- Silersen `freqtrade trade`, `freqtrade list-strategies` gibi komutlar çalışmaz
- `pip install -e .` ile kurulumda şarttır

---

### 2. `scripts/rest_client.py` + `scripts/ws_client.py` ← ÖNEMLİ REFERANS
**Instructions `scripts/` klasörünü tamamen silmek istiyor ama bunlar kritik.**

```
freqtrade/scripts/
├── rest_client.py   ← Freqtrade REST API ile iletişim
└── ws_client.py     ← WebSocket client referans implementasyonu
```

**Neden tutulmalı:**
- Bizim `custom_modules/api_wrapper.py`'ı yazarken **direkt referans** olarak kullanacağız
- Freqtrade'in kendi iç API'sine nasıl bağlanıldığını gösteriyor
- `ws_client.py` → `api_wrapper.monitor_websocket()` implementasyonunda model alınacak
- Kopyala-yapıştır değil ama mimariyi anlamak için şart

---

### 3. `ft_client/` ← API WRAPPER İÇİN GEREKLİ
**Instructions bu klasörden bahsetmiyor, ama silinirse api_wrapper zarar görür.**

```
freqtrade/ft_client/
└── freqtrade_client/
    ├── ft_client.py        ← Freqtrade async REST client
    └── ft_rest_client.py   ← Freqtrade sync REST client
```

**Neden tutulmalı:**
- `capital_manager.py` → Freqtrade'den açık pozisyon bilgisi alacak
- `api_wrapper.py` → Freqtrade'in kendi API'sine programatik erişim
- Bu library olmadan Freqtrade'i dışarıdan kontrol edemeyiz

---

### 4. `freqtrade.service` + `freqtrade.service.watchdog`
**System design'da systemd deployment planlanmış, bu dosyalar lazım.**

- `docs/system_design_v2.md` → "Systemd Service" bölümüne bak
- Linux/VPS deploy'da bu template üzerinden oluşturulacak
- Şimdi silersen ileride manual yazmak gerekir

---

### 5. `setup.ps1` (Windows) / `setup.sh`
**Freqtrade bağımlılıklarını kuran script, özellikle TA-Lib için kritik.**

- TA-Lib Windows'ta elle kurulamaz, setup script gerekir
- Yeni ortamda kurulum yaparken tekrar lazım olacak

---

## ✅ GÜVENLİ SİLİNEBİLECEKLER

| Klasör/Dosya | Sebep | Boyut (tahmini) |
|---|---|---|
| `docs/` (133 dosya) | Freqtrade kendi dökümantasyonu, bizimkiyle karışıyor | ~30 MB |
| `tests/` (188 dosya) | Freqtrade unit testleri, bizimle ilgisiz | ~15 MB |
| `.github/` | CI/CD workflows, fork etmiyoruz | ~1 MB |
| `build_helpers/` | ARM .whl dosyaları (Raspberry Pi için), biz x86 | ~50 MB |
| `docker/` | Docker compose files, kullanmıyoruz | ~1 MB |
| `docker-compose.yml`, `Dockerfile` | Aynı sebep | <1 MB |
| `mkdocs.yml` | MkDocs config, docs/ ile birlikte gider | <1 MB |
| `.readthedocs.yml` | ReadTheDocs CI config | <1 MB |
| `CONTRIBUTING.md` | Open source contribution guide | <1 MB |
| `freqtrade/templates/` | Örnek strategy template'leri (bizimki var) | ~1 MB |
| `user_data/notebooks/` | Jupyter notebooks | <1 MB |
| `user_data/freqaimodels/` | FreqAI model storage (kullanmıyoruz) | <1 MB |
| `user_data/hyperopts/` | Hyperopt results (boş) | <1 MB |

---

## 🤔 SORULMASI GEREKEN 3 SORU

Şu 3 cevaba göre ek silme kararı:

### Soru 1: Backtesting kullanılacak mı?
```
❌ Hayır → freqtrade/optimize/ SİL (~8 MB, 39 dosya)
✅ Evet  → TULT (DynamicGridStrategy'yi canlıya almadan önce backtest şart!)
```
**Öneri: TULT** — Stratejiyi canlıya almadan önce tarihsel data üzerinde test etmek kritik.

### Soru 2: Plotting/grafik çizme gerekli mi?
```
❌ Hayır → freqtrade/plot/ SİL (~2 dosya)
✅ Evet  → TUT
```
**Öneri: SİL** — Telegram bot zaten trade bildirimlerini yapıyor. Ayrı plot gerekmez.

### Soru 3: FreqAI (ML tabanlı sinyal) kullanılacak mı?
```
❌ Hayır → freqtrade/freqai/ SİL (~40 dosya)
✅ Evet  → TUT
```
**Öneri: SİL** — Biz kendi AI modülümüzü kullanıyoruz (`custom_modules/sentiment_analyzer.py`
→ DeepSeek + GPT-4o + Gemini ensemble). FreqAI bize gereksiz.

---

## 📦 TAHMİNİ BOYUT KARŞILAŞTIRMASI

```
ÖNCE cleanup:
freqtrade/  ~450 MB

SONRA cleanup (önerilen):
freqtrade/  ~60-80 MB

  Silinecek (~370 MB):
  ├── docs/            ~30 MB
  ├── tests/           ~15 MB
  ├── build_helpers/   ~50 MB  ← ARM .whl dosyaları büyük
  ├── freqtrade/freqai/ ~5 MB
  ├── freqtrade/plot/   ~1 MB
  └── diğerleri        ~5 MB
```

---

## 📂 HEDEFLEDİĞİMİZ FINAL YAPI

```
freqtrade/                        ← Freqtrade repo root
├── freqtrade/                    ← Core Python package (DOKUNMA)
│   ├── __init__.py
│   ├── configuration/
│   ├── data/
│   ├── enums/
│   ├── exchange/                 ← Binance bağlantısı
│   ├── optimize/                 ← Backtesting (TUT)
│   ├── persistence/              ← Trade database
│   ├── plugins/
│   ├── resolvers/
│   ├── rpc/                      ← REST API / Telegram
│   ├── strategy/                 ← Strategy interface
│   ├── templates/                ← SİL
│   ├── freqai/                   ← SİL
│   ├── plot/                     ← SİL
│   └── wallets.py
│
├── ft_client/                    ← TUT (api_wrapper referansı)
│   └── freqtrade_client/
│       ├── ft_client.py
│       └── ft_rest_client.py
│
├── scripts/                      ← TUT (api_wrapper referansı)
│   ├── rest_client.py
│   └── ws_client.py
│
├── user_data/                    ← KENDİ CONFIG'İMİZ
│   ├── strategies/
│   │   └── DynamicGridStrategy.py  (oluşturulacak)
│   ├── data/                     ← Market data cache
│   └── logs/
│
├── config_examples/              ← TUT (config.json referansı)
│   └── config_binance.example.json
│
├── freqtrade.service             ← TUT (deployment)
├── freqtrade.service.watchdog    ← TUT (deployment)
├── pyproject.toml                ← ASLA SİLME
├── requirements.txt              ← TUT
├── requirements-hyperopt.txt     ← SİL
├── requirements-freqai.txt       ← SİL
├── requirements-freqai-rl.txt    ← SİL
├── requirements-plot.txt         ← SİL
├── requirements-dev.txt          ← SİL
├── setup.ps1                     ← TUT (Windows kurulum)
├── setup.sh                      ← TUT (Linux kurulum)
├── LICENSE                       ← TUT (legal)
└── README.md                     ← TUT
```
