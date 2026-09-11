"""
live_tokens.py
--------------
Builds a real-data `tokens_df` (one row per unique token_mint) from a
validated watchlist, for use in Live (Solana RPC) mode's Overview tab.

Reuses the same data sources as Token Inspector:
    - SolanaClient for mint info, supply, and top holder accounts
    - modules.rugcheck for LP-lock / honeypot-proxy signals
    - modules.gecko_price for pool-creation time (as an age proxy)

Note: this does NOT compute real wallet-convergence / buy counts.
That requires scanning on-chain transaction history per wallet+token
pair (same expensive work as Wallet Scanner / Smart Money), and is left
at 0 for now by design.
"""

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from modules import gecko_price as gpr
from modules import rugcheck as rc
from modules.solana_client import SolanaClient, SolanaRpcError


def _minutes_since(iso_timestamp: str) -> int:
    if not iso_timestamp:
        return 0
    try:
        ts = iso_timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0, int(delta.total_seconds() // 60))
    except (ValueError, TypeError):
        return 0


@st.cache_data(ttl=180, show_spinner=False)
def build_live_tokens_df(watchlist_df: pd.DataFrame, rpc_url: str) -> tuple[pd.DataFrame, list]:
    """
    watchlist_df: validated watchlist with at least 'token_mint' and 'ticker' columns.
    Returns (tokens_df, errors) - tokens_df has one row per unique token_mint using
    real chain data; errors is a list of {"mint": str, "reason": str} for any tokens
    that failed to load.
    """
    errors = []
    if watchlist_df.empty:
        return pd.DataFrame(), errors
    if not rpc_url:
        errors.append({"mint": "(all)", "reason": "No RPC URL configured"})
        return pd.DataFrame(), errors

    client = SolanaClient(rpc_url)
    unique_tokens = watchlist_df.drop_duplicates(subset=["token_mint"])

    rows = []
    for _, row in unique_tokens.iterrows():
        mint_addr = row["token_mint"]
        ticker = row.get("ticker", "?")

        try:
            mint_info = client.get_account_info(mint_addr)
            supply_info = client.get_token_supply(mint_addr)
            largest = client.get_token_largest_accounts(mint_addr)
        except SolanaRpcError as e:
            errors.append({"mint": mint_addr, "reason": f"RPC error: {e}"})
            continue

        if not mint_info:
            errors.append({"mint": mint_addr, "reason": "No mint info returned"})
            continue

        parsed_info = (
            (mint_info or {}).get("value", {}).get("data", {})
            .get("parsed", {}).get("info", {})
        )
        mint_authority_renounced = parsed_info.get("mintAuthority") is None
        freeze_authority_renounced = parsed_info.get("freezeAuthority") is None

        total_supply = float((supply_info or {}).get("uiAmount") or 0)
        top10_accounts = (largest or [])[:10]
        top10_amount = sum(float(a.get("uiAmount") or 0) for a in top10_accounts)
        top10_holder_share = (top10_amount / total_supply) if total_supply else 0.0

        real_largest_accounts = [
            {"uiAmount": float(a.get("uiAmount") or 0)} for a in top10_accounts
        ]

        rc_data = rc.get_lp_lock_and_honeypot(mint_addr)
        lp_locked = bool(rc_data.get("lp_locked"))
        sell_simulation_ok = bool(rc_data.get("honeypot_proxy", True))

        market_data = gpr.get_token_market_data(mint_addr)
        created_minutes_ago = _minutes_since((market_data or {}).get("pool_created_at"))

        rows.append({
            "token_mint": mint_addr,
            "ticker": ticker,
            "total_supply": total_supply,
            "top10_holder_share": top10_holder_share,
            "mint_authority_renounced": mint_authority_renounced,
            "freeze_authority_renounced": freeze_authority_renounced,
            "lp_locked": lp_locked,
            "sell_simulation_ok": sell_simulation_ok,
            "created_minutes_ago": created_minutes_ago,
            "real_largest_accounts": real_largest_accounts,
        })

    return pd.DataFrame(rows), errors
