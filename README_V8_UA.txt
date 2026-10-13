BTC Signal V8 — тестова версія без реальних ордерів

Файли:
- bot_v8.py
- requirements_v8.txt
- test_bot_v8.py

Render:
Build Command: pip install -r requirements_v8.txt
Start Command: python -u bot_v8.py

Обов'язкові Environment Variables:
BOT_TOKEN
CHAT_ID

Рекомендовані:
CHECK_SECONDS=60
PAPER_TRADING=1
REFERENCE_EQUITY=1000
RISK_PCT=0.005
MIN_SCORE=68
MIN_RR=2.0
MAX_OPEN_PAPER=1
MAX_HOLD_HOURS=24
DAILY_LOSS_R=3.0

Що є у V8:
- усе ядро V7: BTC/USDT, 5m/15m/1H/4H, trend/range, breakout/pullback/rejection
- spot + futures order book
- spot + futures tape delta
- funding + OI
- fail-closed перевірка даних
- комісії/прослизання в risk sizing
- Telegram startup/signal/heartbeat/error
- SQLite журнал
- paper trading без реальних ордерів
- автоматичне закриття paper-позиції по observed price: STOP/TARGET/TIME
- P&L, R-multiple, MFE/MAE
- win rate, profit factor, AvgR після закриття
- денний paper loss guard

ВАЖЛИВО:
V8 не має API-ключів біржі й не виставляє реальних ордерів.
Paper exit перевіряється за ціною, яку бот реально побачив на циклі; код не вигадує intrabar fill між перевірками.
Новини/геополітика залишаються ручним EVENT_RISK, якщо окремий live-news модуль не підключений.

Перевірка перед Render:
python -m unittest test_bot_v8.py -v
