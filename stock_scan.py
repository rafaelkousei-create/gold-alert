"""
Daily stock scanner (50 large companies worldwide).

Once a day, after the US market has closed, it looks at the latest DAILY candle
of each stock, scores it with candlestick patterns and simple indicators, and
sends ONE Telegram summary listing the stocks with a BUY or SELL setup.

It only sends alerts. It never trades. Not financial advice.
"""
import os
import sys

import pandas as pd
import requests

# ----------------------------- SETTINGS -----------------------------
ALERT_SCORE = 4          # maximum is 5 here (no news score); lower = more alerts
SEND_WHEN_EMPTY = True   # send a short "no setups" message so you know it is alive
RSI_PERIOD = 14
LEVEL_LOOKBACK = 20      # days used to find recent support / resistance
MIN_CANDLES = 60         # a stock needs at least this many days of history

# Edit this list freely. Use Yahoo Finance ticker symbols.
STOCKS = {
    # --- United States ---
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "GOOGL": "Alphabet",
    "AMZN": "Amazon", "META": "Meta", "TSLA": "Tesla", "BRK-B": "Berkshire Hathaway",
    "JPM": "JPMorgan", "V": "Visa", "JNJ": "Johnson & Johnson", "WMT": "Walmart",
    "XOM": "Exxon Mobil", "UNH": "UnitedHealth", "PG": "Procter & Gamble",
    "KO": "Coca-Cola", "NFLX": "Netflix", "AVGO": "Broadcom", "LLY": "Eli Lilly",
    "COST": "Costco",
    # --- Europe ---
    "ASML.AS": "ASML", "SAP.DE": "SAP", "NESN.SW": "Nestle", "NOVO-B.CO": "Novo Nordisk",
    "MC.PA": "LVMH", "SHEL.L": "Shell", "AZN.L": "AstraZeneca", "ROG.SW": "Roche",
    "SIE.DE": "Siemens", "TTE.PA": "TotalEnergies", "HSBA.L": "HSBC",
    "OR.PA": "L'Oreal", "AIR.PA": "Airbus", "ALV.DE": "Allianz",
    # --- Asia-Pacific ---
    "TSM": "TSMC", "7203.T": "Toyota", "6758.T": "Sony", "9984.T": "SoftBank Group",
    "0700.HK": "Tencent", "9988.HK": "Alibaba", "005930.KS": "Samsung Electronics",
    "RELIANCE.NS": "Reliance Industries", "TCS.NS": "Tata Consultancy",
    "HDFCBANK.NS": "HDFC Bank", "BHP.AX": "BHP", "CBA.AX": "Commonwealth Bank",
    "1211.HK": "BYD",
    # --- Other ---
    "VALE": "Vale", "SHOP.TO": "Shopify", "RY.TO": "Royal Bank of Canada",
}
# --------------------------------------------------------------------

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TEST_MODE = os.environ.get("TEST_MODE", "").strip().lower() == "true"


# ----------------------------- DATA -----------------------------
def download():
    """Download about a year of daily candles for every stock in one request."""
    import yfinance as yf

    data = yf.download(
        list(STOCKS), period="1y", interval="1d", group_by="ticker",
        auto_adjust=False, progress=False, threads=True,
    )
    frames, failed = {}, []
    top = set(data.columns.get_level_values(0)) if len(data.columns) else set()
    for t in STOCKS:
        if t not in top:
            failed.append(t)
            continue
        df = data[t][["Open", "High", "Low", "Close"]].dropna()
        df.columns = ["open", "high", "low", "close"]
        df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
        if len(df) < MIN_CANDLES:
            failed.append(t)
            continue
        frames[t] = df
    return frames, failed


# --------------------------- ANALYSIS ---------------------------
def add_indicators(df):
    df = df.copy()
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + avg_gain / avg_loss)
    prev_close = df["close"].shift()
    true_range = pd.concat([df["high"] - df["low"],
                            (df["high"] - prev_close).abs(),
                            (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    df["atr"] = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    return df


def analyze(df):
    """Score the latest daily candle. Same rules as the gold bot, without news."""
    df = add_indicators(df)
    c, p = df.iloc[-1], df.iloc[-2]

    body = abs(c.close - c.open)
    rng = c.high - c.low
    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low
    body_eff = max(body, rng * 0.05)

    bull_pattern = bear_pattern = None
    if p.close < p.open and c.close > c.open and c.open <= p.close and c.close >= p.open:
        bull_pattern = "Bullish engulfing"
    elif rng > 0 and lower >= 2 * body_eff and upper <= body_eff:
        bull_pattern = "Hammer"
    if p.close > p.open and c.close < c.open and c.open >= p.close and c.close <= p.open:
        bear_pattern = "Bearish engulfing"
    elif rng > 0 and upper >= 2 * body_eff and lower <= body_eff:
        bear_pattern = "Shooting star"

    window = df.iloc[-(LEVEL_LOOKBACK + 1):-1]
    support, resistance = window["low"].min(), window["high"].max()
    near_support = c.low <= support + 0.5 * c.atr
    near_resistance = c.high >= resistance - 0.5 * c.atr
    uptrend = c.ema20 > c.ema50

    buy, buy_why = 0, []
    sell, sell_why = 0, []
    if bull_pattern:
        buy += 2
        buy_why.append(bull_pattern)
    if near_support:
        buy += 1
        buy_why.append("at support")
    if c.rsi < 40:
        buy += 1
        buy_why.append(f"RSI {c.rsi:.0f}")
    if uptrend:
        buy += 1
        buy_why.append("uptrend")

    if bear_pattern:
        sell += 2
        sell_why.append(bear_pattern)
    if near_resistance:
        sell += 1
        sell_why.append("at resistance")
    if c.rsi > 60:
        sell += 1
        sell_why.append(f"RSI {c.rsi:.0f}")
    if not uptrend:
        sell += 1
        sell_why.append("downtrend")

    action = None
    if bull_pattern and buy >= ALERT_SCORE and buy > sell:
        action = "BUY"
    elif bear_pattern and sell >= ALERT_SCORE and sell > buy:
        action = "SELL"

    return {
        "action": action,
        "score": buy if action == "BUY" else sell,
        "reasons": buy_why if action == "BUY" else sell_why,
        "price": c.close,
        "invalidation": c.low if action == "BUY" else c.high,
        "date": df.index[-1],
    }


def scan(frames, today_utc=None, now_hour_utc=None):
    """Analyze every stock. Returns (setups, skipped_stale, as_of_date)."""
    now = pd.Timestamp.now(tz="UTC")
    today = pd.Timestamp(today_utc) if today_utc is not None else now.tz_localize(None).normalize()
    hour = now.hour if now_hour_utc is None else now_hour_utc

    prepared = {}
    for t, df in frames.items():
        # Before the US close, today's candle is still forming. Ignore it.
        if hour < 22 and df.index[-1] >= today:
            df = df.iloc[:-1]
        if len(df) >= MIN_CANDLES:
            prepared[t] = df
    if not prepared:
        return [], [], None

    as_of = max(df.index[-1] for df in prepared.values())
    setups, stale = [], []
    for t, df in prepared.items():
        if df.index[-1] != as_of:      # market was closed that day (holiday): skip
            stale.append(t)
            continue
        a = analyze(df)
        if a["action"]:
            a["ticker"] = t
            setups.append(a)
    setups.sort(key=lambda a: (-a["score"], a["ticker"]))
    return setups, stale, as_of


# -------------------------- NOTIFICATIONS --------------------------
def money(x):
    return f"{x:,.2f}"


def build_message(setups, as_of, scanned, failed, stale):
    day = as_of.strftime("%a %d %b %Y")
    max_score = 5
    lines = [f"📊 Daily stock scan - {day} (daily candles)"]
    buys = [a for a in setups if a["action"] == "BUY"]
    sells = [a for a in setups if a["action"] == "SELL"]
    if not setups:
        lines += ["", f"No setups today. {scanned} stocks checked."]
    for title, group in (("🟢 BUY setups", buys), ("🔴 SELL setups", sells)):
        if not group:
            continue
        lines += ["", f"{title} ({len(group)})"]
        for a in group:
            name = STOCKS.get(a["ticker"], a["ticker"])
            side = "below" if a["action"] == "BUY" else "above"
            lines.append(f"{a['ticker']} ({name}) - score {a['score']}/{max_score}")
            lines.append(f"  Close {money(a['price'])} | invalid {side} {money(a['invalidation'])}")
            lines.append(f"  {', '.join(a['reasons'])}")
    notes = []
    if stale:
        notes.append(f"{len(stale)} market(s) were closed (no new candle): {', '.join(stale)}")
    if failed:
        notes.append(f"No data for: {', '.join(failed)}")
    if notes:
        lines += [""] + notes
    lines += ["", "Alerts only, not financial advice. Check each chart yourself."]
    return "\n".join(lines)


def send(text):
    print(text)
    # Telegram messages are limited to 4096 characters: split if needed.
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = ""
        current += line + "\n"
    chunks.append(current)
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": chunk.rstrip()},
            timeout=30,
        )
        if not r.ok:
            raise RuntimeError(f"Telegram error: {r.text}")


# ------------------------------ MAIN ------------------------------
def main():
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", TG_TOKEN),
                              ("TELEGRAM_CHAT_ID", TG_CHAT)) if not v]
    if missing:
        sys.exit("Missing secret(s): " + ", ".join(missing))

    frames, failed = download()
    setups, stale, as_of = scan(frames)
    if as_of is None:
        send("⚠️ Daily stock scan: no data could be downloaded today. "
             "Yahoo Finance may be down. It will try again tomorrow.")
        return

    scanned = len(frames) - len(stale)
    print(f"Scanned {scanned} stocks, {len(setups)} setups, "
          f"{len(failed)} without data, {len(stale)} closed.")

    if TEST_MODE:
        send(f"✅ Stock scanner test OK\n"
             f"Latest trading day: {as_of.strftime('%a %d %b %Y')}\n"
             f"Stocks with data: {len(frames)} of {len(STOCKS)}\n"
             f"Setups found today: {len(setups)} (alert at {ALERT_SCORE}+ of 5)\n"
             + (f"No data for: {', '.join(failed)}\n" if failed else "")
             + "A summary like this arrives every weekday evening.")
        return

    if setups or SEND_WHEN_EMPTY:
        send(build_message(setups, as_of, scanned, failed, stale))


if __name__ == "__main__":
    main()
