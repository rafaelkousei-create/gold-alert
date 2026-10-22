"""
Gold (XAU/USD) alert bot.

Every hour it looks at the latest closed 1-hour candle, combines candlestick
patterns, simple indicators and news headlines into a score, and sends a
Telegram message when a BUY or SELL setup appears.

It only sends alerts. It never trades. Not financial advice.
"""
import os
import sys
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import pandas as pd
import requests

# ----------------------------- SETTINGS -----------------------------
SYMBOL = "XAU/USD"
BAR_MINUTES = 60          # candle size in minutes (60 = 1-hour candles)
INTERVAL = "1h"           # must match BAR_MINUTES; change the schedule too
CANDLES = 200             # how many past candles to download
ALERT_SCORE = 3           # higher = fewer but stronger alerts (maximum is 6)
RSI_PERIOD = 14
LEVEL_LOOKBACK = 20       # candles used to find recent support / resistance
NEWS_HOURS = 8            # only headlines from the last N hours count
TIMEZONE = "Europe/Athens"  # times in the alert are shown in this timezone
# --------------------------------------------------------------------

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
API_KEY = os.environ.get("TWELVEDATA_API_KEY", "")
TEST_MODE = os.environ.get("TEST_MODE", "").strip().lower() == "true"

# Words that tend to be good for gold / bad for gold in headlines.
DOLLAR_BULL = ["dollar falls", "dollar slips", "dollar weakens", "dollar drops",
               "dollar retreats", "weaker dollar", "dollar slides"]
DOLLAR_BEAR = ["dollar rises", "dollar gains", "dollar strengthens", "dollar climbs",
               "dollar jumps", "stronger dollar", "firmer dollar"]
BULL_WORDS = ["rate cut", "dovish", "safe-haven", "safe haven", "geopolitical",
              "tensions", "escalat", "record high", "rally", "rallies", "surge",
              "jumps", "rebounds", "central bank buying", "inflation fears",
              "yields fall", "yields drop"]
BEAR_WORDS = ["rate hike", "hawkish", "yields rise", "yields climb", "sell-off",
              "selloff", "plunge", "slump", "falls", "drops", "slides", "declines",
              "tumbles", "profit-taking", "ceasefire", "easing tensions",
              "de-escalat", "two-month low", "lowest since"]


# ----------------------------- DATA -----------------------------
def get_candles():
    """Download recent candles and drop the one that is still forming."""
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": SYMBOL, "interval": INTERVAL, "outputsize": CANDLES,
                "timezone": "UTC", "apikey": API_KEY},
        timeout=30,
    )
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"Price data error: {data.get('message', data)}")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df = df.sort_values("datetime").reset_index(drop=True)
    now = pd.Timestamp.now(tz="UTC")
    if df.iloc[-1]["datetime"] + pd.Timedelta(minutes=BAR_MINUTES) > now:
        df = df.iloc[:-1].reset_index(drop=True)  # last candle not closed yet
    return df


def headline_score(title):
    t = title.lower()
    score = 0
    for phrase in DOLLAR_BULL:
        if phrase in t:
            score += 1
            t = t.replace(phrase, " ")
    for phrase in DOLLAR_BEAR:
        if phrase in t:
            score -= 1
            t = t.replace(phrase, " ")
    score += sum(w in t for w in BULL_WORDS)
    score -= sum(w in t for w in BEAR_WORDS)
    return (score > 0) - (score < 0)


def get_news():
    """Return (news_score in -1/0/+1, list of recent headlines)."""
    try:
        r = requests.get(
            "https://news.google.com/rss/search",
            params={"q": "gold price when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"},
            timeout=30,
        )
        root = ET.fromstring(r.content)
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=NEWS_HOURS)
        headlines, net = [], 0
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            low = title.lower()
            if not any(k in low for k in ("gold", "bullion", "xau")):
                continue
            try:
                published = pd.Timestamp(parsedate_to_datetime(item.findtext("pubDate")))
                if published.tzinfo is None:
                    published = published.tz_localize("UTC")
            except Exception:
                continue
            if published < cutoff:
                continue
            net += headline_score(title)
            headlines.append(title)
        news_score = 1 if net >= 2 else -1 if net <= -2 else 0
        return news_score, headlines[:3]
    except Exception as e:  # news is a bonus; never stop the bot because of it
        print(f"News unavailable: {e}")
        return 0, []


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


def analyze(df, news_score):
    """Score the latest closed candle. Returns a dictionary with the result."""
    df = add_indicators(df)
    c, p = df.iloc[-1], df.iloc[-2]

    body = abs(c.close - c.open)
    rng = c.high - c.low
    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low
    body_eff = max(body, rng * 0.05)

    bull_pattern = bear_pattern = None
    if p.close < p.open and c.close > c.open and c.open <= p.close and c.close >= p.open:
        bull_pattern = "Bullish engulfing candle"
    elif rng > 0 and lower >= 2 * body_eff and upper <= body_eff:
        bull_pattern = "Hammer candle (rejection of lower prices)"
    if p.close > p.open and c.close < c.open and c.open >= p.close and c.close <= p.open:
        bear_pattern = "Bearish engulfing candle"
    elif rng > 0 and upper >= 2 * body_eff and lower <= body_eff:
        bear_pattern = "Shooting star candle (rejection of higher prices)"

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
        buy_why.append(f"Price at recent support ({support:,.2f})")
    if c.rsi < 40:
        buy += 1
        buy_why.append(f"RSI is low ({c.rsi:.1f})")
    if uptrend:
        buy += 1
        buy_why.append("Short-term trend is up (EMA20 above EMA50)")
    if news_score > 0:
        buy += 1
        buy_why.append("Recent gold news leans positive")
    elif news_score < 0:
        buy -= 1
        buy_why.append("Recent gold news leans negative (reduces score)")

    if bear_pattern:
        sell += 2
        sell_why.append(bear_pattern)
    if near_resistance:
        sell += 1
        sell_why.append(f"Price at recent resistance ({resistance:,.2f})")
    if c.rsi > 60:
        sell += 1
        sell_why.append(f"RSI is high ({c.rsi:.1f})")
    if not uptrend:
        sell += 1
        sell_why.append("Short-term trend is down (EMA20 below EMA50)")
    if news_score < 0:
        sell += 1
        sell_why.append("Recent gold news leans negative")
    elif news_score > 0:
        sell -= 1
        sell_why.append("Recent gold news leans positive (reduces score)")

    # A candlestick pattern in the right direction is required for any alert.
    action = None
    if bull_pattern and buy >= ALERT_SCORE and buy > sell:
        action = "BUY"
    elif bear_pattern and sell >= ALERT_SCORE and sell > buy:
        action = "SELL"

    return {
        "action": action,
        "score": buy if action == "BUY" else sell,
        "buy_score": buy, "sell_score": sell,
        "reasons": buy_why if action == "BUY" else sell_why,
        "price": c.close, "rsi": c.rsi,
        "candle_open_time": c.datetime,
        "invalidation": c.low if action == "BUY" else c.high,
        "trend": "up" if uptrend else "down",
        "bull_pattern": bull_pattern, "bear_pattern": bear_pattern,
    }


# -------------------------- NOTIFICATIONS --------------------------
def send(text):
    print(text)
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": text},
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram error: {r.text}")


def local_time(ts):
    close_time = ts + pd.Timedelta(minutes=BAR_MINUTES)
    return close_time.tz_convert(TIMEZONE).strftime("%a %d %b, %H:%M")


def build_alert(a, headlines):
    icon = "🟢 BUY" if a["action"] == "BUY" else "🔴 SELL"
    lines = [
        f"{icon} setup on {SYMBOL} ({INTERVAL})",
        f"Price: {a['price']:,.2f}  (candle closed {local_time(a['candle_open_time'])})",
        f"Score: {a['score']}/6",
        "",
        "Why:",
    ]
    lines += [f"• {r}" for r in a["reasons"]]
    side = "below" if a["action"] == "BUY" else "above"
    lines += ["", f"Setup is invalid if price goes {side} {a['invalidation']:,.2f}"]
    if headlines:
        lines += ["", "Recent headlines:"] + [f"- {h}" for h in headlines]
    lines += ["", "Alert only, not financial advice. You decide whether to trade."]
    return "\n".join(lines)


# ------------------------------ MAIN ------------------------------
def main():
    missing = [n for n, v in (("TWELVEDATA_API_KEY", API_KEY),
                              ("TELEGRAM_BOT_TOKEN", TG_TOKEN),
                              ("TELEGRAM_CHAT_ID", TG_CHAT)) if not v]
    if missing:
        sys.exit("Missing secret(s): " + ", ".join(missing))

    df = get_candles()
    news_score, headlines = get_news()
    a = analyze(df, news_score)

    close_time = a["candle_open_time"] + pd.Timedelta(minutes=BAR_MINUTES)
    age = pd.Timestamp.now(tz="UTC") - close_time
    fresh = age <= pd.Timedelta(minutes=BAR_MINUTES * 1.25)

    print(f"Last closed candle: {local_time(a['candle_open_time'])} | price {a['price']:,.2f} | "
          f"RSI {a['rsi']:.1f} | trend {a['trend']} | news {news_score:+d} | "
          f"buy {a['buy_score']} sell {a['sell_score']}")

    if TEST_MODE:
        send("✅ Gold alert bot test OK\n"
             f"Latest closed {INTERVAL} candle: {local_time(a['candle_open_time'])}\n"
             f"Gold price: {a['price']:,.2f}\n"
             f"RSI: {a['rsi']:.1f} | Trend: {a['trend']} | News score: {news_score:+d}\n"
             f"Buy score: {a['buy_score']} | Sell score: {a['sell_score']} "
             f"(alert at {ALERT_SCORE}+)\n"
             + ("" if fresh else "Note: market looks closed (weekend) or data is delayed.\n")
             + "Real alerts will arrive here when a setup appears.")
        return

    if not fresh:
        print("No fresh candle (market closed or data delayed). Nothing to do.")
        return
    if a["action"]:
        send(build_alert(a, headlines))
    else:
        print("No setup on this candle.")


if __name__ == "__main__":
    main()
