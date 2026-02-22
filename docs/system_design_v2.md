# ✅ FİNAL SİSTEM TASARIMI (Consolidated v2)

Mükemmel! Tüm kararlar net. Şimdi sistemi toparlayalım. 🎯

## 📊 PORTFÖY YÖNETİMİ

### AKILLI SERMAYE YÖNETİMİ:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Başlangıç: 1000 USDC (tek wallet)

#### ALLOCATION LOGIC:
┌─────────────────────────────────────────────┐
│ ÖNCELIK 1: Grid Trading (10 coin)          │
│ • Minimum reserve: 600 USDC                │
│ • Tier allocation dinamik                  │
│                                             │
│ ÖNCELIK 2: Screener Opportunities          │
│ • Kalan bakiyeden dinamik tahsis           │
│ • Position başına: 20-100 USDC             │
│                                             │
│ KURAL:                                      │
│ IF available_usdc < screener_amount:       │
│   → Screener trade'i beklet                │
│   → Telegram: "Pozisyon beklemede,         │
│                grid satış bekleniyor"      │
│   → İlk satış gelince screener'ı çalıştır │
└─────────────────────────────────────────────┘

#### BAKIYE KONTROLÜ (Her 15 dakika):
┌─────────────────────────────────────────────┐
│ Total USDC: 1,000                          │
│ Grid Locked: 650 (BTC, ETH, SOL açık)     │
│ Screener Locked: 40 (MATIC açık)          │
│ Available: 310 USDC ✅ Yeni trade OK       │
└─────────────────────────────────────────────┘

#### MANUEL DEPOSIT KONTROLÜ:
IF wallet_balance > last_known_balance + 50:
  → Telegram: "💰 +{amount} USDC deposit algılandı!"
  → Rebalance grid allocation
  → Screener queue'yu kontrol et


## 🔍 CRYPTO SCREENER (Detaylı)

### SCREENER MODULE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

```python
def daily_screener():
    """
    Günde 1 kez (00:00 UTC) tüm Binance USDC pairlerini tara
    """
    
    all_pairs = binance.get_all_usdc_pairs()  # ~200+ coin
    candidates = []
    
    for pair in all_pairs:
        # Fetch data
        volume_24h = get_24h_volume(pair)
        rsi_4h = calculate_rsi(pair, '4h', period=14)
        rsi_1d = calculate_rsi(pair, '1d', period=14)
        price = get_current_price(pair)
        ema200_1d = calculate_ema(pair, '1d', period=200)
        
        # FILTER (Esnek Kriterler)
        if volume_24h > 5_000_000:  # 5M USDC minimum
            if (rsi_4h < 35 or rsi_1d < 30):  # En az biri oversold
                if price < ema200_1d:  # EMA altında
                    
                    # EMA'ya mesafe hesapla
                    distance_to_ema = ((ema200_1d - price) / price) * 100
                    
                    # Skor hesapla
                    score = calculate_opportunity_score(
                        rsi_4h, rsi_1d, distance_to_ema, volume_24h
                    )
                    
                    candidates.append({
                        'pair': pair,
                        'price': price,
                        'rsi_4h': rsi_4h,
                        'rsi_1d': rsi_1d,
                        'ema200': ema200_1d,
                        'distance_pct': distance_to_ema,
                        'volume': volume_24h,
                        'score': score
                    })
    
    # En iyi 5 adayı sırala
    top_5 = sorted(candidates, key=lambda x: x['score'], reverse=True)[:5]
    
    return top_5

def calculate_opportunity_score(rsi_4h, rsi_1d, distance_ema, volume):
    """
    Fırsat skorlama algoritması
    """
    score = 0
    
    # RSI skorları
    if rsi_4h < 25: score += 30
    elif rsi_4h < 30: score += 20
    elif rsi_4h < 35: score += 10
    
    if rsi_1d < 25: score += 40
    elif rsi_1d < 30: score += 30
    elif rsi_1d < 35: score += 15
    
    # EMA mesafe skoru (yakın olanlar daha iyi)
    if distance_ema < 3: score += 5   # Çok yakın (riskli)
    elif distance_ema < 8: score += 25  # Optimal sweet spot
    elif distance_ema < 15: score += 15
    elif distance_ema < 25: score += 5
    
    # Volume skoru
    if volume > 50_000_000: score += 20  # Ultra high
    elif volume > 20_000_000: score += 15
    elif volume > 10_000_000: score += 10
    elif volume > 5_000_000: score += 5
    
    return score
```

#### Skor yorumu:
- **80+**  = Mükemmel fırsat
- **60-79** = İyi fırsat
- **40-59** = Orta fırsat
- **<40**  = Zayıf, önerme


### Dinamik Position Sizing:

```python
def calculate_screener_position_size(candidate, available_usdc):
    """
    Fırsat kalitesine göre dinamik pozisyon boyutu
    """
    score = candidate['score']
    
    # Base amount
    if score >= 80:
        base = 100  # Mükemmel fırsat
    elif score >= 60:
        base = 60   # İyi fırsat
    else:
        base = 30   # Orta fırsat
    
    # Likidite ayarı
    volume = candidate['volume']
    if volume > 50_000_000:
        multiplier = 1.2  # Yüksek likidite
    elif volume < 10_000_000:
        multiplier = 0.8  # Düşük likidite
    else:
        multiplier = 1.0
    
    final_amount = base * multiplier
    
    # Limitleri kontrol et
    final_amount = min(final_amount, available_usdc)  # Bakiye sınırı
    final_amount = max(final_amount, 20)  # Minimum 20 USDC
    final_amount = min(final_amount, 100)  # Maximum 100 USDC
    
    return round(final_amount, 2)
```

**Örnek:**
```python
candidate = {
    'pair': 'MATIC/USDC',
    'score': 85,  # Mükemmel
    'volume': 60_000_000  # Yüksek likidite
}

position_size = calculate_screener_position_size(candidate, 310)
# Result: 100 * 1.2 = 120 → capped at 100 USDC
```

## 📱 TELEGRAM BOT (Detaylı)

### TELEGRAM INTERACTIONS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#### MANUEL ONAY MODU (İlk 30 gün)
```python
async def send_screener_proposal(candidate, position_size):
    """
    Screener bulgusu için onay iste
    """
    
    message = f"""
🔍 YENİ FIRSAT BULUNDU!

📌 {candidate['pair']}
💰 Önerilen: {position_size} USDC

📊 TEKNİK ANALİZ:
━━━━━━━━━━━━━━━━━━━━━━━
• 4h RSI: {candidate['rsi_4h']:.1f} {'🔴 Oversold' if candidate['rsi_4h'] < 30 else '🟡'}
• 1D RSI: {candidate['rsi_1d']:.1f} {'🔴 Oversold' if candidate['rsi_1d'] < 30 else '🟡'}
• Fiyat: ${candidate['price']:.4f}
• EMA200: ${candidate['ema200']:.4f}
• Mesafe: {candidate['distance_pct']:.1f}% altında
• Volume: ${candidate['volume']/1_000_000:.1f}M

🎯 STRATEJİ:
━━━━━━━━━━━━━━━━━━━━━━━
Entry: ${candidate['price']:.4f} (Market)
Stop: ${candidate['price'] * 0.95:.4f} (-5%)

Exit Plan (Hybrid):
├─ EMA200 Touch: ${candidate['ema200']:.4f}
│  → 40% sat (+{((candidate['ema200']/candidate['price'])-1)*100:.1f}% kar)
│
└─ Kademeli:
   • 30% @ +15% (${candidate['price']*1.15:.4f})
   • 20% @ +18% (${candidate['price']*1.18:.4f})
   • 10% @ +20% (${candidate['price']*1.20:.4f})

⚡ FIRSAT SKORU: {candidate['score']}/100

💡 Beklenen Timeline:
EMA'ya dönüş: ~3-7 gün
Risk/Reward: 1:3 ratio
    """
    
    keyboard = [
        [
            InlineKeyboardButton("✅ AL", callback_data=f"buy_{candidate['pair']}_{position_size}"),
            InlineKeyboardButton("❌ REDDET", callback_data=f"reject_{candidate['pair']}")
        ],
        [
            InlineKeyboardButton("📊 DETAY", callback_data=f"detail_{candidate['pair']}")
        ]
    ]
    
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # 24 saat bekle, cevap yoksa timeout
    await asyncio.sleep(86400)
    if not response_received:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⏰ {candidate['pair']} önerisi timeout oldu (24h)"
        )
```

#### AL butonuna basınca
```python
async def handle_buy_callback(pair, amount):
    """
    Manuel onay gelince alım yap
    """
    
    # Bakiye kontrolü
    available = get_available_usdc()
    
    if available < amount:
        await bot.send_message(
            text=f"⚠️ Yetersiz bakiye!\nGerekli: {amount} USDC\nMevcut: {available} USDC\n\nGrid pozisyonlarından satış bekleniyor..."
        )
        # Queue'ya ekle
        add_to_pending_queue(pair, amount)
        return
    
    # Alım gerçekleştir
    order = execute_screener_buy(pair, amount)
    
    # Stop-loss oluştur
    stop_price = order['price'] * 0.95
    create_stop_loss(pair, stop_price)
    
    # Hybrid exit stratejisi kur
    setup_hybrid_exit(pair, order)
    
    await bot.send_message(
        text=f"""
✅ ALIM GERÇEKLEŞTİ

{pair}
━━━━━━━━━━━━━━━━━━━━━━━
Miktar: {order['amount']:.4f} {pair.split('/')[0]}
Toplam: {amount} USDC
Giriş: ${order['price']:.4f}

🛡️ Koruma:
Stop-loss: ${stop_price:.4f} (-5%)

🎯 Exit Plan:
├─ EMA200 Touch: 40% sat
└─ Kademeli: 30%/20%/10%

Kalan bakiye: {available - amount:.2f} USDC
        """
    )
```

#### GÜNLÜK RAPOR
```python
async def send_daily_report():
    """
    00:05 UTC'de otomatik rapor
    """
    
    stats = calculate_daily_stats()
    
    message = f"""
📊 GÜNLÜK ÖZET
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🗓️ {datetime.now().strftime('%d %B %Y')}

💰 P&L:
├─ Günlük: {stats['daily_pnl']:+.2f} USDC ({stats['daily_pnl_pct']:+.2f}%)
├─ Haftalık: {stats['weekly_pnl']:+.2f} USDC ({stats['weekly_pnl_pct']:+.2f}%)
├─ Aylık: {stats['monthly_pnl']:+.2f} USDC ({stats['monthly_pnl_pct']:+.2f}%)
└─ Toplam: {stats['total_pnl']:+.2f} USDC ({stats['total_pnl_pct']:+.2f}%)

💸 MASRAFLAR:
├─ Trading Fees: {stats['fees_today']:.2f} USDC
├─ Haftalık: {stats['fees_week']:.2f} USDC
├─ Aylık: {stats['fees_month']:.2f} USDC
└─ API Costs: {stats['api_costs']:.2f} USDC/ay

📈 TRADE İSTATİSTİKLERİ:
├─ Toplam: {stats['total_trades']} trade
├─ Karlı: {stats['winning_trades']} ({stats['win_rate']:.1f}%)
├─ Zararlı: {stats['losing_trades']}
├─ Ortalama Kar: {stats['avg_win']:+.2f}%
└─ Ortalama Zarar: {stats['avg_loss']:+.2f}%

🎯 AÇIK POZİSYONLAR: {stats['open_positions']}/15
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Grid Trading:
{format_grid_positions(stats['grid_positions'])}

Screener Trades:
{format_screener_positions(stats['screener_positions'])}

💵 BAKİYE:
├─ Total: {stats['total_balance']:.2f} USDC
├─ Grid Locked: {stats['grid_locked']:.2f} USDC
├─ Screener Locked: {stats['screener_locked']:.2f} USDC
└─ Available: {stats['available']:.2f} USDC ✅

📊 EN İYİ/KÖTÜ (Bugün):
🏆 {stats['best_performer']['pair']}: {stats['best_performer']['pnl']:+.2f}%
📉 {stats['worst_performer']['pair']}: {stats['worst_performer']['pnl']:+.2f}%
    """
    
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
```

#### HER TRADE BİLDİRİMİ
```python
async def notify_trade_execution(trade):
    """
    Her alış/satış anında
    """
    
    if trade['side'] == 'buy':
        emoji = "🟢"
        action = "ALIŞ"
    else:
        emoji = "🔴"
        action = "SATIŞ"
    
    message = f"""
{emoji} {action}: {trade['pair']}

Miktar: {trade['amount']:.4f}
Fiyat: ${trade['price']:.4f}
Toplam: {trade['cost']:.2f} USDC
Fee: {trade['fee']:.4f} USDC
    """
    
    if trade['side'] == 'sell':
        pnl_pct = ((trade['price'] - trade['entry_price']) / trade['entry_price']) * 100
        pnl_usdc = (trade['price'] - trade['entry_price']) * trade['amount']
        
        message += f"""
━━━━━━━━━━━━━━━━━
Entry: ${trade['entry_price']:.4f}
P&L: {pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%)
Hold: {trade['hold_time_hours']:.1f} saat
        """
    
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
```

#### MANUEL KOMUTLAR
- `/status`: Portfolio durumu
- `/pnl`: P&L raporu (Daily/weekly/monthly breakdown)
- `/sat MATIC +20` veya `/sat MATIC market`: Satış komutları
- `/grid`: Grid pozisyonları detay
- `/screener`: Manuel screener çalıştır


## 🎯 HYBRID EXIT STRATEGY (Detaylı)

### SCREENER SATIŞ LOJİĞİ:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

```python
def setup_hybrid_exit(pair, entry_order):
    """
    2 aşamalı exit: EMA bazlı + Kademeli
    """
    
    entry_price = entry_order['price']
    amount = entry_order['amount']
    
    # EMA200 seviyesini al
    ema200 = get_ema200(pair, '1d')
    
    # --- PHASE 1: EMA200 TOUCH ---
    ema_exit = {
        'type': 'limit',
        'price': ema200 * 0.998,  # %0.2 altına koy (garantili doluş)
        'amount': amount * 0.40,  # %40'ını sat
        'reason': 'EMA200_TOUCH'
    }
    
    create_order(pair, ema_exit)
    
    # --- PHASE 2: KADEMELI SATIŞ ---
    kademeli_exits = [
        {
            'price': entry_price * 1.15,  # +15%
            'amount': amount * 0.30,
            'reason': 'LADDER_1'
        },
        {
            'price': entry_price * 1.18,  # +18%
            'amount': amount * 0.20,
            'reason': 'LADDER_2'
        },
        {
            'price': entry_price * 1.20,  # +20%
            'amount': amount * 0.10,
            'reason': 'LADDER_3'
        }
    ]
    
    for exit_order in kademeli_exits:
        create_order(pair, exit_order)
    
    # --- DİNAMİK AYARLAMA ---
    # Her 4 saatte bir EMA200'ü güncelle
    schedule_ema_update(pair, interval='4h')


def monitor_ema_exit(pair):
    """
    Her 4 saatte EMA200 güncellemesi
    """
    
    current_ema = get_ema200(pair, '1d')
    existing_orders = get_open_orders(pair)
    
    ema_order = [o for o in existing_orders if o['reason'] == 'EMA200_TOUCH'][0]
    
    # EMA değiştiyse emri güncelle
    new_price = current_ema * 0.998
    
    if abs(new_price - ema_order['price']) / ema_order['price'] > 0.02:  # %2+ fark
        cancel_order(ema_order['id'])
        create_order(pair, {
            'price': new_price,
            'amount': ema_order['amount'],
            'reason': 'EMA200_TOUCH'
        })
        
        telegram_notify(f"""
🔄 EMA Emri Güncellendi

{pair}
Eski: ${ema_order['price']:.4f}
Yeni: ${new_price:.4f}
EMA200: ${current_ema:.4f}
        """)
```

#### ÖRNEK SENARYO:
Entry: $0.85 (100 MATIC)
EMA200: $0.92

**ORDERS:**
┌──────────────────────────────────────┐
│ 1. EMA Touch: $0.917 (40 MATIC)     │
│    → +7.9% kar                       │
│                                      │
│ 2. Ladder 1: $0.978 (30 MATIC)      │
│    → +15% kar                        │
│                                      │
│ 3. Ladder 2: $1.003 (20 MATIC)      │
│    → +18% kar                        │
│                                      │
│ 4. Ladder 3: $1.020 (10 MATIC)      │
│    → +20% kar                        │
│                                      │
│ 5. Stop-loss: $0.808 (100 MATIC)    │
│    → -5% zarar (tümü)                │
└──────────────────────────────────────┘

**Timeline:**
- Day 3: EMA touch → 40 MATIC sat (+$3.16)
- Day 5: Ladder 1 → 30 MATIC sat (+$3.84)
- Day 7: Ladder 2 → 20 MATIC sat (+$3.06)
- Day 9: Ladder 3 → 10 MATIC sat (+$1.70)
- **Total:** +$11.76 (+13.8% ortalama)


## 🔧 API ERROR HANDLER

### ERROR HANDLING:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

```python
class ResilientAPIWrapper:
    """
    Her API call'ı otomatik retry + fallback ile wrap et
    """
    
    def __init__(self):
        self.cache = {}  # Local cache
        self.health_status = {
            'binance': True,
            'deepseek': True,
            'openai': True,
            'gemini': True
        }
    
    @retry(
        max_attempts=5,
        backoff=exponential,
        exceptions=[NetworkError, RateLimitError, TimeoutError]
    )
    def fetch_ohlcv(self, symbol, timeframe):
        """
        OHLCV data çek, hata varsa retry + cache
        """
        try:
            data = binance.fetch_ohlcv(symbol, timeframe, limit=500)
            
            # Başarılıysa cache'le
            cache_key = f"{symbol}_{timeframe}"
            self.cache[cache_key] = {
                'data': data,
                'timestamp': time.time()
            }
            
            return data
            
        except RateLimitError as e:
            logger.warning(f"Rate limit hit: {symbol}")
            time.sleep(60)  # 1 dakika bekle
            raise  # Retry mechanism devreye girer
        
        except NetworkError as e:
            logger.error(f"Network error: {e}")
            
            # FALLBACK: Cache'ten son bilinen data
            cached = self.cache.get(f"{symbol}_{timeframe}")
            if cached and (time.time() - cached['timestamp']) < 3600:  # 1 saat fresh
                logger.info(f"Using cached data for {symbol}")
                telegram_alert(f"⚠️ {symbol} için cached data kullanılıyor")
                return cached['data']
            else:
                raise  # Cache de yoksa/eski ise retry
    
    
    def execute_order(self, symbol, side, amount, price=None):
        """
        Emir gönder, hata varsa intelligent retry
        """
        try:
            if price:
                order = binance.create_limit_order(symbol, side, amount, price)
            else:
                order = binance.create_market_order(symbol, side, amount)
            
            return order
            
        except InsufficientFunds:
            # Bakiye yetersiz - RETRY YOK
            logger.error(f"Insufficient funds for {symbol}")
            telegram_alert(f"❌ Yetersiz bakiye: {symbol} {side}")
            return None
        
        except InvalidOrder as e:
            # Geçersiz emir (min notional, lot size etc)
            logger.error(f"Invalid order: {e}")
            
            # Otomatik düzeltme dene
            if "MIN_NOTIONAL" in str(e):
                min_notional = extract_min_notional(e)
                adjusted_amount = min_notional / price * 1.1  # %10 fazla
                
                logger.info(f"Retrying with adjusted amount: {adjusted_amount}")
                return self.execute_order(symbol, side, adjusted_amount, price)
            
            telegram_alert(f"❌ Geçersiz emir: {symbol} - {e}")
            return None
        
        except ExchangeError as e:
            # Genel exchange hatası
            logger.error(f"Exchange error: {e}")
            
            # 3 deneme
            for attempt in range(3):
                time.sleep(5 * (attempt + 1))  # 5, 10, 15 saniye
                try:
                    return binance.create_market_order(symbol, side, amount)
                except:
                    continue
            
            telegram_alert(f"🚨 3 denemeden sonra başarısız: {symbol}")
            return None
    
    
    def health_check(self):
        """
        Her 30 saniyede exchange sağlığını kontrol et
        """
        try:
            server_time = binance.fetch_time()
            self.health_status['binance'] = True
            
        except Exception as e:
            self.health_status['binance'] = False
            logger.critical("Binance API DOWN!")
            telegram_alert("🚨 CRITICAL: Binance API erişilemiyor!")
            
            # Trading'i durdur
            pause_all_trading()
    
    
    async def monitor_websocket(self):
        """
        WebSocket bağlantısını sürekli izle
        """
        while True:
            try:
                # WebSocket stream
                await binance_ws.run()
                
            except WebSocketDisconnect:
                logger.warning("WebSocket disconnected, reconnecting...")
                await asyncio.sleep(5)
                continue
            
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                telegram_alert("⚠️ WebSocket hatası, yeniden bağlanılıyor...")
                await asyncio.sleep(10)


# GLOBAL ERROR HANDLER
def global_exception_handler(exc_type, exc_value, exc_traceback):
    """
    Yakalanmayan tüm hatalar için
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    
    logger.critical(
        "Uncaught exception",
        exc_info=(exc_type, exc_value, exc_traceback)
    )
    
    telegram_alert(f"""
🚨 CRITICAL ERROR

Type: {exc_type.__name__}
Message: {str(exc_value)}

Bot durdu, manuel kontrol gerekli!
    """)
    
    # Graceful shutdown
    close_all_positions_safely()
    save_state()

sys.excepthook = global_exception_handler
```


## 📂 GÜNCEL DOSYA YAPISI

```
project/
├── freqtrade/                       # Clean install
│   └── user_data/
│       ├── config.json
│       └── strategies/
│           └── DynamicGridStrategy.py
│
├── custom_modules/
│   ├── grid_analyzer.py             # Technical analysis
│   ├── sentiment_analyzer.py        # AI LLM ensemble
│   ├── grid_fusion.py               # Grid + Sentiment merge
│   ├── bnb_manager.py               # BNB auto-buy
│   ├── screener.py                  # ⭐ YENİ: Crypto screener
│   ├── telegram_bot.py              # ⭐ YENİ: Interactive bot
│   ├── api_wrapper.py               # ⭐ YENİ: Resilient API calls
│   ├── risk_manager.py              # Circuit breaker
│   ├── capital_manager.py           # ⭐ YENİ: Balance allocation
│   └── hybrid_exit.py               # ⭐ YENİ: EMA + ladder exits
│
├── data/
│   ├── base_grid.json
│   ├── sentiment_scores.json
│   ├── final_grid.json
│   ├── screener_queue.json          # ⭐ YENİ: Pending screener trades
│   └── positions.json               # Tüm açık pozisyonlar
│
├── logs/
│   ├── trades.log
│   ├── analysis.log
│   ├── api_errors.log               # ⭐ YENİ
│   └── telegram.log                 # ⭐ YENİ
│
└── scripts/
    ├── run_analysis.sh              # 2-hour cron
    ├── run_screener.sh              # ⭐ YENİ: Daily cron
    ├── health_check.sh              # ⭐ YENİ: 30-sec cron
    └── emergency_stop.sh
```

## ⏱️ EXECUTION TIMELINE

### DAILY CYCLE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

- **00:00 UTC**
  ├─ Crypto Screener runs (all Binance pairs)
  ├─ Top 5 opportunities identified
  └─ Telegram proposals sent (manuel approval)

- **00:05 UTC**
  └─ Daily P&L report sent

- **Every 2 hours** (00:00, 02:00, 04:00, ...)
  ├─ Grid technical analysis
  ├─ Sentiment analysis
  ├─ Grid fusion
  └─ Freqtrade update

- **Every 30 seconds**
  └─ Health check (Binance API, WebSocket)

- **Every 4 hours**
  └─ EMA200 update for screener positions

- **Real-time**
  ├─ Trade execution notifications
  ├─ Balance checks
  ├─ BNB auto-buy triggers
  └─ Critical event alerts


## 💰 UPDATED COST

### MONTHLY COSTS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- LLM APIs:         $1.80
- News APIs:        $0.00
- Telegram Bot:     $0.00
- VPS (optional):   $5-10
- ─────────────────────────
- **TOTAL:**            $1.80-11.80/month

**Target Monthly Return:** 8-15% (80-150 USDC)
**ROI vs Cost:** 45x-83x ✅

---

Sistem tamam! Şimdi ne yapalım?
A) Modül-modül kod iskeletleri yazalım (başlangıç)
B) Önce Freqtrade clean install + ilk test
C) Telegram bot setup (bu en basiti, hemen çalışır)
D) Screener algoritmasını detaylandıralım
Hangisi? 🚀