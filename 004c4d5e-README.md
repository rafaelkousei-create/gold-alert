# Gold alert bot: setup guide (no programming needed)

This bot checks gold (XAU/USD) every hour, 24 hours a day, in the cloud for free.
When a candlestick pattern, the indicators and the news line up, it sends you a
BUY or SELL alert on Telegram. It only sends alerts. It never trades for you.

You need three free accounts: Telegram (you probably have it), Twelve Data
(price data) and GitHub (runs the bot). Set aside about 20 minutes.

---

## Step 1: Create your Telegram bot

1. Open Telegram and search for **@BotFather** (blue check mark). Open it and press Start.
2. Send `/newbot`. Choose a name (for example `My Gold Alerts`), then a username
   that ends in `bot` (for example `my_gold_alerts_bot`).
3. BotFather replies with a **token** that looks like `123456789:ABCdef...`.
   Copy it and keep it private. This is your `TELEGRAM_BOT_TOKEN`.
4. Search for your new bot by its username, open it, press Start and send "hi".
   (A bot can only message you after you have written to it first.)
5. Search for **@userinfobot**, open it and press Start. It replies with your
   numeric **Id**. This is your `TELEGRAM_CHAT_ID`.

## Step 2: Get a free price-data key

1. Go to https://twelvedata.com and create a free account.
2. Open the dashboard, find **API Keys** and copy your key.
   This is your `TWELVEDATA_API_KEY`. The free plan is far more than this bot needs.

## Step 3: Put the bot on GitHub

1. Create a free account at https://github.com.
2. Click **+** (top right), then **New repository**. Name it `gold-alert`,
   choose **Private**, and click **Create repository**.
3. Click **uploading an existing file**. Drag in `gold_alert.py` and
   `requirements.txt` (and this README if you like). Click **Commit changes**.
4. Click **Add file**, then **Create new file**. In the name box type exactly:
   `.github/workflows/gold-alert.yml`
   (typing the slashes creates the folders automatically).
   Open the file `gold-alert.yml` I gave you, copy everything and paste it into the big
   text box. Click **Commit changes**.

## Step 4: Add your three secrets

In your repository go to **Settings**, then **Secrets and variables**, then **Actions**,
and click **New repository secret** three times. The names must match exactly:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from Step 1 |
| `TELEGRAM_CHAT_ID` | the Id from Step 1 |
| `TWELVEDATA_API_KEY` | the key from Step 2 |

## Step 5: Test it

1. Open the **Actions** tab. If GitHub asks, click the button to enable workflows.
2. Click **Gold alert** on the left, then **Run workflow**, tick **Send a test message only**,
   and click the green **Run workflow** button.
3. After about a minute you should get a Telegram message with the current gold
   price and scores. If you do, you are done. The bot now runs by itself every hour.

If nothing arrives, open the run in the Actions tab and read the red error line.
The most common causes are a typo in a secret name, or not having pressed Start in
your own bot chat (Step 1, point 4).

---

## How it decides

An alert needs a candlestick pattern in the right direction (bullish or bearish
engulfing, hammer, shooting star). Extra points are added for: price at recent
support or resistance, RSI at an extreme, the short-term trend direction, and the
tone of the latest gold headlines. The score is out of 6 and alerts start at 3.

You can change the numbers in the SETTINGS block at the top of `gold_alert.py`
(for example `ALERT_SCORE = 4` for fewer, stronger alerts). Edit the file on GitHub
with the pencil icon and click Commit.

## Things to know

- **Timing:** it checks once an hour when a candle closes. GitHub can start runs a
  few minutes late. It is not for second-by-second trading.
- **Weekends:** gold trades Sunday evening to Friday evening (UTC). Outside that
  the bot stays quiet.
- **News:** the news score is a simple keyword reading of headlines. It can be wrong,
  so treat it as a small hint only.
- **No guarantees:** no rule set predicts gold reliably. Before risking real money,
  watch the alerts for a few weeks and note how they would have worked out.
  Never risk money you cannot afford to lose. This is not financial advice.
- **If GitHub pauses it:** after long periods of no activity GitHub may switch off
  scheduled runs. Open the Actions tab and re-enable the workflow.
