# Street Smarts (Raschke / Connors, 1995) — Strategy Catalog for Pella

Source PDF: `<DOWNLOADS>/Street Smarts High Probability Short-Term Trading Strategies (Linda Bradford Raschke, Laurence A. Connors) (Z-Library).pdf`

Extracted text: `C:/Lab/strategies/_research/StreetSmarts.txt`

## How to read this catalog

The book is organized into **four families** of swing patterns. Each pattern below is rated for **port priority** to MQL5/MT5:

- **PORT NOW** — fully mechanical, clean rules, ports cleanly to ~150-200 LOC of MQL5
- **PORT LATER** — mechanical but subtle (oscillator hooks, multi-condition triggers); needs careful spec
- **SKIP** — subjective pattern recognition. Authors themselves say "impossible to backtest"

We already have one strategy in this family — **TuesdayTurnaroundNDX** (Tuesday seasonality / pattern), already ported, PASSING gates on XAUUSD. It's NOT explicitly from this book but is the same archetype as the patterns here.

---

## PART ONE — TESTS (false-breakout reversal)

### 1. Turtle Soup (Ch 4) — **PORT NOW** ★

**Concept:** Famous 20-day Donchian breakout fails, market reverses. Trade the reversal.

**Rules (LONG, sells reversed):**
- Today must make a new 20-day LOW
- Previous 20-day low must have been at least **4 trading sessions earlier**
- After the new low, place buy stop **5-10 ticks above the previous 20-day low**
- Stop = 1 tick under today's low
- Exit: trailing stop, holds 2-3 hours to a few days

**Re-entry rule:** if stopped out on day 1 or day 2, re-enter at the same buy stop price.

**Why this works:** ChannelBreakoutVIP_MT5 is a Donchian *breakout* strategy. Turtle Soup is the *opposite side* of the same trade — fades the breakout. **Naturally uncorrelated** with our existing winners. High-value port.

**Defaults:** Period=20, MinSessionsSinceLast=4, EntryOffset=5 ticks, FullStop=today's low.

---

### 2. Turtle Soup Plus One (Ch 5) — **PORT NOW** ★

**Concept:** Same as Turtle Soup but the reversal happens **one day after** the breakout instead of intraday. Catches the late momentum players.

**Rules (LONG):**
- Day 1: market makes a new 20-day low. **Close must be at or below the previous 20-bar low.**
- Day 2: place buy stop **at the earlier 20-day low** (not 5-10 ticks above). If not filled, cancel.
- Stop = 1 tick under the lower of (day-1 low, day-2 low)
- Exit: partial profits in 2-6 bars, trail rest

**Why ports cleanly:** discrete daily logic, no intraday timing dependency. Easier than Turtle Soup to backtest.

**Defaults:** Period=20, MinSessionsSinceLast=3, EntryAtPriorLow.

---

### 3. 80-20's (Ch 6) — **PORT NOW** ★

**Concept:** Day-trade reversal. After a day that opens in the top 20% and closes in the bottom 20% of its range (or vice versa), the next day's morning move usually reverses.

**Rules (LONG, sells reversed):**
- Yesterday: opened in top 20% of daily range AND closed in bottom 20% of daily range
- Today: market trades **5-15 ticks below yesterday's low**
- Place buy stop **at yesterday's low**
- Stop near today's low
- **Day-trade only** — exit by close

**Why ports cleanly:** purely daily-bar conditions. ~50 LOC.

**Defaults:** RangeOpenThreshold=0.20, RangeCloseThreshold=0.80, BelowLowOffset=5 ticks, FlattenAtClose=true.

---

### 4. Momentum Pinball (Ch 7) — **PORT LATER**

**Concept:** Use a 3-period RSI of the 1-period rate-of-change (LBR/RSI) as an overbought/oversold filter. Enter on the breakout of the FIRST HOUR's range the next day.

**Rules (LONG):**
- Day 1: LBR/RSI < 30 (or > 70 for shorts)
- Day 2: place buy stop above the high of the first hour's trading range
- Stop = first hour's low
- If profitable at close, hold overnight; exit next morning

**Why "later":** the "first hour's range" is intraday-specific. Need higher-resolution data + careful definition of "first hour" for FX markets which trade 24h. Spec needs work.

**Useful insight even without porting:** the LBR/RSI indicator (3-RSI of 1-ROC) is a great overbought/oversold filter that we could add to other strategies.

---

### 5. 2-Period ROC (Ch 8) — **NOT A STRATEGY, an INDICATOR**

The chapter is about an indicator/pivot point that signals which day is a "buy day" vs "sell day" in Taylor's framework. Useful as a confirming filter for other patterns.

**Pivot calculation:** `pivot = close[1] + (close[1] - close[3])`. Flip long ↔ short when daily close crosses this pivot.

**Use:** as a `regime detector` or `confirmation filter` to ADD to other strategies. Don't port standalone.

---

## PART TWO — RETRACEMENTS

### 6. The Anti (Ch 9) — **PORT LATER**

**Concept:** Stochastic-based trend-pullback entry. When %D (slow) is rising and %K (fast) hooks back up, enter long.

**Rules (LONG):**
- 7-period %K (smoothed 4 by default if available) and 10-period %D stochastic
- Slow line %D has a definite UPWARD slope (uptrend in momentum)
- Fast line %K rises toward %D, then begins a small consolidation/retracement
- Enter when %K turns back up (forms a hook in the same direction as %D)
- Stop = below the bar of entry
- Exit = within 2-4 bars OR a buying climax / range expansion

**Why "later":** "%K hooks back up" requires bar-by-bar slope detection. Doable but needs careful definition. Also "%D slope = positive for at least 3 days" is a numeric threshold, fine to encode.

**Worth porting** because it's a different mechanism (stochastic-momentum-pullback) than our current Donchian/Keltner crowd.

---

### 7. The Holy Grail (Ch 10) — **PORT NOW** ★

**Concept:** ADX-based trend-pullback. When trend is strong (ADX > 30) and price retraces to the 20-EMA, buy the next bar's high.

**Rules (LONG):**
- 14-period ADX > 30 AND rising → confirms strong trend
- Wait for price retracement to touch the 20-period EMA
- Place buy stop **above the high of the previous bar**
- Stop = newly-formed swing low
- Exit = previous swing high (or trail through)
- After successful trade, ADX must rise above 30 again before re-entering

**Why ports cleanly:** all components are off-the-shelf indicators (ADX, EMA). Stop and target are bar-relative (swing low / swing high). ~200 LOC.

**Different mechanism than what we have:** we have breakout strategies (Donchian/Keltner) and one pattern strategy (TuesdayTurnaround). Holy Grail is a **trend-continuation pullback** — third archetype. Should have low correlation with breakouts.

**Defaults:** ADXPeriod=14, ADXThreshold=30, EMAPeriod=20.

---

### 8. ADX Gapper (Ch 11) — **PORT NOW**

**Concept:** Counter-gap entry in trending market. When trend is strong, buy gaps that go against the trend.

**Rules (LONG):**
- 12-period ADX > 30
- 28-period +DI > -DI (uptrend)
- Today's open gaps **below** yesterday's low
- Place buy stop **at yesterday's low**
- Stop = today's low
- Exit before close OR carry overnight if strong close

**Why ports cleanly:** ADX, +DI/-DI are standard indicators. Daily gap detection is trivial in MQL5. ~150 LOC.

**Defaults:** ADXPeriod=12, DIPeriod=28, ADXThreshold=30.

---

## PART THREE — CLIMAX REVERSALS (mostly subjective)

### 9. Whiplash (Ch 12) — **PORT NOW**

**Concept:** Day gaps lower, then reverses up by close → buy on close, exit next morning.

**Rules (LONG):**
- Today gaps **lower** than yesterday's low
- Today's close > today's open
- Today's close is in the top 50% of today's range
- BUY MOC (market on close)
- If tomorrow opens below today's close, EXIT IMMEDIATELY (cut loss)
- If tomorrow opens with profit, trail stop to capture

**Why ports cleanly:** all daily-bar conditions, deterministic. ~100 LOC. **MOC entry is unusual** — most retail strategies enter intraday; MOC entries are less crowded.

**Defaults:** GapDirection=lower-than-prev-low, CloseRangePct=0.50, ExitOnLossOpen=true.

---

### 10. Three-Day Unfilled Gap Reversal (Ch 13) — **PORT NOW**

**Concept:** Day gaps and doesn't fill. Within 3 trading sessions, price comes back to fill the gap → take it as a continuation entry.

**Rules (LONG, after a gap-down day):**
- Today: market gaps lower AND does not fill the gap intraday
- For the next 3 trading sessions: place a buy stop **1 tick above the high of the gap-down day**
- If filled: stop = low of gap-down day
- If not filled in 3 sessions: cancel

**Why ports cleanly:** trivial daily-bar conditions + a 3-day countdown. ~120 LOC.

---

### Chapters 14-18: Spike & Ledge, Three Little Indians, Fakeout-Shakeout, Wolfe Waves, news-reversals — **SKIP**

The authors explicitly say: *"these patterns are a purely subjective form of pattern recognition... it is impossible to do any form of back testing on them."* Not portable to mechanical EAs.

---

## PART FOUR — BREAKOUT MODE (volatility expansion)

### 11. ID/NR4 (Ch 19) — **PORT NOW** ★★ TOP PRIORITY

**Concept:** When today is BOTH an Inside Day AND the Narrowest Range of last 4 days → bracket the next day with OCO buy/sell stops. Volatility breakout.

**Rules:**
- Today: high < previous high AND low > previous low (Inside Day)
- AND today's range = min(range, range[1], range[2], range[3]) (NR4)
- Tomorrow: place buy stop 1 tick above today's high AND sell stop 1 tick below today's low
- If filled long: also place a sell stop below today's low → **stop and reverse** if it whips against you
- Exit MOC if not profitable within 2 days

**Why this is the #1 port from this book:**
- Fully mechanical, no judgment
- Trades **both directions** (OCO bracket)
- Entirely complements our long-only Donchian/Keltner crowd
- Stop-and-reverse logic catches the common false-breakout-then-real-breakout pattern
- Holding period 1-4 days = different from our intraday-to-multiday strategies
- Pure **volatility expansion** edge — not trend, not reversion. Third uncorrelated mechanism.

**Defaults:** InsideDayCheck=true, NR4Period=4, EntryOffset=1 tick, MaxHoldDays=2, StopReverse=true.

---

### 12. Historical Volatility + NR4 (Ch 20) — **PORT NOW** ★

**Concept:** Same as ID/NR4 but with an extra HV filter to be more selective.

**Rules (extends ID/NR4):**
- 6-day historical volatility / 100-day historical volatility < 0.50 (volatility compressed to half its longer-term reading)
- Today: Inside Day OR NR4 (note: OR, not AND — looser than ID/NR4)
- Tomorrow: bracket with buy/sell stops above/below today's range

**Why port:** essentially "ID/NR4 with quality filter." More selective = fewer trades but higher quality. Should produce different return profile from raw ID/NR4 — both worth porting and comparing.

**Defaults:** HVRatioThreshold=0.50, ShortHVPeriod=6, LongHVPeriod=100.

---

## Cross-reference: where does TuesdayTurnaroundNDX fit?

The book mentions **Taylor's trading technique** repeatedly (Ch 7, 8) — a "buy day / sell day / sellshort day" rhythm based on day-of-week and recent action. **TuesdayTurnaroundNDX is a clock-driven specialization of this idea.** The 58% win rate / 1.06× asymmetry signature is exactly what Raschke describes for these patterns. The strategy fits cleanly into Part One Tests + the Taylor seasonality framework.

---

## Recommended port order for Pella (priority-weighted by gap-fill in our portfolio)

| # | Strategy | Why this order |
|---|---|---|
| 1 | **ID/NR4** | OCO bracket, both directions, volatility-expansion mechanism — fills the gap left by all our long-only trend-followers. Highest portfolio-diversity value. |
| 2 | **HV + NR4** | Selectivity-filtered version of #1. Portfolio: A/B variants. |
| 3 | **Holy Grail** | ADX-pullback retracement entry — third archetype (not breakout, not reversal). Different mechanism = potentially low correlation. |
| 4 | **Turtle Soup** | Fades Donchian breakouts → naturally uncorrelated with ChannelBreakoutVIP / Gen_Breakout. |
| 5 | **Turtle Soup Plus One** | Variant of #4, easier to backtest cleanly. |
| 6 | **80-20's** | Day-trade reversal. Adds intraday horizon to portfolio. |
| 7 | **Whiplash** | Gap-reverse MOC entry — unusual entry style, less crowded. |
| 8 | **3-Day Unfilled Gap** | Gap-fill momentum entry. |
| 9 | **ADX Gapper** | Gap-into-trend continuation. |
| 10 | **The Anti** | Stochastic-hook pullback. Lowest priority — overlaps conceptually with Holy Grail. |

After porting #1-3, we'd have **6 archetypes covered**:
- Trend-following breakouts (Gen_Breakout, ChannelBreakoutVIP)
- Trend-following channel hybrid (GoldTrendBreakout)
- Pattern/seasonality (TuesdayTurnaroundNDX)
- Volatility expansion OCO (ID/NR4) ← gap to fill
- Volatility expansion filtered (HV+NR4) ← gap to fill
- Trend-pullback retracement (Holy Grail) ← gap to fill

## Notes for spec-writing

The book's **money management rules** (Ch 3) are universal across strategies and should be baked into every port:

1. Enter the **entire** position at once (no scaling in)
2. Place initial protective stop **immediately** on fill
3. Scale OUT as profitable (partial profits)
4. On parabolic moves / range expansion bars, tighten or exit
5. **Always have a stop in the market** — never rely on mental stops

Modern compliance overlay (CLAUDE.md / LucidFlex rules):
- Fixed dollar SL (e.g. `$500`) IN ADDITION TO the strategy's structural stop
- Daily loss circuit breaker
- Position size = risk-percent of equity / pip-distance-to-stop
- Min hold time of 10s (anti-microscalping rule)
