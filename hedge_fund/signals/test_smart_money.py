"""Copy desk trackers: SEC parsing, point-in-time filing dates, abstentions,
the warning-only Hype Watch, and how the sleeve blends. No network."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from hedge_fund.data import sec as sec_module
from hedge_fund.data.sec import parse_13f, parse_form4, SecClient, SecSetupError
from hedge_fund.fund import Fund, load_spec
from hedge_fund.models import Signal
from hedge_fund.paths import BUNDLED_PLANS_DIR
from hedge_fund.portfolio.construction import blend_signals
from hedge_fund.signals import get_investment_approach
from hedge_fund.signals import smart_money, social
from hedge_fund.signals.smart_money import CongressModel, InsiderModel, SuperinvestorModel
from hedge_fund.signals.social import SocialWatchModel


@pytest.fixture(autouse=True)
def quick(monkeypatch, tmp_path):
    monkeypatch.setattr(sec_module, "_MIN_INTERVAL", 0)
    monkeypatch.setattr(smart_money, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(social, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("SEC_USER_AGENT", "Test Person test@example.com")


class Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._body if not isinstance(self._body, str) else json.loads(self._body)


class FakeWeb:
    """Canned pages by URL; records every request."""

    def __init__(self, pages):
        self.pages, self.headers, self.calls = pages, {}, []

    def get(self, url, **kwargs):
        self.calls.append(url)
        page = self.pages.get(url)
        return Response(200, page) if page is not None else Response(404, "")

    def post(self, url, **kwargs):
        self.calls.append(url)
        return self.pages[url](kwargs["json"])


def form4(owner, title, code, shares, price, day, officer=True, director=False):
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>{int(director)}</isDirector><isOfficer>{int(officer)}</isOfficer>
      <officerTitle>{title}</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>{day}</value></transactionDate>
      <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{"A" if code == "P" else "D"}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


def info_table(rows):
    body = "".join(
        f"""<infoTable><nameOfIssuer>{name}</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>{cusip}</cusip>
        <value>{value}</value><shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
        {f"<putCall>{put}</putCall>" if put else ""}</infoTable>"""
        for name, cusip, value, shares, put in rows)
    return ('<?xml version="1.0"?><informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">'
            f"{body}</informationTable>")


TICKERS = {"https://www.sec.gov/files/company_tickers.json": {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 797468, "ticker": "OXY", "title": "Occidental Petroleum Corp"},
    "2": {"cik_str": 21344, "ticker": "KO", "title": "COCA COLA CO"},
}}


def sec_with(pages):
    web = FakeWeb({**TICKERS, **pages})
    return SecClient(cache_dir=None, session=web), web


def submissions(rows):
    return {"filings": {"recent": {
        "form": [r[0] for r in rows], "filingDate": [r[1] for r in rows],
        "accessionNumber": [r[2] for r in rows], "primaryDocument": [r[3] for r in rows]}}}


# ---------------------------------------------------------------------------
# SEC client and parsers
# ---------------------------------------------------------------------------

def test_sec_client_refuses_to_run_anonymously(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT")
    with pytest.raises(SecSetupError):
        SecClient(cache_dir=None)
    monkeypatch.setenv("SEC_USER_AGENT", "no email here")
    with pytest.raises(SecSetupError):
        SecClient(cache_dir=None)
    monkeypatch.setenv("SEC_USER_AGENT", "Jane Smith jane@example.com")
    web = FakeWeb({})
    SecClient(cache_dir=None, session=web)
    assert web.headers["User-Agent"] == "Jane Smith jane@example.com"  # only ever what the user entered


def test_parsers_read_form4_and_13f():
    tx = parse_form4(form4("DOE JANE", "Chief Executive Officer", "P", "1,000", "50.5", "2026-09-01"))[0]
    assert (tx.owner, tx.code, tx.acquired, tx.shares, tx.price, tx.is_officer) == (
        "DOE JANE", "P", True, 1000.0, 50.5, True)
    rows = parse_13f(info_table([("OCCIDENTAL PETE CORP", "674599105", 9_000, 300, ""),
                                 ("APPLE INC", "037833100", 1_000, 10, "Put")]))
    assert [(h.issuer, h.shares, h.value, h.put_call) for h in rows] == [
        ("OCCIDENTAL PETE CORP", 300.0, 9000.0, ""), ("APPLE INC", 10.0, 1000.0, "Put")]


def test_documents_are_cached_on_disk_forever(tmp_path):
    url = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/form4.xml"
    web = FakeWeb({url: "<x/>"})
    client = SecClient(cache_dir=tmp_path, session=web)
    assert client.document("0000320193", "0000320193-26-000001", "xslF345X05/form4.xml") == "<x/>"
    again = SecClient(cache_dir=tmp_path, session=FakeWeb({}))
    assert again.document("0000320193", "0000320193-26-000001", "form4.xml") == "<x/>"
    assert web.calls == [url]


# ---------------------------------------------------------------------------
# Insider Tracker
# ---------------------------------------------------------------------------

def _insider_pages():
    base = "https://www.sec.gov/Archives/edgar/data/320193"
    return {
        "https://data.sec.gov/submissions/CIK0000320193.json": submissions([
            ("4", "2026-09-20", "0000320193-26-000005", "xslF345X05/later.xml"),   # after as_of
            ("4", "2026-09-10", "0000320193-26-000004", "xslF345X05/ceo.xml"),
            ("4", "2026-09-05", "0000320193-26-000003", "xslF345X05/dir.xml"),
            ("4", "2026-09-02", "0000320193-26-000002", "xslF345X05/sale.xml"),
            ("4", "2026-01-02", "0000320193-26-000001", "xslF345X05/old.xml"),     # outside the window
            ("10-Q", "2026-08-01", "0000320193-26-000000", "q.htm"),
        ]),
        f"{base}/000032019326000005/later.xml": form4("LATE LARRY", "CFO", "P", 10_000, 50, "2026-09-18"),
        f"{base}/000032019326000004/ceo.xml": form4("BOSS BETTY", "Chief Executive Officer", "P", 4_000, 50, "2026-09-09"),
        f"{base}/000032019326000003/dir.xml": form4("BOARD BOB", "", "P", 1_000, 50, "2026-09-04", officer=False, director=True),
        f"{base}/000032019326000002/sale.xml": form4("SELLER SAM", "COO", "S", 9_000, 50, "2026-09-01"),
        f"{base}/000032019326000001/old.xml": form4("OLD OLGA", "CEO", "P", 9_000, 50, "2026-01-01"),
    }


def test_insiders_follow_open_market_buys_by_filing_date():
    model = InsiderModel()
    model._sec, web = sec_with(_insider_pages())
    signal = model.predict("AAPL", "2026-09-15", None)
    # Two buyers (0.3 each), one of them the CEO (+0.2); the sale, the old buy
    # and the buy filed after the assessment date are all ignored.
    assert signal.value == pytest.approx(0.8)
    assert "Boss Betty" in signal.reasoning and "Late Larry" not in signal.reasoning
    assert not any("old.xml" in c or "later.xml" in c for c in web.calls)
    later = model.predict("AAPL", "2026-09-25", None)
    assert later.value == pytest.approx(1.0)  # CFO joins: 3 buyers, 2 C-suite, a cluster


def test_insiders_abstain_when_only_selling_or_unknown():
    pages = _insider_pages()
    model = InsiderModel()
    model._sec, _ = sec_with(pages)
    quiet = model.predict("AAPL", "2026-09-03", None)
    assert quiet.metadata["abstained"] and "sold" in quiet.reasoning
    etf = model.predict("SPY", "2026-09-15", None)
    assert etf.metadata["abstained"] and "no company" in etf.reasoning


def test_insiders_abstain_cleanly_without_sec_details(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT")
    signal = InsiderModel().predict("AAPL", "2026-09-15", None)
    assert signal.metadata["abstained"] and signal.metadata["abstain_reason"] == "setup"


def test_insiders_abstain_when_the_filing_list_does_not_reach_back():
    pages = _insider_pages()
    sub = pages["https://data.sec.gov/submissions/CIK0000320193.json"]
    sub["filings"]["files"] = [{"name": "CIK0000320193-submissions-001.json"}]
    model = InsiderModel()
    model._sec, _ = sec_with(pages)
    signal = model.predict("AAPL", "2025-06-01", None)
    assert signal.metadata["abstain_reason"] == "not covered"


# ---------------------------------------------------------------------------
# Fund Tracker (13F)
# ---------------------------------------------------------------------------

def _fund_pages():
    base = "https://www.sec.gov/Archives/edgar/data/1067983"
    q2, q3 = "000095012326000002", "000095012326000003"
    return {
        "https://data.sec.gov/submissions/CIK0001067983.json": submissions([
            ("13F-HR", "2026-08-14", "0000950123-26-000003", "primary_doc.xml"),
            ("13F-HR", "2026-05-15", "0000950123-26-000002", "primary_doc.xml"),
        ]),
        f"{base}/{q3}/index.json": {"directory": {"item": [{"name": "primary_doc.xml"}, {"name": "table.xml"}]}},
        f"{base}/{q2}/index.json": {"directory": {"item": [{"name": "primary_doc.xml"}, {"name": "table.xml"}]}},
        f"{base}/{q3}/table.xml": info_table([
            ("OCCIDENTAL PETE CORP", "674599105", 10_000, 200, ""),      # new and 10% of the portfolio
            ("COCA COLA CO", "191216100", 2_000, 400, ""),                # small, unchanged
            ("COCA COLA CONSOLIDATED INC", "191098102", 50_000, 50, ""),  # a different company
            ("APPLE INC", "037833100", 38_000, 1_000, ""),
        ]),
        f"{base}/{q2}/table.xml": info_table([
            ("COCA COLA CO", "191216100", 2_000, 400, ""),
            ("APPLE INC", "037833100", 40_000, 2_000, ""),
        ]),
    }


def test_fund_tracker_scores_new_big_positions_and_ignores_lookalikes():
    model = SuperinvestorModel(funds={"Berkshire Hathaway": "1067983"})
    model._sec, web = sec_with(_fund_pages())
    oxy = model.predict("OXY", "2026-09-15", None)
    assert oxy.value == pytest.approx(1.0) and "new this quarter" in oxy.reasoning
    ko = model.predict("KO", "2026-09-15", None)
    assert ko.value == pytest.approx(0.5)  # held, small, unchanged: Coca-Cola Consolidated not counted
    aapl = model.predict("AAPL", "2026-09-15", None)
    assert aapl.value == pytest.approx(0.5) and "cut 50%" in aapl.reasoning  # big holding, but halved
    tables = [c for c in web.calls if c.endswith("table.xml")]
    assert len(tables) == len(set(tables))  # each filing read once


def test_fund_tracker_only_sees_filings_already_public():
    model = SuperinvestorModel(funds={"Berkshire Hathaway": "1067983"})
    model._sec, _ = sec_with(_fund_pages())
    before = model.predict("OXY", "2026-07-01", None)  # the August filing isn't out yet
    assert before.metadata["abstained"] and "none of the 1 tracked funds" in before.reasoning
    stale = model.predict("KO", "2027-03-01", None)    # the last filing is too old to copy
    assert stale.metadata["abstained"]


# ---------------------------------------------------------------------------
# Capitol Tracker (Bargo)
# ---------------------------------------------------------------------------

def _days_ago(n):
    return (datetime.now(timezone.utc).date() - timedelta(days=n)).isoformat()


def test_congress_counts_disclosed_buys_and_credits_bargo():
    model = CongressModel()
    url = "https://www.bargo.ai/free-apis/congress/v1/trades/NVDA"
    model._session = FakeWeb({url: {"trades": [
        {"member": "Rep. A", "chamber": "House", "type": "purchase", "amount_range": "$50,001 - $100,000",
         "disclosure_date": _days_ago(10)},
        {"member": "Sen. B", "chamber": "Senate", "type": "purchase", "amount_range": "$1,001 - $15,000",
         "disclosure_date": _days_ago(5)},
        {"member": "Rep. C", "chamber": "House", "type": "sale_full", "amount_range": "$1,001 - $15,000",
         "disclosure_date": _days_ago(5)},
        {"member": "Rep. D", "chamber": "House", "type": "purchase", "amount_range": "$1,001 - $15,000",
         "disclosure_date": _days_ago(0)},  # disclosed after the assessment date
    ], "page": 1, "limit": 100, "count": 4}})
    signal = model.predict("NVDA", _days_ago(2), None)
    assert signal.value == pytest.approx(0.8)  # two members (0.3 each), one over $50k (+0.2)
    assert "Rep. D" not in signal.reasoning and "bargo.ai" in signal.reasoning
    model.predict("NVDA", _days_ago(1), None)
    assert len(model._session.calls) == 1  # one request per ticker per day


def test_congress_abstains_outside_the_free_three_months():
    model = CongressModel()
    model._session = FakeWeb({})
    signal = model.predict("NVDA", "2024-01-02", None)
    assert signal.metadata["abstain_reason"] == "not covered" and model._session.calls == []


# ---------------------------------------------------------------------------
# Hype Watch (warning only)
# ---------------------------------------------------------------------------

def _grok(risk):
    reply = {"output": [{"type": "x_search_call"}, {"type": "message", "content": [
        {"type": "output_text", "text": json.dumps({"pump_risk": risk, "summary": "Dozens of new accounts posting $XYZ to the moon."})}]}]}
    return {social.XAI_URL: lambda body: Response(200, reply)}


def test_hype_watch_only_ever_votes_against(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    model = SocialWatchModel()
    model._session = FakeWeb(_grok(0.9))
    today = _days_ago(1)
    signal = model.predict("XYZ", today, None)
    assert signal.value == pytest.approx(-0.9) and not signal.metadata.get("abstained")
    model.predict("XYZ", today, None)
    assert len(model._session.calls) == 1  # cached: a preview and the run pay once

    calm = SocialWatchModel()
    calm._session = FakeWeb(_grok(0.2))
    assert calm.predict("ABC", today, None).metadata["abstained"]
    assert get_investment_approach("social_watch") == "long_only"  # can never open a short


def test_hype_watch_sits_out_backtests_and_without_a_key(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    model = SocialWatchModel()
    model._session = FakeWeb({})
    assert model.predict("XYZ", "2025-03-03", None).metadata["abstain_reason"] == "not covered"
    monkeypatch.delenv("XAI_API_KEY")
    assert model.predict("XYZ", _days_ago(1), None).metadata["abstain_reason"] == "setup"
    assert model._session.calls == []


# ---------------------------------------------------------------------------
# The sleeve and the plan
# ---------------------------------------------------------------------------

def test_smart_money_sleeve_leaves_quiet_stocks_in_cash_and_heeds_warnings():
    def sig(model, ticker, value, abstained=False):
        return Signal(model_name=model, ticker=ticker, date="2026-09-15", value=value,
                      metadata={"abstained": True} if abstained else {})

    weights = {"insiders": 1.0, "superinvestors": 1.0, "congress": 0.5, "social_watch": 1.0}
    approaches = {m: get_investment_approach(m) for m in weights}
    signals = [
        sig("insiders", "BUY", 0.8), sig("superinvestors", "BUY", 1.0), sig("congress", "BUY", 0, True),
        sig("social_watch", "BUY", 0, True),
        sig("insiders", "QUIET", 0, True), sig("superinvestors", "QUIET", 0, True),
        sig("congress", "QUIET", 0, True), sig("social_watch", "QUIET", 0, True),
        sig("insiders", "PUMP", 0.3), sig("social_watch", "PUMP", -0.9),
    ]
    blend = blend_signals(signals, weights, 1.0, mode="long_only", investment_approaches=approaches,
                          method="slot_weighted")
    assert blend.weights["BUY"] == pytest.approx(0.9 / 3)
    assert blend.weights["QUIET"] == 0.0 and blend.weights["PUMP"] == 0.0
    assert all(w >= 0 for w in blend.weights.values())


def test_copy_desk_plan_staffs_every_tracker():
    spec = load_spec(BUNDLED_PLANS_DIR / "copy-desk.yaml")
    fund = Fund(spec)  # builds with no keys at all
    names = {m.name for _, staff in fund.strategies for m in staff}
    assert {"insiders", "superinvestors", "congress", "social_watch", "momentum"} <= names
    assert spec.risk.pump_guard is not None and spec.risk.market_filter is not None
