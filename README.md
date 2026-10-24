# AI Hedge Fund

This is a proof of concept for an AI-powered hedge fund. The goal of this project is to explore the use of AI to make trading decisions. This project is for **educational** purposes only and is not intended for real trading or investment.

<img width="2400" height="1460" alt="image" src="https://github.com/user-attachments/assets/e3985623-c226-4c1a-a587-e03fb4eca31e" />


Note: the system does not actually make any trades.

[![Twitter Follow](https://img.shields.io/twitter/follow/virattt?style=social)](https://twitter.com/virattt)

## Disclaimer

This project is for **educational and research purposes only**.

- Not intended for real trading or investment
- No investment advice or guarantees provided
- Creator assumes no liability for financial losses
- Consult a financial advisor for investment decisions
- Past performance does not indicate future results

By using this software, you agree to use it solely for learning purposes.

## How to Install

```bash
pipx install aihf
```

(or `uv tool install aihf`, or `pip install aihf` into an environment of your choice)

Then run it from anywhere:

```bash
aihf
```

### API keys

The app asks for keys the first time it needs them and saves them to `~/.hedge-fund/.env` — nothing to configure up front. It needs:

- A [Financial Datasets](https://financialdatasets.ai) API key, for prices, fundamentals, and earnings.
- One model API key for the investor agents. Supported providers: Anthropic, OpenAI, DeepSeek, Google, xAI, Kimi, TypeSafe (Jev).

Keys exported in your shell always win over the saved file.

## How to Run

### Interactive app

```bash
aihf
```

With no arguments, this launches the interactive terminal app. It has two modes:

- **Paper trading** — your funds, each with a ledger of real market days and fake money. Highlight a fund to see what its next run will do; press enter to run it through the next completed session (an approval step shows the exact decision about to execute before anything trades), `s` for its full session history, `h`/`r` for the kill switch. Build a new fund from the same list: strategies, capital, cadence, tickers, and it is live. Every session is appended to a hash-chained ledger in `~/.hedge-fund/paper/<name>/`, so NAV is a track record, not a reset.
- **Backtesting** — replay a fund over history and watch its equity curve draw against its benchmark. Results are saved to `~/.hedge-fund/research/`. Fund definitions live in `~/.hedge-fund/mandates/` and carry no tickers; you pick the universe per backtest.

### The FOMO team (the main desk)

`aihf web` opens straight onto the **FOMO desk**: a team of agents that follows the top traders on the FOMO app and paper-copies their trades. Every trade becomes a **case file**, and every agent's message is in it:

1. **FOMO Link** is wired to the top traders' wallets (every 30 seconds with a private Solana address, every minute on the free public one) and reports each trade the moment it lands on the **Live wire**.
2. **Front Desk** opens the case and routes it.
3. **The council** votes: Copy Scout, Pool Guard, Too-Late Check, Crowd Count, Track Record, Market Shield (SOL only) and FOMO Meter. Any veto closes the case.
4. **Social Scout** scrolls X and the web (news, Reddit, forums, public Instagram/TikTok/YouTube posts) through Grok's built-in X and web search, and reports mood, buzz, pump risk, red flags and good signs. A coordinated pump, a rug or scam warning, or a crowd turning negative is a veto. Instagram and Snapchat can't be searched directly by any program, so the Scout only sees what they publish to the open web. Needs an xAI key.
5. **Second Opinion**, an independent AI on the model picked under Keys & model, reads the whole case file and must approve. It can only stop a trade, never enlarge one.
6. **Front Desk double-checks** a fresh price: if it jumped 5% or the pool lost 30% while the team was checking, the buy is cancelled.
7. **Manager** buys, sized by the votes.
8. At **+50% and +100%** (and +200%, +400%), the **Exit Desk** reviews with a fresh Scout report and whether the traders we copied still hold: hold, take half, or withdraw. If every trader we copied has sold out, it withdraws. The trailing stop and stop-loss stay in code and can't be argued with.

Paid AI checks are capped per day (25 by default, about $20 a month); past the cap, the free checks still run. Stock funds are still there under **Stock funds**, but FOMO is the main desk. Paper money only throughout.

### Web dashboard

```bash
aihf web
```

Opens an arcade-style dashboard at http://localhost:8000 for your paper funds: each fund as a save file, the analysts as a party with their latest views per stock, fund value against the S&P 500, holdings, the next plan, and a battle log of every trade, risk trim and halt. **Play next day** shows what will happen and asks before it runs one session (the same `tick` the CLI uses); **Freeze** is the kill switch. You can add keys, pick the AI model and start a new fund from the page.

The home screen opens on **High Scores**: every fund ranked by how far it is ahead of the S&P 500 over its own days, with its return, worst drop and days played. Funds under 21 days are marked *early*, because that is still mostly luck. Each fund earns XP (10 per day played, 5 more per day it is ahead of the S&P 500, 25 per trophy; trading more earns nothing), levels up from Intern to Legend, and collects 16 trophies in its Trophy room, from Opening Bell to Boss Slayer (ahead of the S&P 500 after 63+ days). Level-ups and new trophies get a fanfare; a played day counts the fund value up and shows the gain or loss. Sound effects are made in the browser; turn them off with the ♪ button. Animations respect your system's reduce-motion setting.

**Auto-play.** Switch it on from the home screen and every fund plays its finished days by itself, checking every 30 minutes while the dashboard is open (missed days are caught up in order, up to 30 per check). Frozen funds and funds missing a key are skipped, with the reason shown. To play even when the dashboard is closed, run `aihf play-all` from Windows Task Scheduler (the dashboard's *Window closed?* button has the one-line command); it logs to `~/.hedge-fund/autoplay.log`.

**Alerts.** The Alerts button lists what changed: insiders or a famous fund buying, Congress buying, Hype Watch warnings, pump alarms, the market shield going up or down, ±3% days, drawdowns past 10/20/30%, a fund freezing, and a fund passing the go-live check. Phone alerts are free through the [ntfy](https://ntfy.sh) app: subscribe to a private topic and save the same topic in Alerts (`NTFY_TOPIC`). Pick which kinds to receive. Desktop pop-ups work while the tab is open.

**Go-live check.** Each fund shows eight checks a paper record should pass before real money is even considered: six months played, ahead of the S&P 500, still ahead after estimated trading costs (0.1% per trade), smaller drops, a better Sharpe ratio, ahead in most months, no recent failures, and a 2+ year backtest of the same plan that agrees. It is information only: this app has no way to trade real money.

**Backtest lab.** Replay any plan over 1, 3, 5 or 10 years and watch it race the S&P 500 day by day, then see return, worst drop and Sharpe against the index. Results are saved in `~/.hedge-fund/research/` beside terminal backtests and feed the go-live check. Plans with AI analysts ask you to confirm the estimated number of paid AI calls first.

**FOMO desk.** Follows the top traders on [FOMO](https://fomo.family), the social crypto trading app, and paper-copies their trades as they make them. Paper money only: nothing here can sign a transaction or touch a wallet.

- *Finding them.* Every FOMO trade is signed on Solana by one FOMO wallet, so its trades are public. Every 3 hours the desk saves FOMO's trades per trader and token from [Bitquery](https://bitquery.io) (a free account; the desk uses ~40 of the 10,000 free points a day) and rebuilds the leaderboard: realized profit over 7 days at average cost, at least 5 closed positions, at least half of them winners.
- *Following them.* Every minute it reads the followed wallets' new transactions straight from Solana (free public data, or a free Helius/QuickNode address in `SOLANA_RPC_URL`) and prices tokens with DexScreener.
- *Copying.* A followed buy is copied at the price *now*, with FOMO's 0.5% fee and estimated slippage: 4% of the fund per copy, 6% when two top traders buy the same token within a day, skipping pools under $30k or younger than 10 minutes. A followed sell is mirrored in the same proportion. Stop-loss at -50%, 14-day time limit, a token with no price for a day is written off. The desk records the *lag cost* of each copy: how much more it paid than the trader. That number decides whether riding behind them can work.
- The FOMO desk screen shows the copy fund against simply holding SOL, open copies, the rebuilt leaderboard, and a live trade feed; copies and sells can ping your phone. `aihf fomo` runs one round from Task Scheduler.

Honest limits: most memecoins go to zero, followers always buy after the leader, and seven days of leaderboard is a small sample. FOMO hasn't published its signing wallet; Bitquery identified it from on-chain activity, so if FOMO changes it the desk stops finding new traders until it's updated.

**The team works together.** Every agent, on every desk, reports to one board, and a manager acts on what the whole team sees:

- *The FOMO council.* Before the FOMO desk copies a trade, eight agents vote. Copy Scout (who bought, their rank), Pool Guard (enough liquidity, not minutes old), Too-Late Check (vetoes if the price is already 15% above what the trader paid or up 60% in an hour), Crowd Count (more top traders buying = bigger bet), Track Record (benches traders whose copies have lost money), Market Shield (SOL down 8% in a day, *or the stock desk's S&P 500 shield is up*), FOMO Meter (FOMO buying 2.5x normal = a euphoric crowd = half size) and Hype Watch (Grok checks X for a coordinated pump; asked last, only if nobody vetoed). Any veto stops the copy; otherwise the manager sizes it from all the votes, between a quarter and double the normal slot. Every vote is saved with the trade.
- *Coordinated exits.* Take half off when a copy doubles; sell the rest if it falls 35% from its high (25% when the market is risk-off or the crowd euphoric); take half off if Hype Watch flags a held token; plus the stop-loss and time limit. Each outcome feeds the trader's track record, and losing traders drop off the follow list.
- *The Team room.* One screen for all of it: every desk's paper money combined, a live feed of every agent's messages to the manager, an agreement map per stock or token (who's for, who's against, who vetoed), the roster, and the **manager's briefing**: one AI call that reads the whole board and explains, in plain English, where the team agrees, where it's split, the risks and what to watch. The briefing is advice only; trading rules stay in code.

It serves this computer only (127.0.0.1) and needs no extra install. `--port 8080` to move it, `--no-browser` to skip opening a tab. Backtesting is still in the terminal app.

### Free data

Without a Financial Datasets key the app uses free daily prices from Yahoo Finance. That is all the price-based analysts below need. The AI investor personas read company financials, which Yahoo's free feed doesn't have, so on Yahoo they abstain; add a Financial Datasets key to bring them in. Force a source with `DATA_SOURCE=yahoo` or `DATA_SOURCE=financialdatasets` in `~/.hedge-fund/.env`. Yahoo prices are raw closes (dividends not included, for the fund and the benchmark alike) and come from an unofficial endpoint that can rate-limit.

### Fund plans

Four ready-made plans are copied into `~/.hedge-fund/mandates/` the first time you run this version. Run them side by side as separate paper funds and let the results decide:

| Plan | Analysts | Needs | Aims for |
| --- | --- | --- | --- |
| `trend-shield` | Trend (200-day average) on 5 asset classes: SPY, EFA, IEF, VNQ, GLD | nothing | Smaller drops; lags in strong bull markets |
| `momentum-leaders` | Momentum ×2, Trend, Low volatility on 12 large caps, plus a market filter that cuts to 30% invested when SPY is below its 200-day average | nothing | Beat SPY with smaller drops |
| `ai-quality-check` | Momentum Leaders on 70% of the money, Buffett and Munger on 30% | Financial Datasets key + an AI key | Tests whether the AI adds anything |
| `copy-desk` | Insider, fund, congress and hype trackers on 40%, Momentum Leaders on 60% (see below) | Your name and email for the SEC; xAI key optional | Tests whether copying the smart money adds anything |

New building blocks: price-only analysts `trend`, `momentum` and `low_vol` (no AI, no fundamentals); `blend: {method: slot_weighted}`, which gives each ticker an equal slot and leaves unused slots in cash; and `risk.market_filter: {ma_days, risk_off_gross}`. The two stock plans also carry `risk.pump_guard`: any stock that jumps 25% in a day or 40% in a week on 4× its normal volume is treated as a likely pump, and the fund stays out of it (selling if it holds it) for 10 trading days. Each alarm shows in the dashboard's battle log. It reads price and volume only; social-media buzz isn't part of the check yet. None of these patterns is a promise: published market patterns have lost about a third of their strength after publication on average, and momentum has crashed hard after market bottoms. Backtest first with `aihf backtest ~/.hedge-fund/mandates/momentum-leaders.yaml --universe AAPL,MSFT,...`.

### Copy desk: following the smart money

The `copy-desk` plan puts 40% of the money behind four trackers that read trades people are required to publish, and 60% behind the Momentum Leaders signals:

| Tracker | Watches | Source | Needs |
| --- | --- | --- | --- |
| `insiders` (Insider Tracker) | Open-market buys by a company's officers and directors; strongest when several buy together or the CEO/CFO buys | SEC Form 4, public within 2 business days | Your name and email for the SEC (free) |
| `superinvestors` (Fund Tracker) | What Berkshire, Pershing Square, Appaloosa, Baupost and Duquesne own, how big a bet it is, and whether they added | SEC 13F, up to 4.5 months old | Same |
| `congress` (Capitol Tracker) | Stock buys disclosed by members of Congress (half weight: the evidence is mixed) | [Bargo](https://www.bargo.ai) free feed, last 3 months only | Nothing; a free Bargo key raises the daily limit |
| `social_watch` (Hype Watch) | Asks Grok to search X for pump chatter. It can only vote **against** a stock, never for it | xAI live X search | An xAI key (a few cents per stock per day); sits out without one |

Every tracker reads a filing by the day it became public, never the day the trade happened, so backtests can't peek. Each abstains when there is nothing to copy, and a stock nobody is buying keeps its slot in cash. Sales are ignored: insiders and politicians sell for many reasons, and the research finds sales say little. The SEC asks every program for a contact name and email (`SEC_USER_AGENT="Jane Smith jane@example.com"`); add it under **Keys & model**. It is only sent to the SEC. Filings are cached in `~/.hedge-fund/cache/`. Edit the fund list with `params: {funds: {Name: CIK}}` on `superinvestors`.

What to expect, honestly: insider buying has historically beaten the market by about 0.4% a month on average, more for cluster and CEO buys, but it is widely known and has weakened. Copying funds' 13F "best ideas" has helped on average, with stale data. Congressional trading showed an edge in early studies and little after the 2012 STOCK Act. Hype Watch is a brake, not an engine. Paper-trade it next to `momentum-leaders` and keep it only if it earns its place. Hype Watch and the congress feed only cover recent days, so backtests test the insider and fund trackers alone.



## Development

```bash
git clone https://github.com/virattt/ai-hedge-fund.git
cd ai-hedge-fund
poetry install
poetry run aihf
poetry run pytest hedge_fund
```

## How to Contribute

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

**Important**: Please keep your pull requests small and focused. This will make it easier to review and merge.

## Feature Requests

If you have a feature request, please open an [issue](https://github.com/virattt/ai-hedge-fund/issues) and make sure it is tagged with `enhancement`.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
