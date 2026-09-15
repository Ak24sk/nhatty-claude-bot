"""Trade Rules: position sizing calculator + pre-trade checklist. Informational only — no execution."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class PositionSizeResult:
    max_risk_usd: float
    position_size_usd: float
    position_size_tokens: Optional[float]
    stop_loss_price: Optional[float]


def calculate_position_size(account_size_usd, risk_pct, stop_loss_pct, entry_price=None):
    max_risk_usd = account_size_usd * (risk_pct / 100.0)
    position_size_usd = 0.0 if stop_loss_pct <= 0 else max_risk_usd / (stop_loss_pct / 100.0)

    position_size_tokens = None
    stop_loss_price = None
    if entry_price and entry_price > 0:
        position_size_tokens = position_size_usd / entry_price
        stop_loss_price = entry_price * (1 - stop_loss_pct / 100.0)

    return PositionSizeResult(max_risk_usd, position_size_usd, position_size_tokens, stop_loss_price)


CHECKLIST_ITEMS = [
    ("mint_authority", "Mint authority is revoked/null"),
    ("freeze_authority", "Freeze authority is revoked/null"),
    ("lp_locked", "LP is locked or burned, not just claimed"),
    ("holder_concentration", "Top 10 holders control under 50% of supply (ideally under 20%)"),
    ("no_bundled_buys", "No obvious bundled/same-block insider buying at launch"),
    ("liquidity_size", "Liquidity is large enough to exit your position size without heavy slippage"),
    ("position_sized", "Position size follows the calculator above, not a gut-feel amount"),
    ("exit_plan", "You have both a stop-loss AND a time-stop (e.g. exit by 24h if it hasn't moved)"),
]
