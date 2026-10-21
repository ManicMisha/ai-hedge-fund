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
