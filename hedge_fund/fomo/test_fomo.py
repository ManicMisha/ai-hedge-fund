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
    assert event["kind"] == "buy" and event["usd"] == pytest.approx(40.0)  # 4% of $1,000
    pos = desk.state["positions"][BONK]
    slip = 0.003 + 40 / 200_000
    assert pos["entry"] == pytest.approx(0.00011 * (1 + slip))
    assert pos["qty"] == pytest.approx((40 - 40 * FEE) / pos["entry"])
    assert event["lag"] == pytest.approx(pos["entry"] / 0.0001 - 1) and event["delay"] == 20
    assert desk.state["cash"] == pytest.approx(960.0)
    assert "Copied" in event["text"] and event_alert(event)["title"] == "FOMO copy: bought BONK"


def test_skips_small_pools_and_sizes_up_when_two_top_traders_buy(desk):
    desk.dex_.book[WIF] = Quote(WIF, "WIF", 1.0, 5_000.0, 0)
    _trade(desk, W, "s1", "buy", WIF, 100, 1.0)
    assert desk.poll() == [] and desk.state["events"][0]["kind"] == "skip"
    desk.dex_.book[WIF] = Quote(WIF, "WIF", 1.0, 500_000.0, 0)
    _trade(desk, W2, "s2", "buy", WIF, 100, 1.0)
    [event] = desk.poll()
    assert event["cluster"] is True and event["usd"] == pytest.approx(60.0)  # 1.5 x 4%


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
    assert gone["usd"] == 0 and gone["profit"] == pytest.approx(-40.0) and "written off" in gone["text"]
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
