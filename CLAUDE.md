# Working on the FOMO desk

The desk lives in `desk/`, `prompts/`, `config/` and `docs/team-design.md`. The older `hedge_fund/` app is a separate project; leave it alone unless asked.

## The specification

- `docs/team-design.md` is the specification: five agents, how they hand off, 32 parameters, and each agent's system prompt.
- If a request and that document disagree, or the document is ambiguous, stop and ask the owner. Do not resolve it yourself.
- `prompts/` holds each agent's prompt word for word from that document. Never edit a prompt to make code or a test pass. `desk/tests/test_design_doc.py` checks them against the document.

## Rules that do not bend

- **Paper trading only.** No live-trading adapter, no exchange or wallet keys, and no code path that can place a real order. Live trading would be a separate change the owner asks for explicitly.
- **The risk rules matter more than the signals.** Build so the rules cannot be skipped, including by the owner in a hurry.
- **Never weaken a risk rule, a test or a check to make something pass.** If a rule looks wrong, tell the owner and leave it in place.
- **Never invent a parameter value.** `config/desk.yaml` starts with all 32 empty. Ask the owner. Code supplies no defaults.
- **Fail closed.** Missing data, stale data, an empty or invalid parameter: no trade, and name what is missing.
- **Limits change in configuration, not in conversation.** No code path waives a limit for one trade.
- **Only the Execution agent may reach the broker.** The Scout, the Analyst and the Risk Officer are read-only, enforced by tool allowlists where possible and in code where not.
- **The Risk agent can never approve what the risk engine vetoes.**
- **Every approval needs the owner.** A ticket executes only after the repository owner adds the `approved` label to its GitHub issue, the ticket has not expired, and checks A to D pass. This applies in paper mode too.
- **Data sources are read-only.** The desk never posts, replies, likes or promotes anything.
- **Labels.** Every agent output is labelled ANALYSIS or PROPOSED ACTION. Nothing in code, prompts or docs presents results as guaranteed or as financial advice. Every result figure is labelled simulated.
- **Nothing runs or spends money unattended** (schedules, GitHub Actions, cron) without asking the owner first.
- **Never commit secrets.** Keys go in `.env`, which is git-ignored; `.env.example` lists names only.

## How to work

- Build in stages, one pull request per stage, and run the tests before each one: `make desk-test`.
- Report plainly what is verified, what is stubbed and what could not be tested.

## Commands

- `python -m desk check`: which settings are filled in, which agents are waiting.
- `python -m desk render`: fill the prompts; refuses while anything is empty.
- `python -m desk web`: the desk page at http://localhost:8787.
