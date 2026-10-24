"""FOMO desk: parsing, the rebuilt leaderboard, and the paper copy rules. No network."""

from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from hedge_fund.fomo import leaderboard as lb
from hedge_fund.fomo.desk import Desk, event_alert, FEE
from hedge_fund.fomo.sources import (
    Bitquery, DexScreener, parse_swaps, Quote, SetupError, SOL_MINT, SourceError, Swap, USDC_MINT, WindowRow,
)

W = "Wa11etTraderAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
W2 = "Wa11etTraderBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2"
BONK = "BonkMintXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
WIF = "WifMintYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY"


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

def _tx(wallet, token_pre, token_post, lamports_pre, lamports_post, mint=BONK, extra=None):
    bal = lambda amount, m=mint: {"owner": wallet, "mint": m, "uiTokenAmount": {"uiAmount": amount}}
    return {"blockTime": 1_760_000_000, "meta": {
        "err": None, "preBalances": [lamports_pre, 5], "postBalances": [lamports_post, 5],
        "preTokenBalances": [bal(token_pre)] + (extra or [])[:1],
        "postTokenBalances": [bal(token_post)] + (extra or [])[1:]},
        "transaction": {"message": {"accountKeys": [{"pubkey": wallet}, {"pubkey": "other"}]}}}


def test_parse_swaps_reads_buys_and_sells_from_balance_changes():
    [buy] = parse_swaps(_tx(W, 0, 1_000_000, 5_000_000_000, 3_000_000_000), W, "sig1")
    assert (buy.side, buy.mint, buy.amount, buy.cash_mint, buy.cash) == ("buy", BONK, 1_000_000, SOL_MINT, 2.0)
    usdc = [{"owner": W, "mint": USDC_MINT, "uiTokenAmount": {"uiAmount": 10}},
            {"owner": W, "mint": USDC_MINT, "uiTokenAmount": {"uiAmount": 410}}]
    [sell] = parse_swaps(_tx(W, 1_000_000, 0, 1, 1, extra=usdc), W, "sig2")
    assert (sell.side, sell.cash_mint, sell.cash) == ("sell", USDC_MINT, 400)
    assert parse_swaps(_tx(W, 0, 500, 1_000_000_000, 999_995_000), W, "fee-only") == []  # an airdrop, no payment
    assert parse_swaps(_tx(W, 0, 500, 5_000_000_000, 3_000_000_000) | {"meta": {"err": "x"}}, W, "failed") == []


def test_bitquery_window_needs_a_token_and_parses_rows(monkeypatch):
    monkeypatch.delenv("BITQUERY_TOKEN", raising=False)
    with pytest.raises(SetupError):
        Bitquery()
    sent = {}

    class Http:
        def post(self, url, json, headers, timeout):
            sent.update(url=url, json=json, headers=headers)
            return Resp(200, {"data": {"Solana": {"DEXTradeByTokens": [
                {"Trade": {"Account": {"Owner": W}, "Currency": {"MintAddress": BONK, "Symbol": "BONK"}},
                 "bought_usd": "120.5", "sold_usd": "0", "bought": "1000", "sold": None, "trades": "3"},
                {"Trade": {"Account": {"Owner": ""}, "Currency": {"MintAddress": BONK}}}]}}})
    rows = Bitquery("tok", Http()).window("2026-10-23T00:00:00Z", "2026-10-23T03:00:00Z")
    assert rows == [WindowRow(W, BONK, "BONK", 120.5, 0.0, 1000.0, 0.0, 3)]
    assert sent["headers"]["Authorization"] == "Bearer tok" and "AgmLJBMD" in sent["json"]["query"]
    assert sent["json"]["variables"]["since"] == "2026-10-23T00:00:00Z"

    class Broken:
        def post(self, *a, **k):
            return Resp(200, {"errors": [{"message": "not enough points"}]})
    with pytest.raises(SourceError, match="points"):
        Bitquery("tok", Broken()).window("a", "b")


def test_dexscreener_picks_the_deepest_pool_and_batches_by_30():
    calls = []

    class Http:
        def get(self, url, timeout):
            calls.append(url)
            mints = url.rsplit("/", 1)[1].split(",")
            pairs = [{"baseToken": {"address": m, "symbol": "T"}, "priceUsd": "1.0", "liquidity": {"usd": 1000}} for m in mints]
            pairs.append({"baseToken": {"address": mints[0], "symbol": "T"}, "priceUsd": "2.0", "liquidity": {"usd": 9000}})
            return Resp(200, pairs)
    mints = [f"M{i:02d}" for i in range(31)]
    quotes = DexScreener(Http()).quotes(mints)
    assert len(calls) == 2 and len(quotes) == 31 and quotes["M00"].price == 2.0 and quotes["M01"].price == 1.0


# ---------------------------------------------------------------------------
# leaderboard
# ---------------------------------------------------------------------------

def _store(tmp_path):
    return lb.Store(tmp_path / "f.sqlite")


def test_realized_profit_uses_average_cost_and_ignores_unknown_cost(tmp_path):
    store = _store(tmp_path)
    t0, t1 = "2026-10-22T00:00:00Z", "2026-10-22T03:00:00Z"
    store.save_window(t0, t1, [WindowRow(W, BONK, "BONK", 100, 0, 1000, 0, 2),
                               WindowRow(W, WIF, "WIF", 0, 500, 0, 50, 1)])     # sold WIF bought before we watched
    store.save_window(t1, "2026-10-22T06:00:00Z", [WindowRow(W, BONK, "BONK", 100, 300, 1000, 1000, 3)])
    [row] = lb.leaderboard(store, datetime(2026, 10, 23, tzinfo=timezone.utc))
    # 2000 bought for $200 (avg $0.10), 1000 sold for $300 -> +$200 realized; WIF has no known cost.
    assert row["realized"] == 200 and row["closed"] == 0 and row["open"] == 1
    assert lb.leaderboard(store, datetime(2026, 11, 30, tzinfo=timezone.utc)) == []  # outside 7 days


def test_eligibility_needs_enough_closed_winning_positions(tmp_path):
    store = _store(tmp_path)
    rows = []
    for i in range(6):  # W: 5 wins, 1 loss, all closed
        sold_usd = 300 if i < 5 else 50
        rows.append(WindowRow(W, f"M{i}", f"T{i}", 100, sold_usd, 10, 10, 2))
    rows.append(WindowRow(W2, "LUCKY", "LUCK", 100, 5000, 10, 10, 2))  # one lucky trade
    store.save_window("2026-10-22T00:00:00Z", "2026-10-22T03:00:00Z", rows)
    board = lb.leaderboard(store, datetime(2026, 10, 23, tzinfo=timezone.utc))
    assert [r["wallet"] for r in board] == [W, W2]  # eligible first, even though W2 made more
    assert board[0]["eligible"] and board[0]["closed"] == 6 and board[0]["win_rate"] == pytest.approx(5 / 6)
    assert not board[1]["eligible"] and board[1]["best"]["symbol"] == "LUCK"


def test_due_windows_are_aligned_and_skip_saved_ones(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc)
    due = lb.due_windows(store, now)
    assert due == [("2026-10-22T21:00:00Z", "2026-10-23T00:00:00Z"), ("2026-10-23T00:00:00Z", "2026-10-23T03:00:00Z"),
                   ("2026-10-23T03:00:00Z", "2026-10-23T06:00:00Z")]
    store.save_window(*due[1], [])
    assert len(lb.due_windows(store, now)) == 2


# ---------------------------------------------------------------------------
# the paper copy desk
# ---------------------------------------------------------------------------

class FakeRPC:
    def __init__(self):
        self.sigs, self.swaps_by_sig = {}, {}

    def new_signatures(self, wallet, until, limit=25):
        sigs = self.sigs.get(wallet, [])
        if until is None:
            return [{"signature": s} for s in sigs[-limit:]]
        idx = sigs.index(until) if until in sigs else -1
        return [{"signature": s} for s in sigs[idx + 1:]]

    def swaps(self, wallet, signature):
        return self.swaps_by_sig.get(signature, [])


class FakeDex:
    def __init__(self):
        self.book = {SOL_MINT: Quote(SOL_MINT, "SOL", 200.0, 1e9, 0)}

    def quotes(self, mints):
        return {m: self.book[m] for m in mints if m in self.book}


class Clock:
    def __init__(self, t=1_760_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def desk(tmp_path):
    rpc, dex, clock = FakeRPC(), FakeDex(), Clock()
    d = Desk(tmp_path, {"capital": 1000.0}, rpc=rpc, dex=dex, bitquery=lambda: NS(window=lambda s, t: []), clock=clock)
    d.state["followed"] = [W, W2]
    d.poll()  # first look: cursors set, nothing copied
    d.rpc_, d.dex_, d.clock_ = rpc, dex, clock
    return d


def _trade(d, wallet, sig, side, mint, amount, sol, t_offset=-20):
    d.rpc_.sigs.setdefault(wallet, []).append(sig)
    d.rpc_.swaps_by_sig[sig] = [Swap(wallet, sig, int(d.clock_.t + t_offset), mint, side, amount, SOL_MINT, sol)]


def test_a_followed_buy_is_copied_with_fees_slippage_and_lag(desk):
    desk.dex_.book[BONK] = Quote(BONK, "BONK", 0.00011, 200_000.0, 0)
    _trade(desk, W, "s1", "buy", BONK, 1_000_000, 0.5)  # they paid 0.5 SOL = $100 -> $0.0001 each
    [event] = desk.poll()
    # 4% of $1,000, x1.25 because Copy Scout backs the #1 trader, x0.75 because
    # Too-Late Check sees the price 10% above what they paid.
    assert event["kind"] == "buy" and event["usd"] == pytest.approx(37.5) and event["mult"] == 0.94
    pos = desk.state["positions"][BONK]
    slip = 0.003 + 37.5 / 200_000
    assert pos["entry"] == pytest.approx(0.00011 * (1 + slip))
    assert pos["qty"] == pytest.approx((37.5 - 37.5 * FEE) / pos["entry"])
    assert {v["agent"] for v in event["votes"]} >= {"Copy Scout", "Pool Guard", "Too-Late Check", "Market Shield"}
    assert event["lag"] == pytest.approx(pos["entry"] / 0.0001 - 1) and event["delay"] == 20
    assert desk.state["cash"] == pytest.approx(962.5)
    assert "Copied" in event["text"] and event_alert(event)["title"] == "FOMO copy: bought BONK"


def test_skips_small_pools_and_sizes_up_when_two_top_traders_buy(desk):
    desk.dex_.book[WIF] = Quote(WIF, "WIF", 1.0, 5_000.0, 0)
    _trade(desk, W, "s1", "buy", WIF, 100, 1.0)
    assert desk.poll() == [] and desk.state["events"][0]["kind"] == "skip" and "Pool Guard" in desk.state["events"][0]["text"]
    desk.dex_.book[WIF] = Quote(WIF, "WIF", 1.0, 500_000.0, 0)
    _trade(desk, W2, "s2", "buy", WIF, 100, 1.0)
    [event] = desk.poll()
    assert event["cluster"] is True and event["usd"] == pytest.approx(75.0)  # 4% x 1.5 crowd x 1.25 top-3 trader


def test_mirrors_their_sells_and_runs_the_stop_loss(desk):
    desk.dex_.book[BONK] = Quote(BONK, "BONK", 0.0001, 200_000.0, 0)
    _trade(desk, W, "s1", "buy", BONK, 1_000_000, 0.5)
    desk.poll()
    qty = desk.state["positions"][BONK]["qty"]
    _trade(desk, W, "s2", "sell", BONK, 500_000, 0.6)  # they sell half
    [half] = desk.poll()
    assert half["share"] == pytest.approx(0.5) and desk.state["positions"][BONK]["qty"] == pytest.approx(qty / 2)
    desk.dex_.book[BONK] = Quote(BONK, "BONK", 0.00004, 200_000.0, 0)  # -60%
    [stop] = desk.poll()
    assert "stop-loss" in stop["text"] and BONK not in desk.state["positions"] and stop["profit"] < 0
    assert event_alert(stop)["level"] == "bad"


def test_a_token_with_no_price_for_a_day_is_written_off(desk):
    desk.dex_.book[BONK] = Quote(BONK, "BONK", 0.0001, 200_000.0, 0)
    _trade(desk, W, "s1", "buy", BONK, 1_000_000, 0.5)
    desk.poll()
    del desk.dex_.book[BONK]
    assert desk.poll() == []
    desk.clock_.t += 90_000
    [gone] = desk.poll()
    assert gone["usd"] == 0 and gone["profit"] == pytest.approx(-50.0) and "written off" in gone["text"]
    assert BONK not in desk.state["positions"]


def test_unfollowed_buys_are_ignored_but_status_reports_everything(desk):
    stranger = "StrangerZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"
    desk.state["cursors"][stranger] = ""
    desk.dex_.book[BONK] = Quote(BONK, "BONK", 0.0001, 200_000.0, 0)
    _trade(desk, stranger, "x1", "buy", BONK, 1_000_000, 0.5)
    assert desk.poll() == []
    st = desk.status()
    assert st["nav"] == pytest.approx(1000.0) and st["sol_return_pct"] == pytest.approx(0.0)
    assert {"leaderboard", "positions", "events", "coverage", "errors", "avg_lag"} <= set(st)


def test_collect_follows_the_top_eligible_wallets(tmp_path):
    rows = [WindowRow(W, f"M{i}", f"T{i}", 100, 300, 10, 10, 2) for i in range(5)]

    class Q:
        def window(self, since, till):
            return rows if since == "2026-10-23T03:00:00Z" else []
    d = Desk(tmp_path, {"follow": 3}, rpc=FakeRPC(), dex=FakeDex(), bitquery=lambda: Q(), clock=Clock())
    d.collect(datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc))
    assert d.state["followed"] == [W] and d.state["events"][0]["kind"] == "follow"

    def missing():
        raise SetupError("Add a Bitquery token")
    d2 = Desk(tmp_path / "two", rpc=FakeRPC(), dex=FakeDex(), bitquery=missing, clock=Clock())
    assert d2.collect() == 0 and "Bitquery" in d2.status()["errors"]["bitquery"]["message"]


# ---------------------------------------------------------------------------
# the council and the exits
# ---------------------------------------------------------------------------

from hedge_fund.fomo.council import buy_council, exit_checks, Situation


def _sit(**kw):
    base = dict(symbol="BONK", wallet_label="7xKX…9fQa", usd=200, rank=4, their_price=1.0, price=1.02,
                liquidity=200_000, pool_age_s=86_400, change_h1=0.05, holders_buying=1, record={})
    return Situation(**{**base, **kw})


def test_council_vetoes_late_chases_tiny_pools_and_benched_traders():
    for kw, agent in [({"price": 1.2}, "Too-Late Check"), ({"change_h1": 0.9}, "Too-Late Check"),
                      ({"liquidity": 4_000}, "Pool Guard"), ({"pool_age_s": 60}, "Pool Guard"),
                      ({"record": {"closed": 4, "wins": 1, "pnl": -30}}, "Track Record")]:
        approved, _, votes = buy_council(_sit(**kw))
        assert not approved and next(v for v in votes if v.verdict == "veto").agent == agent


def test_council_sizes_from_every_agent():
    ok, mult, _ = buy_council(_sit())
    assert ok and mult == 1.0
    _, mult, _ = buy_council(_sit(rank=0, holders_buying=3, record={"closed": 5, "wins": 4, "pnl": 80}))
    assert mult == 2.0  # 1.25 x 1.5 x 1.25 = 2.34, capped at double
    _, mult, _ = buy_council(_sit(risk_off=["S&P 500 below its 200-day average"], heat=3.0))
    assert mult == 0.25  # half for risk-off, half for a euphoric crowd
    _, mult, _ = buy_council(_sit(price=1.1))
    assert mult == 0.75


def test_social_scout_is_asked_last_and_only_when_needed():
    asked = []
    scout = lambda: asked.append(1) or {"pump_risk": 0.8, "sentiment": 0.5, "red_flags": [], "green_flags": []}
    approved, _, votes = buy_council(_sit(), hype=scout)
    assert not approved and votes[-1].agent == "Social Scout" and asked == [1]
    buy_council(_sit(liquidity=10), hype=scout)
    assert asked == [1]  # someone already vetoed: don't pay for a search
    approved, mult, _ = buy_council(_sit(), hype=lambda: {"pump_risk": 0.5, "sentiment": 0.0})
    assert approved and mult == 0.5
    rug = {"pump_risk": 0.1, "sentiment": 0.3, "red_flags": ["devs dumped, possible rug"], "green_flags": []}
    assert not buy_council(_sit(), hype=lambda: rug)[0]
    assert not buy_council(_sit(), hype=lambda: {"pump_risk": 0.1, "sentiment": -0.6})[0]
    ok, mult, _ = buy_council(_sit(), hype=lambda: {"pump_risk": 0.1, "sentiment": 0.6, "green_flags": ["CEX listing"]})
    assert ok and mult == 1.1


def test_trailing_stop_and_checkpoints():
    from hedge_fund.fomo.council import next_checkpoint
    pos = {"entry": 1.0, "peak": 2.1}
    assert exit_checks(pos, 1.5, tight=False) is None           # 29% off the high: hold
    assert exit_checks(pos, 1.3, tight=False)[0] == 1.0         # 38% off: out
    assert exit_checks(pos, 1.5, tight=True)[0] == 1.0          # risk-off: 25% trail
    assert exit_checks({"entry": 1.0, "peak": 1.2}, 0.8, tight=False) is None  # never ran up: stop-loss's job
    assert next_checkpoint({"checkpoints": []}, 0.3) is None
    assert next_checkpoint({"checkpoints": []}, 0.6) == 0.5
    assert next_checkpoint({"checkpoints": [0.5]}, 1.3) == 1.0 and next_checkpoint({"checkpoints": [0.5, 1.0]}, 1.3) is None


def test_desk_benches_traders_whose_copies_lose(desk):
    for i in range(3):
        mint = f"Loser{i}" + "Z" * 36
        desk.dex_.book[mint] = Quote(mint, f"L{i}", 1.0, 500_000.0, 0)
        _trade(desk, W, f"b{i}", "buy", mint, 50, 0.25)
        desk.poll()
        desk.dex_.book[mint] = Quote(mint, f"L{i}", 0.4, 500_000.0, 0)
        desk.poll()  # stop-loss
    assert desk.state["records"][W]["closed"] == 3 and desk.benched(W)
    desk.dex_.book[BONK] = Quote(BONK, "BONK", 0.0001, 200_000.0, 0)
    _trade(desk, W, "b9", "buy", BONK, 1_000_000, 0.5)
    assert desk.poll() == [] and "Track Record" in desk.state["events"][0]["text"]
    assert desk.status()["council"]["vetoes_by_agent"]["Track Record"] == 1


def test_sol_falling_halves_copies(tmp_path):
    rpc, dex, clock = FakeRPC(), FakeDex(), Clock()
    dex.book[SOL_MINT] = Quote(SOL_MINT, "SOL", 200.0, 1e9, 0, None, -0.10)
    d = Desk(tmp_path, {"capital": 1000.0}, rpc=rpc, dex=dex, bitquery=lambda: NS(window=lambda s, t: []), clock=clock)
    d.state["followed"] = ["x", "y", "z", W]   # rank 4: no top-3 boost
    d.poll()
    rpc.sigs[W] = ["s1"]
    rpc.swaps_by_sig["s1"] = [Swap(W, "s1", int(clock.t), BONK, "buy", 1_000_000, SOL_MINT, 0.5)]
    dex.book[BONK] = Quote(BONK, "BONK", 0.0001, 200_000.0, 0)
    [event] = d.poll()
    assert event["usd"] == pytest.approx(20.0) and d.status()["risk_off"] == ["SOL down 10% in a day"]


# ---------------------------------------------------------------------------
# the full team: case files, Social Scout, Second Opinion, double-check, Exit Desk
# ---------------------------------------------------------------------------

class FakeScout:
    ready = True

    def __init__(self, report=None):
        self.report_ = report or {"sentiment": 0.2, "pump_risk": 0.1, "buzz": 0.4, "red_flags": [], "green_flags": [],
                                  "sources_checked": ["X", "news", "Reddit"], "summary": "Normal chatter."}
        self.calls = []

    def report(self, symbol, mint, purpose, max_age_s):
        self.calls.append(purpose)
        return dict(self.report_)


class FakeReviewers:
    def __init__(self, entry="approve", exit="hold"):
        self.entry_, self.exit_, self.files = entry, exit, []

    def entry(self, case_file):
        self.files.append(case_file)
        return {"decision": self.entry_, "confidence": 0.7, "reason": "looks fine" if self.entry_ == "approve" else "late chase"}

    def exit(self, case_file):
        self.files.append(case_file)
        return {"decision": self.exit_, "confidence": 0.6, "reason": "momentum fading"}


def _team_desk(tmp_path, scout=None, reviewers=None, **config):
    rpc, dex, clock = FakeRPC(), FakeDex(), Clock()
    d = Desk(tmp_path, {"capital": 1000.0, **config}, rpc=rpc, dex=dex, bitquery=lambda: NS(window=lambda s, t: []),
             clock=clock, scout=scout, reviewers=reviewers)
    d.state["followed"] = [W, W2]
    d.poll()
    d.rpc_, d.dex_, d.clock_ = rpc, dex, clock
    return d


def test_a_copy_goes_through_every_desk_in_order(tmp_path):
    scout, reviewers = FakeScout(), FakeReviewers()
    d = _team_desk(tmp_path, scout, reviewers)
    d.dex_.book[BONK] = Quote(BONK, "BONK", 0.0001, 200_000.0, 0)
    _trade(d, W, "s1", "buy", BONK, 1_000_000, 0.5)
    [event] = d.poll()
    [case] = d.state["cases"]
    agents = [m["agent"] for m in case["messages"]]
    assert agents[:2] == ["FOMO Link", "Front Desk"] and agents[-2:] == ["Front Desk", "Manager"]
    assert agents.index("Social Scout") < agents.index("Second Opinion") < len(agents) - 2
    assert case["status"] == "holding" and event["case"] == case["id"] and d.state["wire"][0]["outcome"] == "copied"
    assert scout.calls == ["entry"] and reviewers.files[0]["social_scout"]["sources_checked"] == ["X", "news", "Reddit"]
    assert d.state["ai_calls"]["count"] == 2
    assert "Checked X, news, Reddit" in next(m["text"] for m in case["messages"] if m["agent"] == "Social Scout")


def test_second_opinion_can_reject_and_the_double_check_can_cancel(tmp_path):
    d = _team_desk(tmp_path, FakeScout(), FakeReviewers(entry="reject"))
    d.dex_.book[BONK] = Quote(BONK, "BONK", 0.0001, 200_000.0, 0)
    _trade(d, W, "s1", "buy", BONK, 1_000_000, 0.5)
    assert d.poll() == [] and d.state["cases"][0]["status"] == "rejected" and not d.state["positions"]

    d2 = _team_desk(tmp_path / "two", FakeScout(), FakeReviewers())
    first = Quote(BONK, "BONK", 0.0001, 200_000.0, 0)
    jumped = Quote(BONK, "BONK", 0.000108, 200_000.0, 0)
    calls = []
    d2.dex_.quotes = lambda mints: (calls.append(1), {SOL_MINT: d2.dex_.book[SOL_MINT], BONK: first} if len(calls) == 1 else {BONK: jumped})[1]
    _trade(d2, W, "s1", "buy", BONK, 1_000_000, 0.5)
    assert d2.poll() == [] and d2.state["cases"][0]["status"] == "cancelled"
    assert "price jumped 8%" in d2.state["cases"][0]["messages"][-1]["text"]


def test_ai_budget_caps_paid_checks(tmp_path):
    d = _team_desk(tmp_path, FakeScout(), FakeReviewers(), ai_checks_per_day=1)
    d.dex_.book[BONK] = Quote(BONK, "BONK", 0.0001, 200_000.0, 0)
    _trade(d, W, "s1", "buy", BONK, 1_000_000, 0.5)
    d.poll()
    texts = " ".join(m["text"] for m in d.state["cases"][0]["messages"])
    assert "budget is used up" in texts and d.state["positions"]  # the council's checks still stand


def test_exit_desk_reviews_at_checkpoints_and_withdraws(tmp_path):
    reviewers = FakeReviewers(exit="take_half")
    d = _team_desk(tmp_path, FakeScout(), reviewers)
    d.dex_.book[BONK] = Quote(BONK, "BONK", 0.0001, 200_000.0, 0)
    _trade(d, W, "s1", "buy", BONK, 1_000_000, 0.5)
    d.poll()
    qty = d.state["positions"][BONK]["qty"]
    d.dex_.book[BONK] = Quote(BONK, "BONK", 0.00016, 200_000.0, 0)   # +55%
    [half] = d.poll()
    assert half["share"] == 0.5 and d.state["positions"][BONK]["qty"] == pytest.approx(qty / 2)
    assert reviewers.files[-1]["copied_traders_still_holding"] == {"Wa11…AAA1": True}
    assert d.poll() == []                                                # same checkpoint: not asked again
    reviewers.exit_ = "exit"
    d.dex_.book[BONK] = Quote(BONK, "BONK", 0.00021, 200_000.0, 0)   # +100%
    [out] = d.poll()
    assert out["share"] == 1.0 and BONK not in d.state["positions"] and d.state["cases"][0]["status"] == "closed"
    assert any(m["agent"] == "Exit Desk" for m in d.state["cases"][0]["messages"])


def test_without_a_reviewer_the_rules_decide_and_sold_out_traders_mean_exit(tmp_path):
    d = _team_desk(tmp_path)
    d.dex_.book[BONK] = Quote(BONK, "BONK", 0.0001, 200_000.0, 0)
    _trade(d, W, "s1", "buy", BONK, 1_000_000, 0.5)
    d.poll()
    d.dex_.book[BONK] = Quote(BONK, "BONK", 0.00016, 200_000.0, 0)   # +55%: rule says hold
    assert d.poll() == []
    d.state["holdings"][W][BONK] = 0.0                                 # they quietly sold elsewhere
    d.dex_.book[BONK] = Quote(BONK, "BONK", 0.00021, 200_000.0, 0)
    [out] = d.poll()
    assert out["share"] == 1.0 and "sold out" in d.state["cases"][0]["messages"][-2]["text"]


def test_followed_traders_sells_hit_the_wire(desk):
    desk.dex_.book[WIF] = Quote(WIF, "WIF", 1.0, 500_000.0, 0)
    _trade(desk, W2, "x1", "sell", WIF, 10, 0.05)
    desk.poll()
    assert desk.state["wire"][0]["side"] == "sell" and desk.state["wire"][0]["outcome"] == "we don't hold it"


def test_social_scout_searches_x_and_the_web_and_caches(tmp_path, monkeypatch):
    from hedge_fund.fomo.scout import SocialScout, describe
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert SocialScout(tmp_path).report("BONK", BONK) is None and not SocialScout(tmp_path).ready
    sent = []

    class Http:
        def post(self, url, timeout, headers, json):
            sent.append(json)
            body = '{"sentiment": 0.7, "pump_risk": 2, "buzz": 0.5, "red_flags": ["anon team"], "green_flags": [],' \
                   ' "sources_checked": ["X", "news"], "summary": "Busy."}'
            return Resp(200, {"output": [{"type": "message", "content": [{"type": "output_text", "text": body}]}]})
    clock = Clock()
    scout = SocialScout(tmp_path, key="xai-test", session=Http(), clock=clock)
    report = scout.report("BONK", BONK, "entry")
    assert [t["type"] for t in sent[0]["tools"]] == ["x_search", "web_search"] and "BONK" in sent[0]["input"][0]["content"]
    assert report["pump_risk"] == 1.0 and report["red_flags"] == ["anon team"]   # clipped to 0..1
    assert scout.report("BONK", BONK, "entry")["cached"] and len(sent) == 1
    clock.t += 3 * 3600
    scout.report("BONK", BONK, "entry")
    assert len(sent) == 2
    assert "Checked X, news" in describe(report) and "anon team" in describe(report)


def test_reviewers_parse_strict_answers():
    from hedge_fund.fomo.review import parse, Reviewers

    class LLM:
        model = "m"

        def __init__(self, text):
            self.text = text

        def complete(self, system, user):
            return self.text
    r = Reviewers(lambda: LLM('{"decision": "Approve", "confidence": 3, "reason": "fine"}'))
    assert r.entry({"x": 1}) == {"decision": "approve", "confidence": 1.0, "reason": "fine"}
    assert Reviewers(lambda: LLM('```json\n{"decision":"take_half","reason":"r"}\n```')).exit({})["decision"] == "take_half"
    with pytest.raises(ValueError):
        parse('{"decision": "buy more"}', ("hold", "take_half", "exit"))
