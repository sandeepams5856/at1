# at1 — NSE / BSE buy & sell signal desk

A read-only dashboard. It pulls candles for Indian indices, NSE/BSE stocks,
commodity contracts, Bitcoin and NSE option chains, runs 19 technical measures over them,
and renders **one BUY / SELL verdict** plus the chart evidence behind it.

It does not trade. There is no broker credential, no order endpoint and no write
path to any exchange — you read the signal here and act on your own terminal.

---

## 1. Run it

```
cd <folder where you put at1>
run.bat
```

`run.bat` starts the server on <http://127.0.0.1:8777> and opens your browser.
Ctrl+C in the console stops it.

**First time only**, create the virtualenv (kept outside OneDrive on purpose —
syncing `site-packages` is slow and corruption-prone):

```
uv venv %USERPROFILE%\.venvs\at1 --python 3.14
%USERPROFILE%\.venvs\at1\Scripts\python.exe -m pip install -r requirements.txt
```

Tests:

```
%USERPROFILE%\.venvs\at1\Scripts\python.exe -m pytest
```

---

## 2. The screen, left to right

```
┌─ header ───────────────────────────────────────────────┬──────────────┐
│ name · LTP · change        5m 15m 30m 1h 1d 1w  ⟳ ☐60s │ Stocks       │
├────────────────────────────────────────────────────────┤ Options      │
│ ▼ STRONG SELL   score −67.8   ▲1 ▼17 ●1   [meter]      │ Bench        │
│ ☑EMA9 ☑21 ☑50 ☑200 ☑VWAP ☐Boll ☐Supertrend ☑arrows     │  group ▾     │
│ ┌ PRICE ────────────────────── candles + EMAs + arrows │  sort  ▾     │
│ ┌ VOLUME ───────────────────── bars + 20-bar average   │  search      │
│ ┌ RSI 14 ────────────────────────────────────────────  │              │
│ ┌ MACD 12/26/9 ──────────────────────────────────────  │  RELIANCE    │
│ ┌ COMPOSITE SIGNAL SCORE ────── the verdict, per bar    │  1,226 −1.4% │
│ Trade plan │ What just happened │ Signal drivers        │  ▼STRONG SELL│
│ All measures (table) │ Last 20 bars (table)             │  HDFCBANK …  │
└────────────────────────────────────────────────────────┴──────────────┘
```

### The right-hand panel — pick what to look at
* **Stocks & Indices / Options / Bench** — the three modes. Bench is the
  compare-several-contracts page; see section 6.
* **Group** — Indices (9), NSE Stocks (60), BSE Stocks (20), Commodities (7),
  Currency (4), Crypto (2: `BTC-USD`, `BTC-INR`). In Options and Bench modes: the 43 NSE underlyings with listed
  options.
* **Sort** — exchange order, strongest buy first, strongest sell first, top
  gainers, top losers. This is the fastest way to use the app: set sort to
  *strongest buy first* and the panel becomes a ranked screener.
* **Search** — filters by name or symbol. Typing a symbol Yahoo knows but the
  catalog doesn't (e.g. `IDEA.NS`) still works.
* Every row carries the live LTP, day change and its own verdict chip, scored on
  whichever timeframe is selected in the header.

### The verdict card — the answer
```
▼ STRONG SELL     composite score −67.8  (coverage 100%)
▲ Buy 1   ▼ Sell 17   ● Neutral 1
−100 sell ─────────────────●──────── buy +100
```
* **Score** is the weighted mean of all 19 measures, rescaled to −100…+100.
* **Bands:** ≥ +45 STRONG BUY · +15…45 BUY · −15…+15 HOLD · −45…−15 SELL ·
  ≤ −45 STRONG SELL.
* **Tally** is how many measures individually read Buy / Sell / Neutral. A score
  of −67 with a 17-to-1 tally is agreement; −20 with 8-to-7 is a coin toss with a
  tilt. Always read the tally next to the score.
* **Coverage** is the share of total weight that actually voted. A newly listed
  stock has no EMA 200, so those measures abstain rather than voting zero. Below
  45% coverage the app refuses to show a verdict at all — that is the `NO DATA`
  case, and it is deliberate.

---

## 3. Reading the five panes

| Pane | What to look at |
|---|---|
| **Price** | Candles, EMA 9 / 21 / 50 / 200, VWAP, optional Bollinger + Supertrend. The **▲ / ▼ arrows are the timing signal** — they mark only the bars where the verdict *crossed into* buy or sell territory, so a 40-bar bull run paints one arrow, not forty. Arrows labelled `BUY` / `SELL` in text are the STRONG flips. |
| **Volume** | Bars coloured by the candle's direction, plus the 20-bar average line. Volume above its average is confirmation; a breakout on below-average volume usually fails. |
| **RSI 14** | 30 / 70 lines drawn. Above 70 is overbought — not a sell on its own, but a bad place to chase. |
| **MACD 12/26/9** | Line, signal and histogram. The histogram flipping sign is the momentum turn. |
| **Composite signal score** | The verdict itself plotted per bar, −100…+100, with the ±15 and ±45 bands. This is the pane to read for *"was this signal already there yesterday, or did it just appear?"* A score climbing through the bands is a developing trend; a score that spikes and snaps back is noise. |

Crosshair anywhere and all five panes move together; each pane's legend shows
that bar's values. Times on the axis are **IST**.

### The EMA / average lines, concretely
* **EMA 9 vs 21** — the fast cross. Short-term momentum.
* **EMA 50 vs 200** — golden cross (up) / death cross (down). The regime.
* **Price vs EMA 21** — whether the pullback is still shallow.
* **VWAP** — intraday it is anchored to the session open, so it is the honest
  "average price paid today"; above it buyers are in control. On daily/weekly
  candles there is no single session to anchor to, so a 20-bar rolling VWAP is
  used instead.

---

## 4. The cards below the charts

* **Trade plan (ATR based)** — entry at the last close, stop at 1.5 × ATR, targets
  at 1.5 R and 3 R, plus the nearest 20-bar swing high/low as resistance/support.
  ATR-scaled, not round numbers, so the same rules work on a ₹2.9 gas contract and
  a 74,000-point Sensex. On a HOLD verdict the plan says **WAIT** and shows no
  levels — by design.
* **What just happened** — plain-language lines for the last few bars: golden/death
  cross, MACD cross, VWAP cross, Supertrend flip, volume spike, RSI extreme,
  20-bar breakout/breakdown, and an "ADX below 18 — no real trend" warning.
* **Signal drivers** — the four measures doing the most to move the score right
  now (|score| × weight). This is *why* the verdict says what it says.
* **All measures** — every voter: its current value, its reading, its −1…+1 score,
  a contribution bar and its weight. Scroll here when you disagree with the
  verdict and want to see who outvoted you.
* **Last 20 bars** — the same data as a table, so nothing on the chart is
  colour-only.

### The 19 measures and their weights

| Group | Measures (weight) |
|---|---|
| Trend | EMA stack 9/21/50/200 **2.5** · Supertrend 10,3 **2.5** · ADX/DI **2.0** · EMA 9v21 **1.5** · EMA 50v200 **1.5** · Price vs SMA 200 **1.0** |
| Momentum | MACD **2.0** · RSI **1.5** · Price vs VWAP **1.5** · Stochastic **1.0** · CCI **1.0** · Rate of change **1.0** · Williams %R **0.8** |
| Volume | Volume confirmation **2.0** · OBV trend **1.5** · MFI **1.0** |
| Volatility / structure | 20-bar breakout **1.5** · Bollinger position **1.0** · Position in 252-bar range **1.0** |

Trend and volume carry the most weight, because direction plus participation is
what actually separates a move from a wiggle. Oscillators *fade* at extremes — RSI
85 scores slightly **negative**, not maximally positive, since exhaustion is not
strength.

---

## 5. Options mode

Pick **Options** in the right panel, then an underlying (NIFTY, BANKNIFTY,
RELIANCE …), an expiry and how many strikes to show either side of ATM.

* **Verdict at the top** is the *price chart's* verdict for that underlying — the
  direction comes from the chart, and the chain only supplies the strike, the
  premium and the level that invalidates it. For FINNIFTY and MIDCPNIFTY, which
  have no usable free price history, the card is labelled **"option chain only"**
  and the direction is inferred from PCR + OI additions + the max-pain gap. That
  is weaker evidence and the label says so.
* **Stat row** — spot, ATM strike, PCR, max pain, OI-wall support/resistance,
  ATM IV and skew.
* **Suggested option trade** — which leg (CE/PE), which strike (slightly ITM on a
  STRONG signal for delta, ATM otherwise), the premium, the breakeven at expiry
  and the spot level that kills the thesis. **A HOLD produces no trade** — buying
  premium without an edge is how theta eats an account.
* **What the chain says** — PCR reading, the max-pain magnet, the OI walls, which
  side today's OI is being added to, and IV skew.
* **Option chain table** — CE on the left, strike in the middle, PE on the right,
  with OI depth bars, change in OI, IV, LTP and the **buildup** column:
  long buildup / short buildup / short covering / long unwinding. The buildup
  column is where the real information in a chain is.

### The Target planner — deciding where to take profit

The suggestion card says which leg to buy. It cannot say where to get out, which is
the number that decides whether the trade is worth taking. The planner is the panel
for that one question. Type the contract in — **strike, leg, entry premium, stop as
a % of the premium, quantity, and how long you will give it** — and it prices six
candidate targets from 0.5:1 to 5:1 against your stop distance.

It loads itself with the ATM strike and the chain's own leg as soon as a chain
opens, so there is always something to read; change any box and it re-plans.

| Column | Where it comes from |
|---|---|
| Target premium | entry + (stop distance × the multiple) |
| Move needed | that target as a % of the premium you are paying |
| Gain | quantity × distance, in rupees |
| **Spot needed** | the spot level at which Black-Scholes makes the premium worth the target, using this leg's own IV. Inverted by bisection, not by a delta approximation |
| Spot move | the same as a percentage — usually far smaller than the premium move, which is the point of buying options |
| Spot needed late | the same target if it takes the **whole horizon** to arrive. Theta means a later target needs a bigger move, and this is the column that prices it |
| Volume traded there | the underlying's own traded volume in the price band that spot level falls in |
| At that strike | traded volume and OI at the listed strike nearest that spot level — where the writers already are |
| **Reached first** | how often, historically, spot made the target's move before making the stop's move |
| IV chance | the risk-neutral chance of *touching* that spot level inside the horizon, from the leg's implied volatility |

**Reached first is a counted frequency, not a probability.** Every closed bar of the
underlying is one attempt: from its close, the target's and the stop's percentage
moves are placed, and the next H bars decide which came first. Only bars with a full
horizon after them count, so lengthening the horizon shrinks the sample rather than
quietly measuring a shorter one. A bar that spans both levels is scored as the
**stop** — the order of prices inside a bar is unknowable and the pessimistic
reading is the survivable one. Below **40 attempts the column is left blank** rather
than printing a confident-looking small number. The caveats travel with the numbers
in *Read this with the numbers* under the table.

**Where spot has actually traded** is the firmer of the two readings, because it is a
fact rather than an estimate: the last 120 bars as a volume profile, price bands
highest-first, with your spot / stop / target levels flagged in the band they fall
in. Each bar's volume is spread *evenly across its high-low range* rather than
dumped on its close — the exchange does not publish where inside a bar the business
was done, and assigning a whole bar to its close would invent a spike at whatever
price those minutes happened to end on. Read the shape as real and the individual
numbers as approximate. A target in a band holding 0.2% of the volume is a price
this market has barely ever paid, and that is worth more than any percentage on the
page. Indices that report no volume on Yahoo get no profile, and the panel says so.

Two things it deliberately does not do. It has **no premium history** to work from —
NSE serves a live chain snapshot only, and there is no free source of Indian option
premium candles — so every number here is about the *underlying's* moves translated
through Black-Scholes, never about what this premium itself did last week. And it
**places nothing**: it is a reading room, and the plan it convinces you of goes into
your broker terminal by hand.

To put several contracts side by side under one set of assumptions instead of
planning them one at a time, use the **Bench** (section 6).

---

## 6. The option bench — ITM against ATM against OTM

The planner answers *"where can this one contract get to?"*. The bench answers the
question that comes before it: **which contract should I be holding at all?** Pick
**Bench** in the right panel and it opens on an ITM / ATM / OTM ladder around the
money — five calls, deepest in-the-money first — which is the comparison most people
want. Then play with it: add a contract (any strike, either leg), drop one, or clear
the bench and build your own list. Up to **eight rows**; more than that stops being
readable across.

The four boxes below the buttons — **stop as a % of premium, quantity, how long you
will give it, and the reward:risk in focus** — apply to *every* row. That is the
whole point: one set of assumptions, so the columns are comparable. Change one and
all the rows re-price together.

| Column | What it tells you |
|---|---|
| Contract · Moneyness | the code (`24000CE`) and an **ITM / ATM / OTM** chip with how far from spot it is, in %. ATM is the chain's own ATM strike, not a tolerance band |
| Premium | the chain LTP you would pay, with the bid–ask underneath |
| **Time value** | how much of that premium is *not* intrinsic value, in rupees and as a share, with the per-session theta beside it. This is the single most useful column on the page |
| Delta | how much of each point of spot reaches the premium |
| Volume | contracts traded today, its share of the same side on screen, and open interest |
| Target / Spot needed | the target premium at the reward:risk in focus and the spot level Black-Scholes needs for it |
| Volume traded there / Reached first / IV chance | as in the planner, but per contract |
| **Target it can keep** | the furthest reward:risk whose *counted* hit rate is still at least even money (50%). Blank when the sample was too thin to say — which is a different claim from "it fails" |

The five tinted columns are the ones that move when you change the reward:risk
selector. Every row has a `Plan ▸` button that opens that contract in the full Target
planner, and a `✕` that takes it off the bench.

**What the comparison says** is the card that does the arguing: which contract needs
the smallest move, what that costs in delta, which is all time value and therefore
expires worthless if nothing happens, which is actually traded, and which spread is
too wide to take the row seriously.

### The one result that will look wrong

Because the stop is a **percentage of each premium**, the cheap far OTM strike shows
the *smallest* spot move needed for the same reward:risk — monotonically, all the way
out. That is the opposite of the usual advice, and it is not an edge:

> every row's stop is the same **percentage** of a different premium, so the cheap far
> strike only has to move a little to gain that percentage — it will almost always
> show the smallest move here. That is arithmetic, not an edge.

What the cheap strike gives up is in the two columns next to it: a much smaller delta
(less of each point reaches you) and 100% time value (nothing at all if spot sits
still). The read card says this in place, and a test pins it so nobody "corrects" it
back to the folk wisdom later.

Same rules as everywhere else: it is a reading room. No premium history exists to
work from, so every spot level is Black-Scholes on the underlying, and nothing here
can send an order.

---

## 7. Timeframes, and which to use

| Pill | Interval | History pulled | Use for |
|---|---|---|---|
| 5 min | 5m | 30 days | intraday scalps |
| 15 min | 15m | 60 days | intraday entries — the usual intraday choice |
| 30 min | 30m | 60 days | intraday swing |
| 1 hour | 60m | 300 days | multi-day positions |
| **1 day** | 1d | 5 years | positional / delivery — the default |
| 1 week | 1wk | 10 years | long-term regime |

The windows are the longest Yahoo allows per interval that still leave enough bars
for a 200-period average. A signal is only as good as its timeframe: check that
the daily and the 15-minute agree before taking an intraday trade against the
daily trend.

---

## 8. Data caveats — read these once

* **Prices are delayed**, typically a few minutes on Indian symbols. Fine for
  reading signals; never treat the LTP here as executable. Verify on your broker.
* **Yahoo is the price feed.** It covers NSE (`.NS`), BSE (`.BO`), the main indices
  (`^NSEI`, `^NSEBANK`, `^BSESN`) and the global commodity contracts.
* **Commodities are the global contract, not MCX.** MCX has no free feed, so
  Natural Gas is Henry Hub (`NG=F`) in **USD**, Crude is WTI/Brent in USD, Gold and
  Silver are COMEX. They track MCX closely but are not the same quote, and there is
  no INR conversion.
* **Crypto trades 24x7.** Bitcoin is Yahoo's `BTC-USD` (USD) and `BTC-INR`
  (rupees). There is no session close, so a daily candle is a UTC day and the
  "change" is against the previous UTC-day close. There are no BTC options here.
* **Several indices report zero volume** on Yahoo. When that happens the volume
  pane is hidden and the volume measures abstain, rather than drawing an empty
  histogram and looking broken.
* **Sector indices are deliberately absent.** Yahoo serves `^CNXAUTO`,
  `NIFTY_FIN_SERVICE.NS` and friends with intraday bars but only one daily bar,
  which makes 200-period averages meaningless.
* **NSE throttles non-browser traffic.** The option chain needs a browser-shaped
  session; if NSE refuses, the options card shows a reason and a retry hint
  instead of an error, and the price-chart signal is unaffected.
* **Weekends and holidays** show the last session's bars. Nothing is stale-cached
  longer than 60–900 seconds depending on timeframe; **Refresh** forces a re-fetch.

---

## 9. Deep links

The URL hash is the state, so any view can be bookmarked:

```
http://127.0.0.1:8777/#symbol=RELIANCE.NS&tf=15m&group=nse&theme=light
http://127.0.0.1:8777/#mode=options&code=BANKNIFTY&tf=1d&strike=54000&leg=PE
http://127.0.0.1:8777/#mode=bench&code=NIFTY&legs=24000CE,24100CE,24200CE&r=2
```

A bench link carries its own list of contracts, so a comparison you liked is a
bookmark. Omit `legs` and it opens on the ITM / ATM / OTM ladder instead.

---

## 10. How the code fits together

```
app/universe.py    the catalog behind the right-hand panel
app/data.py        Yahoo loading, per-timeframe caching, IST handling
app/indicators.py  the indicator maths, straight pandas (no TA-Lib)
app/signals.py     19 weighted voters -> one verdict, markers, trade plan
app/options.py     NSE option chain fetch + the read of it
app/targets.py     the Target planner and the bench: Black-Scholes, greeks,
                   counted hit rates, volume profile, the comparison read
app/main.py        FastAPI endpoints and the static mount
static/            index.html, app.js (Lightweight Charts), styles.css
tests/             106 tests - indicators, signal engine, target planner, bench.
                   No network: fixture chains and synthetic candles throughout
```

JSON endpoints, if you want the numbers without the UI:

| Endpoint | Returns |
|---|---|
| `/api/analyze?symbol=^NSEI&tf=1d` | verdict, all measures, drivers, events, plan, chart series |
| `/api/watchlist?symbols=A,B,C&tf=1d` | one scored row per symbol |
| `/api/options/chain?symbol=NIFTY&expiry=…&window=10` | parsed chain, notes, suggestion |
| `/api/options/targets?symbol=NIFTY&strike=24000&leg=CE&stop_pct=25&qty=75&horizon=3` | candidate targets, spot needed, volume there, hit rates |
| `/api/options/bench?symbol=NIFTY&legs=24000CE,24100CE&stop_pct=25&focus=2` | several contracts on one stop: moneyness, greeks, the target each keeps, the comparison read. Drop `legs` for the ITM/ATM/OTM ladder |
| `/api/options/expiries?symbol=NIFTY` | listed expiry dates |
| `/api/universe` | groups, option underlyings, timeframes |

---

## 11. What this is not

Signals are computed from delayed public data for study and planning. This is not
investment advice, no backtest or forward test has been run on this weighting, and
the app never places an order. Position sizing, slippage and taxes are yours to
model.

Charts are drawn with [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts)
(Apache-2.0), loaded from a CDN.
