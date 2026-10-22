"""The FOMO desk: rebuild FOMO's leaderboard from public trades, follow the
top traders, and paper-copy their trades. Paper money only."""

from hedge_fund.fomo.desk import Desk, event_alert

__all__ = ["Desk", "event_alert"]
