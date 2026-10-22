"""Where the FOMO desk gets its data. All three are read-only.

- Bitquery: every trade made in the FOMO app is signed by one FOMO wallet
  (identified on-chain by Bitquery; FOMO hasn't published it). Bitquery's
  realtime Solana data covers about the last 12 hours and costs a flat 5
  points per query, so the desk asks once every few hours for each trader's
  per-token buy and sell totals. That's ~40 points a day; new accounts get
  10,000 free points for the first month.
- Solana RPC: the followed wallets are watched directly on the blockchain,
  every minute, for free: list each wallet's new transactions, then read how
  its token balances changed. Set SOLANA_RPC_URL to a free Helius/QuickNode
  URL for reliability; the public endpoint works but rate-limits.
- DexScreener: prices and pool liquidity for any token, free, no key.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests

BITQUERY_URL = "https://streaming.bitquery.io/graphql"
BITQUERY_TOKEN_VAR = "BITQUERY_TOKEN"
RPC_VAR = "SOLANA_RPC_URL"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
DEXSCREENER = "https://api.dexscreener.com/tokens/v1/solana"

FOMO_SIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
CASH_MINTS = {SOL_MINT, USDC_MINT, USDT_MINT}
STABLES = {USDC_MINT, USDT_MINT}


class SourceError(Exception):
    """A data source failed or answered with something unusable."""


class SetupError(SourceError):
    """A key the desk needs is missing."""


def _post(session, url: str, payload: dict, headers: dict | None = None, timeout: float = 30.0, retries: int = 2):
    for attempt in range(retries + 1):
        try:
            resp = session.post(url, json=payload, headers=headers or {}, timeout=timeout)
        except requests.RequestException as exc:
            if attempt == retries:
                raise SourceError(f"can't reach {url.split('/')[2]}: {type(exc).__name__}") from exc
            time.sleep(1 + attempt)
            continue
        if resp.status_code == 429 and attempt < retries:
            time.sleep(2 + 2 * attempt)
            continue
        if resp.status_code != 200:
            raise SourceError(f"{url.split('/')[2]} answered HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"{url.split('/')[2]} sent something that isn't JSON") from exc
    raise SourceError("rate-limited")  # pragma: no cover


# ---------------------------------------------------------------------------
# Bitquery: FOMO's trades, aggregated per trader and token
# ---------------------------------------------------------------------------

WINDOW_QUERY = """
query FomoWindow($since: DateTime!, $till: DateTime!, $limit: Int!) {
  Solana(dataset: realtime) {
    DEXTradeByTokens(
      where: {
        Transaction: {Signer: {is: "%s"}, Result: {Success: true}}
        Trade: {Dex: {ProtocolName: {is: "dflow"}}, Currency: {MintAddress: {notIn: ["%s", "%s", "%s"]}}}
        Block: {Time: {since: $since, till: $till}}
      }
      orderBy: {descendingByField: "volume"}
      limit: {count: $limit}
    ) {
      Trade { Account { Owner } Currency { MintAddress Symbol } }
      volume: sum(of: Trade_Side_AmountInUSD)
      bought_usd: sum(of: Trade_Side_AmountInUSD, if: {Trade: {Side: {Type: {is: buy}}}})
      sold_usd: sum(of: Trade_Side_AmountInUSD, if: {Trade: {Side: {Type: {is: sell}}}})
      bought: sum(of: Trade_Amount, if: {Trade: {Side: {Type: {is: buy}}}})
      sold: sum(of: Trade_Amount, if: {Trade: {Side: {Type: {is: sell}}}})
      trades: count
    }
  }
}
""" % (FOMO_SIGNER, SOL_MINT, USDC_MINT, USDT_MINT)


@dataclass(frozen=True)
class WindowRow:
    owner: str
    mint: str
    symbol: str
    bought_usd: float
    sold_usd: float
    bought: float
    sold: float
    trades: int


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class Bitquery:
    def __init__(self, token: str | None = None, session=None, url: str = BITQUERY_URL) -> None:
        self.token = (token or os.environ.get(BITQUERY_TOKEN_VAR, "")).strip()
        if not self.token:
            raise SetupError("Add a Bitquery token (FOMO desk settings) so the desk can find FOMO's top traders.")
        self.session = session or requests.Session()
        self.url = url

    def window(self, since: str, till: str, limit: int = 5000) -> list[WindowRow]:
        """Every (trader, token) pair FOMO traded in [since, till), with totals."""
        data = _post(self.session, self.url, {"query": WINDOW_QUERY, "variables": {"since": since, "till": till, "limit": limit}},
                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}, timeout=90)
        if data.get("errors"):
            raise SourceError(f"Bitquery: {data['errors'][0].get('message', 'query failed')}"[:200])
        try:
            rows = data["data"]["Solana"]["DEXTradeByTokens"]
        except (KeyError, TypeError) as exc:
            raise SourceError("Bitquery answered without trade data") from exc
        out = []
        for r in rows:
            trade = r.get("Trade") or {}
            owner = ((trade.get("Account") or {}).get("Owner") or "").strip()
            cur = trade.get("Currency") or {}
            if not owner or not cur.get("MintAddress"):
                continue
            out.append(WindowRow(owner=owner, mint=cur["MintAddress"], symbol=(cur.get("Symbol") or "?")[:20],
                                 bought_usd=_num(r.get("bought_usd")), sold_usd=_num(r.get("sold_usd")),
                                 bought=_num(r.get("bought")), sold=_num(r.get("sold")), trades=int(_num(r.get("trades")))))
        return out


# ---------------------------------------------------------------------------
# Solana RPC: a wallet's swaps, read from its balance changes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Swap:
    wallet: str
    signature: str
    time: int            # unix seconds (block time)
    mint: str            # the token bought or sold
    side: str            # "buy" or "sell"
    amount: float        # tokens
    cash_mint: str       # what was paid / received: SOL, USDC or USDT
    cash: float          # amount of it


class SolanaRPC:
    def __init__(self, url: str | None = None, session=None, pause: float = 0.12) -> None:
        self.url = (url or os.environ.get(RPC_VAR, "") or PUBLIC_RPC).strip()
        self.session = session or requests.Session()
        self.pause = pause
        self._id = 0

    def call(self, method: str, params: list):
        self._id += 1
        if self.pause:
            time.sleep(self.pause)  # stay well under free-tier rate limits
        data = _post(self.session, self.url, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if "error" in data:
            raise SourceError(f"Solana RPC: {data['error'].get('message', 'error')}"[:200])
        return data.get("result")

    def new_signatures(self, wallet: str, until: str | None, limit: int = 25) -> list[dict]:
        """Successful transactions newer than *until*, oldest first."""
        opts = {"limit": limit}
        if until:
            opts["until"] = until
        rows = self.call("getSignaturesForAddress", [wallet, opts]) or []
        return [r for r in reversed(rows) if not r.get("err")]

    def swaps(self, wallet: str, signature: str) -> list[Swap]:
        tx = self.call("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        return parse_swaps(tx, wallet, signature) if tx else []


def parse_swaps(tx: dict, wallet: str, signature: str) -> list[Swap]:
    """Turn a transaction into the wallet's swaps: tokens it gained against
    SOL/stablecoins it spent (a buy), or the reverse (a sell)."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    deltas: dict[str, float] = {}

    def amounts(rows):
        out = {}
        for b in rows or []:
            if b.get("owner") != wallet:
                continue
            ui = (b.get("uiTokenAmount") or {}).get("uiAmount")
            out[b["mint"]] = out.get(b["mint"], 0.0) + _num(ui)
        return out
    pre, post = amounts(meta.get("preTokenBalances")), amounts(meta.get("postTokenBalances"))
    for mint in set(pre) | set(post):
        d = post.get(mint, 0.0) - pre.get(mint, 0.0)
        if abs(d) > 0:
            deltas[mint] = d
    keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys") or []
    for i, key in enumerate(keys):
        pubkey = key.get("pubkey") if isinstance(key, dict) else key
        if pubkey == wallet and i < len(meta.get("preBalances") or []):
            lamports = meta["postBalances"][i] - meta["preBalances"][i]
            if abs(lamports) > 2_000_000:  # ignore fees and rent dust (< 0.002 SOL)
                deltas[SOL_MINT] = deltas.get(SOL_MINT, 0.0) + lamports / 1e9
    cash = {m: d for m, d in deltas.items() if m in CASH_MINTS}
    tokens = {m: d for m, d in deltas.items() if m not in CASH_MINTS}
    if len(tokens) != 1 or not cash:
        return []  # transfers, multi-hop dust, liquidity moves: not a clean swap
    mint, amount = next(iter(tokens.items()))
    cash_mint, cash_amount = max(cash.items(), key=lambda kv: abs(kv[1]))
    if amount > 0 and cash_amount < 0:
        side = "buy"
    elif amount < 0 and cash_amount > 0:
        side = "sell"
    else:
        return []
    return [Swap(wallet=wallet, signature=signature, time=int(tx.get("blockTime") or 0), mint=mint, side=side,
                 amount=abs(amount), cash_mint=cash_mint, cash=abs(cash_amount))]


# ---------------------------------------------------------------------------
# DexScreener: price and liquidity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Quote:
    mint: str
    symbol: str
    price: float          # USD
    liquidity: float      # USD in the deepest pool
    pair_created: int     # ms since epoch, 0 if unknown


class DexScreener:
    def __init__(self, session=None) -> None:
        self.session = session or requests.Session()

    def quotes(self, mints: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        mints = list(dict.fromkeys(m for m in mints if m))
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            try:
                resp = self.session.get(f"{DEXSCREENER}/{','.join(chunk)}", timeout=20)
            except requests.RequestException as exc:
                raise SourceError(f"can't reach DexScreener: {type(exc).__name__}") from exc
            if resp.status_code != 200:
                raise SourceError(f"DexScreener answered HTTP {resp.status_code}")
            try:
                pairs = resp.json()
            except ValueError as exc:
                raise SourceError("DexScreener sent something that isn't JSON") from exc
            for pair in pairs if isinstance(pairs, list) else (pairs or {}).get("pairs") or []:
                base = pair.get("baseToken") or {}
                mint = base.get("address")
                if mint not in chunk:
                    continue
                price, liq = _num(pair.get("priceUsd")), _num((pair.get("liquidity") or {}).get("usd"))
                if price <= 0:
                    continue
                best = out.get(mint)
                if best is None or liq > best.liquidity:  # the deepest pool sets the price
                    out[mint] = Quote(mint, (base.get("symbol") or "?")[:20], price, liq, int(_num(pair.get("pairCreatedAt"))))
        return out
