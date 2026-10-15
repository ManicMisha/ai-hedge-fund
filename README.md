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

It serves this computer only (127.0.0.1) and needs no extra install. `--port 8080` to move it, `--no-browser` to skip opening a tab. Backtesting is still in the terminal app.

### Free data

Without a Financial Datasets key the app uses free daily prices from Yahoo Finance. That is all the price-based analysts below need. The AI investor personas read company financials, which Yahoo's free feed doesn't have, so on Yahoo they abstain; add a Financial Datasets key to bring them in. Force a source with `DATA_SOURCE=yahoo` or `DATA_SOURCE=financialdatasets` in `~/.hedge-fund/.env`. Yahoo prices are raw closes (dividends not included, for the fund and the benchmark alike) and come from an unofficial endpoint that can rate-limit.

### Fund plans

Three ready-made plans are copied into `~/.hedge-fund/mandates/` the first time you run this version. Run them side by side as separate paper funds and let the results decide:

| Plan | Analysts | Needs | Aims for |
| --- | --- | --- | --- |
| `trend-shield` | Trend (200-day average) on 5 asset classes: SPY, EFA, IEF, VNQ, GLD | nothing | Smaller drops; lags in strong bull markets |
| `momentum-leaders` | Momentum ×2, Trend, Low volatility on 12 large caps, plus a market filter that cuts to 30% invested when SPY is below its 200-day average | nothing | Beat SPY with smaller drops |
| `ai-quality-check` | Momentum Leaders on 70% of the money, Buffett and Munger on 30% | Financial Datasets key + an AI key | Tests whether the AI adds anything |

New building blocks: price-only analysts `trend`, `momentum` and `low_vol` (no AI, no fundamentals); `blend: {method: slot_weighted}`, which gives each ticker an equal slot and leaves unused slots in cash; and `risk.market_filter: {ma_days, risk_off_gross}`. None of these patterns is a promise: published market patterns have lost about a third of their strength after publication on average, and momentum has crashed hard after market bottoms. Backtest first with `aihf backtest ~/.hedge-fund/mandates/momentum-leaders.yaml --universe AAPL,MSFT,...`.



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
