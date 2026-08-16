"""
NSE RSI(60) + Bollinger Band(40, 1.4) Telegram alert bot.
Runs once per invocation - designed to be triggered every 10 min by GitHub Actions.
"""

import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

# ---------------- CONFIG ----------------
# Secrets are read from environment variables (set as GitHub Actions secrets - never hardcode them)
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOLS = [
    "^NSEI", "ASHOKLEY.NS", "VOLTAS.NS", "INDHOTEL.NS", "ITC.NS",
    "INFY.NS", "TATAMOTORS.NS", "SBIN.NS", "HDFCBANK.NS", "RELIANCE.NS",
    "WIPRO.NS", "AXISBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS", "BAJFINANCE.NS",
    "BAJAJFINSV.NS", "MARUTI.NS", "SUNPHARMA.NS", "TITAN.NS", "ULTRACEMCO.NS",
    "NESTLEIND.NS", "POWERGRID.NS", "NTPC.NS", "ONGC.NS", "COALINDIA.NS",
    "JSWSTEEL.NS", "TATASTEEL.NS", "HINDALCO.NS", "ADANIENT.NS", "ADANIPORTS.NS",
    "TECHM.NS", "HCLTECH.NS", "LT.NS", "BHARTIARTL.NS", "DIVISLAB.NS",
    "DRREDDY.NS", "CIPLA.NS", "EICHERMOT.NS", "HEROMOTOCO.NS",
]

RSI_LEN = 60
BB_LEN = 40
BB_MULT = 1.4
BUY_RSI_MIN, BUY_RSI_MAX = 1, 40
SELL_RSI_MIN, SELL_RSI_MAX = 60, 99
VOL_LEN = 20

MARKET_OPEN = (9, 20)
MARKET_CLOSE = (15, 10)

# Add/verify 2026 NSE holiday dates before relying on this list
NSE_HOLIDAYS = {
    "2024-01-26", "2024-03-08", "2024-03-25", "2024-03-29", "2024-04-11", "2024-04-17",
    "2024-05-01", "2024-06-17", "2024-07-17", "2024-08-15", "2024-10-02", "2024-11-01",
    "2024-11-15", "2024-12-25",
    "2025-01-26", "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10", "2025-04-14",
    "2025-04-18", "2025-05-01", "2025-06-07", "2025-07-06", "2025-08-15", "2025-08-27",
    "2025-10-02", "2025-10-21", "2025-10-22", "2025-11-05", "2025-11-15", "2025-12-25",
    # TODO: add 2026 dates
}

IST = ZoneInfo("Asia/Kolkata")


# ---------------- MARKET HOURS ----------------
def is_trading_time() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return False
    if now.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
        return False
    open_min = MARKET_OPEN[0] * 60 + MARKET_OPEN[1]
    close_min = MARKET_CLOSE[0] * 60 + MARKET_CLOSE[1]
    cur_min = now.hour * 60 + now.minute
    return open_min <= cur_min <= close_min


# ---------------- INDICATORS ----------------
def calculate_rsi(closes, length):
    if len(closes) < length + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(diff if diff > 0 else 0)
        losses.append(-diff if diff < 0 else 0)

    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length

    first_rs = 100 if avg_loss == 0 else avg_gain / avg_loss
    rsi = [100 - 100 / (1 + first_rs)]

    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        rs = 100 if avg_loss == 0 else avg_gain / avg_loss
        rsi.append(100 - 100 / (1 + rs))
    return rsi


def calculate_bb(values, length, mult):
    upper, lower, basis = [], [], []
    for i in range(length - 1, len(values)):
        window = values[i - length + 1: i + 1]
        mean = sum(window) / length
        variance = sum((v - mean) ** 2 for v in window) / length
        std = variance ** 0.5
        basis.append(mean)
        upper.append(mean + std * mult)
        lower.append(mean - std * mult)
    return upper, lower, basis


def calculate_sma(values, length):
    sma = []
    for i in range(length - 1, len(values)):
        sma.append(sum(values[i - length + 1: i + 1]) / length)
    return sma


# ---------------- DATA FETCH ----------------
def fetch_symbol_data(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "5m", "range": "5d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    result = data.get("chart", {}).get("result")
    if not result:
        print(f"No data for {symbol}: {data.get('chart', {}).get('error')}")
        return None

    r = result[0]
    quotes = (r.get("indicators", {}).get("quote") or [{}])[0]
    closes = quotes.get("close")
    volumes = quotes.get("volume")
    timestamps = r.get("timestamp")

    if not closes or not volumes or not timestamps:
        print(f"Malformed quote data for {symbol}")
        return None

    clean = [
        (c, v, t)
        for c, v, t in zip(closes, volumes, timestamps)
        if c is not None and v is not None
    ]
    if len(clean) < RSI_LEN + BB_LEN:
        print(f"Insufficient data for {symbol}: {len(clean)}")
        return None

    cc = [x[0] for x in clean]
    cv = [x[1] for x in clean]
    ct = [x[2] for x in clean]
    return cc, cv, ct


# ---------------- STRATEGY ----------------
def check_symbol(symbol, last_alerts):
    data = fetch_symbol_data(symbol)
    if data is None:
        return
    closes, volumes, times = data

    rsi = calculate_rsi(closes, RSI_LEN)
    if len(rsi) < BB_LEN + 2:
        print(f"Insufficient RSI series for {symbol}")
        return

    current_rsi, prev_rsi = rsi[-1], rsi[-2]

    upper, lower, _ = calculate_bb(rsi, BB_LEN, BB_MULT)
    if len(lower) < 2:
        return
    upper_bb, lower_bb = upper[-1], lower[-1]
    prev_upper_bb, prev_lower_bb = upper[-2], lower[-2]

    vol_sma = calculate_sma(volumes, VOL_LEN)
    vol_ok = volumes[-1] > vol_sma[-1]

    bb_cross_up = prev_rsi <= prev_lower_bb and current_rsi > lower_bb
    bb_cross_down = prev_rsi >= prev_upper_bb and current_rsi < upper_bb
    buy_cond = BUY_RSI_MIN <= current_rsi <= BUY_RSI_MAX
    sell_cond = SELL_RSI_MIN <= current_rsi <= SELL_RSI_MAX

    key = f"{symbol}_{times[-1]}"
    if last_alerts.get(symbol) == key:
        return  # already alerted for this candle

    if bb_cross_up and buy_cond and vol_ok:
        send_alert(symbol, "CE-BB", current_rsi, closes[-1])
        last_alerts[symbol] = key
    elif bb_cross_down and sell_cond and vol_ok:
        send_alert(symbol, "PE-BB", current_rsi, closes[-1])
        last_alerts[symbol] = key


# ---------------- TELEGRAM ----------------
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    r = requests.post(url, json=payload, timeout=15)
    if not r.ok:
        print(f"Telegram send failed: {r.status_code} {r.text}")


def send_alert(symbol, alert_type, rsi, price):
    emoji = "🟢" if alert_type == "CE-BB" else "🔴"
    label = "BUY (CE)" if alert_type == "CE-BB" else "SELL (PE)"
    time_str = datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S")
    sym_name = "NIFTY" if symbol == "^NSEI" else symbol.replace(".NS", "")

    msg = (
        f"{emoji} <b>{label}</b> - {time_str}\n\n"
        f"📈 <b>{sym_name}</b>\n"
        f"📊 RSI: {rsi:.2f}\n"
        f"💰 Price: {price}\n"
        f"🎯 Strategy: RSI({RSI_LEN}) BB({BB_LEN},{BB_MULT})"
    )
    send_telegram(msg)


# ---------------- STATE PERSISTENCE (across separate GitHub Actions runs) ----------------
STATE_FILE = "last_alerts.json"


def load_last_alerts():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_last_alerts(last_alerts):
    with open(STATE_FILE, "w") as f:
        json.dump(last_alerts, f)


# ---------------- MAIN ----------------
def main():
    if not is_trading_time():
        print("Outside trading hours - skipping")
        return

    last_alerts = load_last_alerts()
    for symbol in SYMBOLS:
        try:
            check_symbol(symbol, last_alerts)
        except Exception as e:
            print(f"Error {symbol}: {e}")
    save_last_alerts(last_alerts)


if __name__ == "__main__":
    main()
