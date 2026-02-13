# -*- coding: utf-8 -*-
import time
import logging
import os
from decimal import Decimal, ROUND_DOWN
from typing import List, Tuple

# pybit v5 (5.13.x): правильный импорт
from pybit.unified_trading import HTTP

# ================== НАСТРОЙКИ ==================
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

symbol = "SOLUSDT"
BASE_COIN = "SOL"
TESTNET = False   # True = testnet, False = real

BALANCE_CAP_USDT = 6      # лимит (не тратим больше этого)
ORDER_COUNT = 1              # 2 BUY ордера
USDT_PER_ORDER = 5.2         # каждый BUY примерно на 5.2 USDT

GRID_STEP_PERCENT = 0.5      # шаг сетки (в %)
STOP_LOSS_PERCENT = 35       # глобальный стоп (от стартовой цены)
GRID_REBUILD_THRESHOLD = 2.0 # пересборка сетки, если цена ушла на X%

CHECK_DELAY = 2              # секунд
HEARTBEAT_EVERY = 30         # секунд (чтобы было видно, что бот жив и ждёт без спама)
LOG_FILE = "bybit_grid_bot.log"

# ====== ТРЕНД-ФИЛЬТР ======
USE_TREND_FILTER = True
KLINE_INTERVAL = "5"         # 5 минут
EMA_FAST = 20
EMA_SLOW = 50
DOWNTREND_BARS_CONFIRM = 2   # чтобы не дёргалось: downtrend должен быть подтвержден N раз подряд

# ================== ЛОГИ ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

def log(msg: str):
    logging.info(msg)

# ================== КЛИЕНТ ==================
client = HTTP(
    api_key=API_KEY,
    api_secret=API_SECRET,
    testnet=TESTNET
)

# ================== УТИЛИТЫ ОКРУГЛЕНИЯ ==================
def _dec(x) -> Decimal:
    return Decimal(str(x))

def floor_to_step(value: float, step: float) -> float:
    v = _dec(value)
    s = _dec(step)
    if s <= 0:
        return float(v)
    q = (v / s).to_integral_value(rounding=ROUND_DOWN) * s
    return float(q)

def fmt_by_step(value: float, step: float) -> str:
    v = _dec(floor_to_step(value, step))
    s = _dec(step)
    places = max(0, -s.as_tuple().exponent)
    return f"{v:.{places}f}"

# ================== MARKET / FILTERS ==================
def get_filters() -> Tuple[float, float, float, float]:
    data = client.get_instruments_info(category="spot", symbol=symbol)
    lst = data.get("result", {}).get("list", [])
    if not lst:
        raise RuntimeError("Не смог получить instruments-info (пусто).")
    info = lst[0]

    price_filter = info.get("priceFilter", {}) or {}
    lot_filter = info.get("lotSizeFilter", {}) or {}

    tick_size = float(price_filter.get("tickSize") or 0.00001)

    qty_step = None
    if lot_filter.get("qtyStep") is not None:
        qty_step = float(lot_filter.get("qtyStep"))
    elif lot_filter.get("basePrecision") is not None:
        qty_step = float(lot_filter.get("basePrecision"))
    elif lot_filter.get("minOrderQty") is not None:
        qty_step = float(lot_filter.get("minOrderQty"))
    else:
        qty_step = 0.01

    min_qty = float(lot_filter.get("minOrderQty") or 0)
    min_amt = float(lot_filter.get("minOrderAmt") or 0)

    return tick_size, qty_step, min_qty, min_amt

def get_price() -> float:
    data = client.get_tickers(category="spot", symbol=symbol)
    lst = data.get("result", {}).get("list", [])
    if not lst:
        return 0.0
    return float(lst[0].get("lastPrice") or 0.0)

# ================== KLINES / EMA TREND ==================
def get_closes(limit: int = 120) -> List[float]:
    res = client.get_kline(
        category="spot",
        symbol=symbol,
        interval=KLINE_INTERVAL,
        limit=limit
    )
    lst = res.get("result", {}).get("list", []) or []
    # Bybit обычно отдаёт: [startTime, open, high, low, close, volume, turnover]
    closes = []
    for row in lst:
        try:
            closes.append(float(row[4]))
        except:
            pass
    closes.reverse()  # старые -> новые
    return closes

def ema(values: List[float], period: int) -> float:
    if not values or period <= 1 or len(values) < period:
        return 0.0
    k = 2.0 / (period + 1.0)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
    return e

def is_downtrend() -> bool:
    if not USE_TREND_FILTER:
        return False
    closes = get_closes(limit=max(EMA_SLOW * 3, 120))
    if len(closes) < EMA_SLOW + 5:
        return False
    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)
    return fast > 0 and slow > 0 and fast < slow

# ================== BALANCE (UNIFIED) ==================
def get_coin_balance(coin: str) -> float:
    data = client.get_wallet_balance(accountType="UNIFIED", coin=coin)
    lst = data.get("result", {}).get("list", [])
    if not lst:
        return 0.0
    coins = lst[0].get("coin", [])
    for c in coins:
        if c.get("coin") == coin:
            for key in ("availableToWithdraw", "availableBalance", "walletBalance", "free"):
                if c.get(key) is not None:
                    try:
                        return float(c.get(key))
                    except:
                        pass
            try:
                return float(c.get("walletBalance") or 0.0)
            except:
                return 0.0
    return 0.0

def get_usdt_balance() -> float:
    return get_coin_balance("USDT")

def get_base_balance() -> float:
    return get_coin_balance(BASE_COIN)

# ================== ORDERS ==================
def cancel_all_open_orders():
    log("🧹 Clearing old orders...")
    try:
        res = client.get_open_orders(category="spot", symbol=symbol)
        orders = res.get("result", {}).get("list", []) or []
        for o in orders:
            oid = o.get("orderId")
            if oid:
                client.cancel_order(category="spot", symbol=symbol, orderId=oid)
        log("🧹 Old orders cleared")
    except Exception as e:
        log(f"⚠️ ERROR while clearing orders: {e}")

def place_limit_buy(price: float, usdt_amount: float, tick_size: float, qty_step: float, min_qty: float, min_amt: 

float):
    if price <= 0:
        return None

    price_rounded = floor_to_step(price, tick_size)
    if price_rounded <= 0:
        return None

    qty = usdt_amount / price_rounded
    qty_rounded = floor_to_step(qty, qty_step)

    if min_qty > 0 and qty_rounded < min_qty:
        qty_rounded = floor_to_step(min_qty, qty_step)

    notional = qty_rounded * price_rounded
    if min_amt > 0 and notional < min_amt:
        qty_need = (min_amt / price_rounded)
        qty_rounded = floor_to_step(qty_need, qty_step)

    if qty_rounded <= 0:
        return None

    if get_usdt_balance() < usdt_amount:
        log("⚠️ Not enough USDT")
        return None

    try:
        client.place_order(
            category="spot",
            symbol=symbol,
            side="Buy",
            orderType="Limit",
            timeInForce="GTC",
            qty=fmt_by_step(qty_rounded, qty_step),
            price=fmt_by_step(price_rounded, tick_size)
        )
        log(f"🟢 BUY placed @ {fmt_by_step(price_rounded, tick_size)} | ~{usdt_amount} USDT")
        return True
    except Exception as e:
        log(f"⚠️ ERROR BUY: {e}")
        return None

def place_limit_sell_from_fill(buy_price: float, filled_qty: float, tick_size: float, qty_step: float):
    if buy_price <= 0 or filled_qty <= 0:
        return False

    # ✅ пауза 1–2 сек чтобы баланс успел обновиться
    time.sleep(1.5)

    available = get_base_balance()
    qty = min(filled_qty, available)
    qty = floor_to_step(qty, qty_step)
    if qty <= 0:
        return False

    sell_price = buy_price * (1 + GRID_STEP_PERCENT / 100.0)
    sell_price = floor_to_step(sell_price, tick_size)
    if sell_price <= 0:
        return False

    try:
        client.place_order(
            category="spot",
            symbol=symbol,
            side="Sell",
            orderType="Limit",
            timeInForce="GTC",
            qty=fmt_by_step(qty, qty_step),
            price=fmt_by_step(sell_price, tick_size)
        )
        log(f"🔴 TP-SELL placed @ {fmt_by_step(sell_price, tick_size)} | qty={fmt_by_step(qty, qty_step)}")
        log(f"🔄 BUY filled @ {fmt_by_step(buy_price, tick_size)} → SELL @ {fmt_by_step(sell_price, tick_size)}")
        return True
    except Exception as e:
        log(f"⚠️ ERROR SELL: {e}")
        return False

# ================== GRID ==================
grid_prices = []

def build_grid(base_price: float, tick_size: float, qty_step: float, min_qty: float, min_amt: float, allow_buy: 

bool):
    global grid_prices
    grid_prices = []

    if not allow_buy:
        log("🟡 BUY disabled (ONLY-SELL mode) → grid not built")
        return

    for i in range(ORDER_COUNT):
        p = base_price * (1 - (GRID_STEP_PERCENT / 100.0) * i)
        p = floor_to_step(p, tick_size)
        if p <= 0:
            continue

        if get_usdt_balance() < USDT_PER_ORDER:
            log("⚠️ Not enough USDT")
            break

        ok = place_limit_buy(p, USDT_PER_ORDER, tick_size, qty_step, min_qty, min_amt)
        if ok:
            grid_prices.append(p)

# ===== AUTH CHECK =====
def auth_check_or_exit():
    try:
        client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        client.get_open_orders(category="spot", symbol=symbol)
    except Exception as e:
        log(f"🛑 AUTH ERROR (401): проверь API ключи и права. Детали: {e}")
        raise SystemExit(1)


# ================== ОСНОВНОЙ ЗАПУСК ==================
def main():
    auth_check_or_exit()

    tick_size, qty_step, min_qty, min_amt = get_filters()

    cancel_all_open_orders()

    start_price = get_price()
    if start_price <= 0:
        log("⚠️ Price is 0 — stop")
        return

    stop_price = start_price * (1 - STOP_LOSS_PERCENT / 100.0)
    stop_price = floor_to_step(stop_price, tick_size)

    log("🚀 GRID BOT STARTED (BYBIT TESTNET)" if TESTNET else "🚀 GRID BOT STARTED (BYBIT REAL)")
    log(f"📉 Stop price: {fmt_by_step(stop_price, tick_size)}")
    log(f"🧾 Balance cap: {BALANCE_CAP_USDT} USDT | Orders: {ORDER_COUNT} | Target per order: {USDT_PER_ORDER} USDT")
    log(f"🔧 Filters: tickSize={tick_size} qtyStep={qty_step}")

    # ✅ железный режим: если downtrend → ONLY-SELL (запрет BUY + запрет rebuild/grid)
    downtrend_hits = 0
    only_sell = False
    if USE_TREND_FILTER:
        if is_downtrend():
            downtrend_hits += 1
        if downtrend_hits >= DOWNTREND_BARS_CONFIRM:
            only_sell = True

    allow_buy = not only_sell

    # первая сетка
    build_grid(start_price, tick_size, qty_step, min_qty, min_amt, allow_buy=allow_buy)

    processed_fills = set()
    last_heartbeat = 0
    bot_start_ts_ms = int(time.time() * 1000)

    while True:
        try:
            current_price = get_price()
            if current_price <= 0:
                time.sleep(CHECK_DELAY)
                continue

            # стоп
            if current_price <= stop_price:
                log("🛑 STOP LOSS HIT → cancel all and exit")
                cancel_all_open_orders()
                break

            # тренд-режим обновляем “с подтверждением”
            if USE_TREND_FILTER:
                if is_downtrend():
                    downtrend_hits = min(downtrend_hits + 1, DOWNTREND_BARS_CONFIRM)
                else:
                    downtrend_hits = max(downtrend_hits - 1, 0)
                only_sell = (downtrend_hits >= DOWNTREND_BARS_CONFIRM)

            allow_buy = not only_sell

            # rebuild grid (только если BUY разрешены)
            if allow_buy and grid_prices:
                mid = grid_prices[0]
                change = abs(current_price - mid) / mid * 100.0
                if change >= GRID_REBUILD_THRESHOLD:
                    log(f"🔄 Price moved {change:.2f}% → Rebuilding grid")
                    cancel_all_open_orders()
                    build_grid(current_price, tick_size, qty_step, min_qty, min_amt, allow_buy=allow_buy)

            # обрабатываем только новые fills, которые реально произошли после запуска
            hist = client.get_order_history(category="spot", symbol=symbol, limit=50)
            orders = hist.get("result", {}).get("list", []) or []

            for o in orders:
                oid = o.get("orderId")
                status = o.get("orderStatus") or o.get("status")
                side = o.get("side")
                price_str = o.get("price")
                qty_str = o.get("qty") or o.get("origQty")
                created_ms = None

                for k in ("createdTime", "createdTimeMs", "createdAt", "createTime"):
                    if o.get(k) is not None:
                        try:
                            created_ms = int(o.get(k))
                            break
                        except:
                            pass

                if not oid or oid in processed_fills:
                    continue
                if status not in ("Filled", "FILLED"):
                    continue
                if created_ms is not None and created_ms < bot_start_ts_ms:
                    # старые сделки до запуска игнорируем
                    processed_fills.add(oid)
                    continue

                processed_fills.add(oid)

                try:
                    fill_price = float(price_str)
                except:
                    fill_price = 0.0
                try:
                    fill_qty = float(qty_str)
                except:
                    fill_qty = 0.0

                if side == "Buy":
                    ok = place_limit_sell_from_fill(fill_price, fill_qty, tick_size, qty_step)
                    if not ok:
                        log("⚠️ Could not place SELL after BUY fill")
                elif side == "Sell":
                    log(f"💰 SELL filled @ {fmt_by_step(fill_price, tick_size)} | qty={fmt_by_step(fill_qty, 

qty_step)}")

            # heartbeat
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_EVERY:
                try:
                    res = client.get_open_orders(category="spot", symbol=symbol)
                    opens = res.get("result", {}).get("list", []) or []
                    mode = "ONLY-SELL" if only_sell else "BUY+SELL"
                    log(f"💡 Alive | mode={mode} | open_orders={len(opens)} | price={fmt_by_step(current_price, 

tick_size)}")
                except Exception:
                    mode = "ONLY-SELL" if only_sell else "BUY+SELL"
                    log(f"💡 Alive | mode={mode} | price={fmt_by_step(current_price, tick_size)}")
                last_heartbeat = now

            time.sleep(CHECK_DELAY)

        except KeyboardInterrupt:
            log("🧠 Bot stopped manually.")
            break
        except Exception as e:
            log(f"⚠️ ERROR: {e}")
            time.sleep(5)

    log("🧠 BOT FINISHED")

if __name__ == "__main__":
    main()
