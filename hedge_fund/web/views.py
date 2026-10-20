"""Read paper funds off disk into the shapes the web dashboard draws.

Everything here is a read of what the engine already wrote — the ledger,
the kill switch, the events log, the mandates — turned into plain dicts the
browser can render. Nothing here trades or writes; that is server.py's job,
and it goes through the same `tick` / `redo` / `Ledger.halt` the CLI uses.
"""

from __future__ import annotations

import os
from pathlib import Path

from hedge_fund.data.source import data_source, LABELS as SOURCE_LABELS, needs_data_key
from hedge_fund.fund import FundSpec, load_spec
from hedge_fund.llm import DEFAULT_MODEL, load_api_models, PROVIDER_ENV_VARS, provider_for
from hedge_fund.models import Signal
from hedge_fund.paper import Ledger, list_deployed, load_deployed
from hedge_fund.pipeline.models import DecisionRecord, StrategyRecord
from hedge_fund.pipeline.session import SessionRecord
from hedge_fund.signals import ALPHA_MODEL_REGISTRY, LLMAgent

DATA_KEY = "FINANCIAL_DATASETS_API_KEY"
MODEL_VAR = "HEDGE_FUND_LLM_MODEL"

# How each analyst is introduced on screen. The personas are stylized
# approximations of public investment philosophies (see VISION.md), not the
# people themselves.
ANALYSTS = {
    "buffett": ("Warren Buffett", "Quality at a fair price"),
    "munger": ("Charlie Munger", "Great businesses, few mistakes"),
    "graham": ("Benjamin Graham", "Margin of safety"),
    "lynch": ("Peter Lynch", "Growth you can understand"),
    "druckenmiller": ("Stanley Druckenmiller", "Macro and momentum"),
    "pead": ("Earnings Drift", "Maths only: drift after earnings surprises"),
    "trend": ("Trend Scout", "Maths only: above its 200-day average or out"),
    "momentum": ("Momentum Runner", "Maths only: last year's winners keep running"),
    "low_vol": ("Steady Hand", "Maths only: calmer stocks, smaller drops"),
    "insiders": ("Insider Tracker", "Copies company bosses buying their own stock"),
    "superinvestors": ("Fund Tracker", "Copies famous funds' biggest bets (13F)"),
    "congress": ("Capitol Tracker", "Copies stock buys disclosed by Congress"),
    "social_watch": ("Hype Watch", "Warns when X looks like a pump. Never buys"),
}

SEC_VAR = "SEC_USER_AGENT"
BARGO_VAR = "BARGO_API_KEY"
XAI_VAR = "XAI_API_KEY"

_LOG_LIMIT = 200


# ---------------------------------------------------------------------------
# App-level state: keys, model, mandates, the fund list
# ---------------------------------------------------------------------------

def app_state(paper_root: Path, mandates_root: Path) -> dict:
    return {
        "data_key_set": bool(os.environ.get(DATA_KEY)),
        "data_source": data_source(),
        "data_label": SOURCE_LABELS[data_source()],
        "data_ready": not needs_data_key(),
        "sec_set": bool(os.environ.get(SEC_VAR, "").strip()),
        "bargo_set": bool(os.environ.get(BARGO_VAR, "").strip()),
        "xai_set": bool(os.environ.get(XAI_VAR, "").strip()),
        "model": current_model(),
        "models": [_model_view(mid, label, provider) for label, mid, provider in load_api_models()],
        "mandates": mandates(mandates_root),
        "funds": [fund_summary(d) for d in list_deployed(paper_root)],
    }


def current_model() -> dict:
    model_id = os.environ.get(MODEL_VAR) or DEFAULT_MODEL
    label = next((lbl for lbl, mid, _ in load_api_models() if mid == model_id), model_id)
    return _model_view(model_id, label, provider_for(model_id))


def _model_view(model_id: str, label: str, provider: str | None) -> dict:
    env_var = PROVIDER_ENV_VARS.get(provider) if provider else None
    return {
        "id": model_id,
        "label": label,
        "provider": provider,
        "key_env": env_var,
        "key_set": bool(env_var and os.environ.get(env_var)),
    }


def allowed_key_vars() -> set[str]:
    """The only environment variables the dashboard may write."""
    return {DATA_KEY, SEC_VAR, BARGO_VAR, *PROVIDER_ENV_VARS.values()}


def mandates(root: Path) -> list[dict]:
    out = []
    for path in sorted(Path(root).glob("*.yaml")):
        try:
            spec = load_spec(path)
        except ValueError as exc:
            out.append({"file": path.stem, "error": str(exc)})
            continue
        out.append({"file": path.stem, **spec_view(spec)})
    return out


def spec_view(spec: FundSpec) -> dict:
    return {
        "name": spec.name,
        "capital": spec.capital,
        "rebalance": spec.rebalance,
        "benchmark": spec.benchmark,
        "max_position_pct": spec.risk.max_position_pct,
        "strategies": [
            {
                "name": s.name,
                "mode": s.blend.mode,
                "method": s.blend.method,
                "weight": s.weight,
                "analysts": [analyst_view(m.name) for m in s.models],
            }
            for s in spec.strategies
        ],
    }


def analyst_view(name: str) -> dict:
    display, motto = ANALYSTS.get(name, (name.replace("_", " ").title(), ""))
    cls = ALPHA_MODEL_REGISTRY.get(name)
    is_llm = isinstance(cls, type) and issubclass(cls, LLMAgent)
    kind = "ai" if is_llm else getattr(cls, "kind", "quant")
    return {"id": name, "name": display, "motto": motto, "kind": kind,
            "needs": list(getattr(cls, "required_env", ()))}


# ---------------------------------------------------------------------------
# One fund
# ---------------------------------------------------------------------------

def fund_summary(directory: Path) -> dict:
    directory = Path(directory)
    try:
        deployed = load_deployed(directory)
        ledger = Ledger(directory)
        sessions = ledger.sessions()
        first = ledger.read(sessions[0]) if sessions else None
        latest = ledger.read(sessions[-1]) if sessions else None
        halted = ledger.halted()
    except Exception as exc:  # one broken fund must not hide the others
        return {"name": directory.name, "error": f"{type(exc).__name__}: {exc}"}
    capital = deployed.spec.capital
    nav = latest.nav if latest else capital
    return {
        "name": deployed.name,
        "mandate": deployed.spec.name,
        "universe": deployed.universe,
        "capital": capital,
        "rebalance": deployed.spec.rebalance,
        "benchmark": deployed.spec.benchmark,
        "created": deployed.created,
        "sessions": len(sessions),
        "last_session": latest.session if latest else None,
        "nav": nav,
        "return_pct": nav / capital - 1,
        "benchmark_return_pct": _bench_return(first, latest),
        "halted": halted,
        "pending": latest is not None and latest.decision is not None,
    }


def fund_detail(directory: Path) -> dict:
    directory = Path(directory)
    deployed = load_deployed(directory)
    ledger = Ledger(directory)
    records = ledger.records()
    spec = deployed.spec
    capital = spec.capital
    latest = records[-1] if records else None
    navs = [r.nav for r in records]

    return {
        **fund_summary(directory),
        "spec": spec_view(spec),
        "llm_model": latest.llm_model if latest else None,
        "series": [
            {"session": r.session, "nav": r.nav, "benchmark_close": r.benchmark_close,
             "rebalance": r.rebalance, "traded": bool(r.executed and r.executed.fills)}
            for r in records
        ],
        "max_drawdown_pct": _max_drawdown([capital, *navs]),
        "cash": latest.cash if latest else capital,
        "holdings": _holdings(latest),
        "view": _latest_view(records),
        "pending": _decision_targets(latest.decision) if latest and latest.decision else None,
        "log": _log(records, ledger.events()),
    }


def _bench_return(first: SessionRecord | None, latest: SessionRecord | None) -> float | None:
    if first is None or latest is None or not first.benchmark_close:
        return None
    return latest.benchmark_close / first.benchmark_close - 1


def _max_drawdown(navs: list[float]) -> float:
    peak, worst = navs[0], 0.0
    for nav in navs:
        peak = max(peak, nav)
        if peak > 0:
            worst = min(worst, nav / peak - 1)
    return worst


def _holdings(latest: SessionRecord | None) -> list[dict]:
    if latest is None:
        return []
    rows = []
    for ticker, shares in sorted(latest.positions.items()):
        if not shares:
            continue
        mark = latest.marks.get(ticker)
        value = shares * mark if mark is not None else None
        rows.append({
            "ticker": ticker, "shares": shares, "mark": mark, "value": value,
            "weight": value / latest.nav if value is not None and latest.nav else None,
        })
    return sorted(rows, key=lambda r: -abs(r["value"] or 0))


def _latest_view(records: list[SessionRecord]) -> dict | None:
    """The analysts' most recent opinions: the decision waiting to execute if
    there is one, else the last decision that was executed."""
    for record in reversed(records):
        if record.decision is not None:
            return _decision_view(record.decision, "pending")
        if record.executed is not None:
            assessed = record.executed.original_assessment
            if assessed is not None:
                return _decision_view(assessed, "executed")
            return {
                "as_of": record.executed.as_of, "status": "executed",
                "signals": _signals(record.executed.strategies),
                "targets": record.executed.final_weights,
                "clamps": [c.model_dump() for c in record.executed.clamps],
            }
    return None


def _decision_view(decision: DecisionRecord, status: str) -> dict:
    return {
        "as_of": decision.as_of,
        "status": status,
        "signals": _signals(decision.strategies),
        "targets": decision.final_weights,
        "clamps": [c.model_dump() for c in decision.clamps],
    }


def _decision_targets(decision: DecisionRecord) -> dict:
    return {"as_of": decision.as_of, "targets": decision.final_weights,
            "clamps": [c.model_dump() for c in decision.clamps]}


def _signals(strategies: list[StrategyRecord]) -> list[dict]:
    return [signal_view(sig, sr.name) for sr in strategies for sig in sr.signals]


def signal_view(sig: Signal, strategy: str) -> dict:
    meta = sig.metadata or {}
    abstained = bool(meta.get("abstained"))
    stance = meta.get("signal")
    if stance not in ("bullish", "bearish", "neutral"):
        stance = "bullish" if sig.value > 0.05 else "bearish" if sig.value < -0.05 else "neutral"
    confidence = meta.get("confidence")
    if confidence is None:
        confidence = round(abs(sig.value) * 100)
    return {
        "analyst": sig.model_name,
        "strategy": strategy,
        "ticker": sig.ticker,
        "stance": "abstain" if abstained else stance,
        "confidence": confidence,
        "value": sig.value,
        "reasoning": sig.reasoning or "",
    }


def _log(records: list[SessionRecord], events: list[dict]) -> list[dict]:
    """The battle log: every fill, every risk trim, every decision, and the
    fund's own events (halts, failures, re-runs). Newest first."""
    entries: list[dict] = []
    for r in records:
        if r.executed is not None:
            for fill in r.executed.fills:
                entries.append({"at": r.session, "kind": "trade", "side": fill.side,
                                "ticker": fill.ticker, "quantity": fill.quantity,
                                "price": fill.price, "value": fill.quantity * fill.price})
            for clamp in r.executed.clamps:
                entries.append({"at": r.session, "kind": "clamp", **clamp.model_dump()})
        if r.decision is not None:
            entries.append({"at": r.session, "kind": "decision",
                            "targets": r.decision.final_weights})
    for event in events:
        if event.get("kind") == "tick":
            continue  # every session already shows up through its record
        entries.append({"at": str(event.get("time", ""))[:10], "time": event.get("time"),
                        "kind": event.get("kind", "event"),
                        "detail": event.get("reason") or event.get("error") or event.get("session")})
    # Newest first; on a tie, the entry written later comes first.
    order = sorted(enumerate(entries), key=lambda p: (p[1]["at"], p[1].get("time") or "", p[0]), reverse=True)
    return [e for _, e in order][:_LOG_LIMIT]
