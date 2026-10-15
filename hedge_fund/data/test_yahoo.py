"""The free Yahoo price source and the data-source switch. No network."""

import pytest

from hedge_fund.data import source
from hedge_fund.data.client import FDClient
from hedge_fund.data.yahoo import parse_chart, yahoo_symbol, YahooClient, YahooError

# 2024-06-03 and 2024-06-04, 13:30 UTC = 09:30 New York (market open)
STAMPS = [1717421400, 1717507800, 1717594200]
CHART = {"result": [{
    "meta": {"exchangeTimezoneName": "America/New_York"},
    "timestamp": STAMPS,
    "indicators": {"quote": [{"open": [10, 11, 12], "high": [10.5, 11.5, 12.5],
                              "low": [9.5, 10.5, 11.5], "close": [10.2, None, 12.2],
                              "volume": [100, 200, None]}]},
}], "error": None}


class Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class Session:
    def __init__(self, *responses):
        self.responses, self.calls, self.headers = list(responses), 0, {}

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return self.responses.pop(0)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def completed(monkeypatch):
    import hedge_fund.data.yahoo as y
    monkeypatch.setattr(y, "completed_through", lambda: "2024-06-05")


def test_parse_chart_dates_by_exchange_day_and_skips_missing_closes():
    bars = parse_chart("TEST", CHART)
    assert [b.time[:10] for b in bars] == ["2024-06-03", "2024-06-05"]
    assert bars[0].close == 10.2 and bars[1].volume == 0
    assert parse_chart("NOPE", {"result": None, "error": {"code": "Not Found"}}) == []
    with pytest.raises(YahooError):
        parse_chart("BAD", {"result": None, "error": {"code": "Internal"}})


def test_symbols_use_dashes_for_share_classes():
    assert yahoo_symbol("brk.b") == "BRK-B"


def test_one_fetch_serves_every_range_and_survives_on_disk(tmp_path):
    session = Session(Resp(200, {"chart": CHART}))
    client = YahooClient(cache_dir=tmp_path, session=session)
    assert [b.close for b in client.get_prices("TEST", "2024-06-01", "2024-06-30")] == [10.2, 12.2]
    assert [b.close for b in client.get_prices("TEST", "2024-06-04", "2024-06-05")] == [12.2]
    assert session.calls == 1
    fresh = YahooClient(cache_dir=tmp_path, session=Session())  # no responses: must use disk
    assert len(fresh.get_prices("TEST", "2024-06-01", "2024-06-05")) == 2


def test_failures_raise_and_unknown_symbols_are_empty(tmp_path, monkeypatch):
    import hedge_fund.data.yahoo as y
    monkeypatch.setattr(y.time, "sleep", lambda s: None)
    assert YahooClient(cache_dir=tmp_path, session=Session(Resp(404))).get_prices("ZZZZ", "2024-01-01", "2024-06-05") == []
    busy = Session(Resp(429), Resp(429), Resp(429))
    with pytest.raises(YahooError, match="429"):
        YahooClient(cache_dir=tmp_path, session=busy).get_prices("SPY", "2024-01-01", "2024-06-05")
    assert busy.calls == 3
    with pytest.raises(YahooError, match="daily"):
        YahooClient(cache_dir=tmp_path, session=Session()).get_prices("SPY", "2024-01-01", "2024-06-05", "week")


def test_fundamentals_are_honestly_absent(tmp_path):
    client = YahooClient(cache_dir=tmp_path, session=Session())
    assert client.get_financial_metrics("AAPL", "2024-06-05") == []
    assert client.get_earnings_history("AAPL") == [] and client.get_market_cap("AAPL", "2024-06-05") is None


def test_source_switch(monkeypatch):
    monkeypatch.delenv("DATA_SOURCE", raising=False)
    monkeypatch.delenv("FINANCIAL_DATASETS_API_KEY", raising=False)
    assert source.data_source() == "yahoo" and not source.needs_data_key()
    assert isinstance(source.open_client(), YahooClient)
    monkeypatch.setenv("FINANCIAL_DATASETS_API_KEY", "k")
    assert source.data_source() == "financialdatasets"
    assert isinstance(source.open_client(), FDClient)
    monkeypatch.setenv("DATA_SOURCE", "yahoo")
    assert source.data_source() == "yahoo"
    monkeypatch.setenv("DATA_SOURCE", "financialdatasets")
    monkeypatch.delenv("FINANCIAL_DATASETS_API_KEY")
    assert source.needs_data_key()
