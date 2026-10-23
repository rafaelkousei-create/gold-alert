"""
Gold (XAU/USD) alert bot, with trade exits.

Every 15 minutes it looks at the latest closed candle, combines candlestick
patterns, simple indicators and news headlines into a score, and sends a
Telegram message when a BUY or SELL setup appears.

When a setup appears it also remembers it as an "open trade" with a stop and a
target, and sends you another message when it is time to close it:
  - target reached
  - stop hit (the setup failed)
  - price moved in your favour: move the stop to break-even
  - the opposite signal appeared
  - the trade is too old (time limit)
  - the market is about to close for the weekend

It only sends alerts. It never trades. Not financial advice.
"""
import json
import os
import sys
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import pandas as pd
import requests

# ----------------------------- SETTINGS -----------------------------
SYMBOL = "XAU/USD"
BAR_MINUTES = 15          # candle size in minutes
INTERVAL = "15min"        # must match BAR_MINUTES; change the schedule too
CANDLES = 200             # how many past candles to download
ALERT_SCORE = 4           # higher = fewer but stronger alerts (maximum is 6)
RSI_PERIOD = 14
LEVEL_LOOKBACK = 20       # candles used to find recent support / resistance
NEWS_HOURS = 8            # only headlines from the last N hours count
TIMEZONE = "Europe/Athens"  # times in the messages are shown in this timezone

# --- exit rules ---
STOP_BUFFER_ATR = 0.25    # stop sits this far (in ATRs) beyond the signal candle's low/high
TARGET_R = 1.5            # target = this many times the risk (1.5 = win 1.5 for every 1 risked)
BREAKEVEN_R = 1.0         # when profit reaches this many times the risk, suggest moving the stop to entry
MAX_HOURS = 6             # close the trade after this many hours if neither stop nor target was hit
WEEKEND_EXIT = True       # suggest closing before the Friday market close
WEEKEND_EXIT_HOUR_UTC = 20  # Friday time (UTC) from which the weekend exit applies
STATE_FILE = "state.json" # where the bot remembers the open trade between runs
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
        "price": c.close, "rsi": c.rsi, "atr": c.atr,
        "candle_open_time": c.datetime,
        "invalidation": c.low if action == "BUY" else c.high,
        "trend": "up" if uptrend else "down",
        "bull_pattern": bull_pattern, "bear_pattern": bear_pattern,
    }


# ---------------------------- TRADE STATE ----------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("position", None)
    s.setdefault("history", [])
    s.setdefault("last_entry_candle", None)
    return s


def save_state(state):
    state["history"] = state["history"][-200:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def open_position(a):
    """Turn a BUY/SELL signal into a tracked trade with a stop and a target."""
    buy = a["action"] == "BUY"
    entry = float(a["price"])
    buffer = STOP_BUFFER_ATR * float(a["atr"])
    stop = float(a["invalidation"]) - buffer if buy else float(a["invalidation"]) + buffer
    risk = abs(entry - stop)
    if risk < 0.01:
        return None
    target = entry + TARGET_R * risk if buy else entry - TARGET_R * risk
    opened = a["candle_open_time"]
    return {
        "action": a["action"], "entry": entry, "stop": stop, "target": target,
        "risk": risk, "breakeven": False,
        "last_checked": opened.isoformat(),
        "entry_candle_time": opened.isoformat(),
        "entry_close_time": (opened + pd.Timedelta(minutes=BAR_MINUTES)).isoformat(),
    }


def close_trade(pos, exit_price, reason, close_ts):
    sign = 1 if pos["action"] == "BUY" else -1
    points = sign * (exit_price - pos["entry"])
    return {
        "action": pos["action"], "entry": pos["entry"], "exit": float(exit_price),
        "points": points, "r": points / pos["risk"], "reason": reason,
        "opened": pos["entry_close_time"], "closed": close_ts.isoformat(),
    }


def manage_position(pos, df, a):
    """
    Walk through every candle that closed after the trade was opened.
    Returns (notes, position_or_None, closed_trade_or_None).
    A candle that touches both stop and target counts as a stop (the cautious choice).
    """
    notes = []
    buy = pos["action"] == "BUY"
    entry, risk = pos["entry"], pos["risk"]
    entry_time = pd.Timestamp(pos["entry_candle_time"])
    entry_close = pd.Timestamp(pos["entry_close_time"])
    # Never look at the same candle twice (a moved stop must not be applied to an old candle).
    start = max(entry_time, pd.Timestamp(pos.get("last_checked", pos["entry_candle_time"])))

    for _, k in df[df["datetime"] > start].iterrows():
        close_ts = k.datetime + pd.Timedelta(minutes=BAR_MINUTES)
        hit_stop = k.low <= pos["stop"] if buy else k.high >= pos["stop"]
        hit_target = k.high >= pos["target"] if buy else k.low <= pos["target"]
        if hit_stop:
            reason = "breakeven" if pos["breakeven"] else "stop"
            return notes, None, close_trade(pos, pos["stop"], reason, close_ts)
        if hit_target:
            return notes, None, close_trade(pos, pos["target"], "target", close_ts)

        if not pos["breakeven"]:
            moved = (k.high - entry) if buy else (entry - k.low)
            if moved >= BREAKEVEN_R * risk:
                pos["stop"] = entry
                pos["breakeven"] = True
                notes.append({"kind": "breakeven", "level": entry, "ts": close_ts})

        if close_ts - entry_close >= pd.Timedelta(hours=MAX_HOURS):
            return notes, None, close_trade(pos, k.close, "time", close_ts)
        if WEEKEND_EXIT and close_ts.weekday() == 4 and close_ts.hour >= WEEKEND_EXIT_HOUR_UTC:
            return notes, None, close_trade(pos, k.close, "weekend", close_ts)
        pos["last_checked"] = k.datetime.isoformat()

    # Opposite signal on the newest candle: close and (below) possibly open a new trade.
    latest = df.iloc[-1]
    if (a["action"] and a["action"] != pos["action"]
            and latest["datetime"] > entry_time):
        close_ts = latest["datetime"] + pd.Timedelta(minutes=BAR_MINUTES)
        return notes, None, close_trade(pos, latest["close"], "opposite", close_ts)
    return notes, pos, None


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


def fmt_local(ts):
    return pd.Timestamp(ts).tz_convert(TIMEZONE).strftime("%a %d %b, %H:%M")


def local_time(candle_open):
    return fmt_local(candle_open + pd.Timedelta(minutes=BAR_MINUTES))


def build_alert(a, pos, headlines):
    icon = "🟢 BUY" if a["action"] == "BUY" else "🔴 SELL"
    sign = 1 if a["action"] == "BUY" else -1
    lines = [
        f"{icon} setup on {SYMBOL} ({INTERVAL})",
        f"Entry (candle close): {pos['entry']:,.2f}  ({local_time(a['candle_open_time'])})",
        f"Stop: {pos['stop']:,.2f}  (risk {pos['risk']:,.2f})",
        f"Target: {pos['target']:,.2f}  ({sign * (pos['target'] - pos['entry']):+,.2f}, {TARGET_R:g}x the risk)",
        f"Score: {a['score']}/6",
        "",
        "Why:",
    ]
    lines += [f"• {r}" for r in a["reasons"]]
    if headlines:
        lines += ["", "Recent headlines:"] + [f"- {h}" for h in headlines]
    extra = "or before the Friday market close" if WEEKEND_EXIT else ""
    lines += ["",
              "I will message you when to close: target or stop hit, a move to break-even, "
              f"the opposite signal, after {MAX_HOURS} hours {extra}".rstrip() + ".",
              "Alert only, not financial advice. You decide whether to trade."]
    return "\n".join(lines)


def build_breakeven(pos, note):
    return "\n".join([
        f"🛡️ MOVE STOP TO BREAK-EVEN ({pos['action']} trade on {SYMBOL})",
        f"Price has moved {BREAKEVEN_R:g}x the risk in your favour ({fmt_local(note['ts'])}).",
        f"Consider moving your stop to the entry price: {note['level']:,.2f}.",
        f"The target stays at {pos['target']:,.2f}.",
    ])


def tally(history):
    wins = sum(1 for t in history if t["r"] > 0.05)
    losses = sum(1 for t in history if t["r"] < -0.05)
    even = len(history) - wins - losses
    net = sum(t["r"] for t in history)
    return (f"Record since the bot started: {len(history)} trades, {wins} wins, "
            f"{losses} losses, {even} even, net {net:+.1f}x the risk.")


REASONS = {
    "target": ("🎯", "TARGET REACHED"),
    "stop": ("🛑", "STOP HIT"),
    "breakeven": ("🛡️", "STOPPED AT BREAK-EVEN"),
    "time": ("⏰", f"TIME LIMIT ({MAX_HOURS} HOURS)"),
    "weekend": ("🕗", "MARKET CLOSES FOR THE WEEKEND"),
    "opposite": ("↔️", "OPPOSITE SIGNAL"),
}


def build_exit(t, history):
    icon, title = REASONS[t["reason"]]
    held = pd.Timestamp(t["closed"]) - pd.Timestamp(t["opened"])
    mins = int(held.total_seconds() // 60)
    lines = [
        f"{icon} CLOSE THE {t['action']} TRADE: {title}",
        f"Entry {t['entry']:,.2f}  →  Exit {t['exit']:,.2f}",
        f"Result: {t['points']:+,.2f} points ({t['r']:+.1f}x the risk)",
        f"Held for {mins // 60}h {mins % 60:02d}m  (closed {fmt_local(t['closed'])})",
    ]
    if t["reason"] in ("time", "weekend", "opposite"):
        lines.append("The exit price shown is the candle close; your real price will differ.")
    lines += ["", tally(history)]
    return "\n".join(lines)


# ------------------------------ MAIN ------------------------------
def main():
    missing = [n for n, v in (("TWELVEDATA_API_KEY", API_KEY),
                              ("TELEGRAM_BOT_TOKEN", TG_TOKEN),
                              ("TELEGRAM_CHAT_ID", TG_CHAT)) if not v]
    if missing:
        sys.exit("Missing secret(s): " + ", ".join(missing))

    state = load_state()
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
        pos = state["position"]
        open_line = (f"Open trade: {pos['action']} from {pos['entry']:,.2f} "
                     f"(stop {pos['stop']:,.2f}, target {pos['target']:,.2f})\n"
                     if pos else "Open trade: none\n")
        send("✅ Gold alert bot test OK\n"
             f"Latest closed {INTERVAL} candle: {local_time(a['candle_open_time'])}\n"
             f"Gold price: {a['price']:,.2f}\n"
             f"RSI: {a['rsi']:.1f} | Trend: {a['trend']} | News score: {news_score:+d}\n"
             f"Buy score: {a['buy_score']} | Sell score: {a['sell_score']} "
             f"(alert at {ALERT_SCORE}+)\n"
             + open_line
             + ("" if fresh else "Note: market looks closed (weekend) or data is delayed.\n")
             + "Real alerts will arrive here when a setup appears.")
        return

    messages = []

    # 1) Look after the trade that is already open.
    pos = state["position"]
    if pos:
        notes, pos, closed = manage_position(pos, df, a)
        state["position"] = pos
        for n in notes:
            if state["position"]:      # no point announcing break-even if it already closed
                messages.append(build_breakeven(state["position"], n))
        if closed:
            state["history"].append(closed)
            messages.append(build_exit(closed, state["history"]))

    # 2) Open a new trade if there is a fresh signal and nothing is open.
    candle_key = a["candle_open_time"].isoformat()
    if (state["position"] is None and a["action"] and fresh
            and state["last_entry_candle"] != candle_key):
        new_pos = open_position(a)
        if new_pos:
            state["position"] = new_pos
            state["last_entry_candle"] = candle_key
            messages.append(build_alert(a, new_pos, headlines))

    if not messages:
        print("Nothing to report on this candle.")
    for m in messages:
        send(m)
    save_state(state)  # saved after sending, so a failed message is retried next run


if __name__ == "__main__":
    main()
