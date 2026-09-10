"""
demo_data.py
------------
Generates believable synthetic data so the whole app can be explored on
Streamlit Community Cloud with zero API keys and zero live RPC calls. Also
provides CSV loaders for the "bring your own data" mode.
"""

import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List
import pandas as pd

FAKE_TICKERS = ["WOJAK2", "FROGGY", "MOONCAT", "BONKZ", "PEPESOL",
                "TURBOAPE", "RUGCHECK", "SOLDOGE", "NANOFOX", "ZKPUP"]


def _fake_address(rnd: random.Random, prefix: str = "") -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return prefix + "".join(rnd.choice(alphabet) for _ in range(40))


def generate_demo_wallets(n: int = 12, seed: int = 42) -> pd.DataFrame:
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        buy_count = rnd.randint(2, 40)
        sell_count = rnd.randint(0, buy_count)
        rows.append({
            "wallet": _fake_address(rnd, "W"),
            "total_trades": buy_count + sell_count + rnd.randint(0, 5),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "unknown_count": rnd.randint(0, 3),
            "first_trade_time": int((datetime.now(timezone.utc) -
                                      timedelta(days=rnd.randint(5, 400))).timestamp()),
        })
    return pd.DataFrame(rows)


def generate_demo_tokens(n: int = 8, seed: int = 7) -> pd.DataFrame:
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        ticker = FAKE_TICKERS[i % len(FAKE_TICKERS)]
        mint = _fake_address(rnd, "T")
        top10_share = round(rnd.uniform(0.08, 0.55), 3)
        rows.append({
            "token_mint": mint,
            "ticker": ticker,
            "total_supply": rnd.choice([1_000_000_000, 100_000_000, 1_000_000_000_000]),
            "top10_holder_share": top10_share,
            "mint_authority_renounced": rnd.random() > 0.3,
            "freeze_authority_renounced": rnd.random() > 0.2,
            "lp_locked": rnd.random() > 0.35,
            "sell_simulation_ok": rnd.random() > 0.1,
            "created_minutes_ago": rnd.randint(10, 600),
        })
    return pd.DataFrame(rows)


def generate_demo_buy_events(wallets_df: pd.DataFrame, tokens_df: pd.DataFrame,
                              seed: int = 99) -> pd.DataFrame:
    """Synthetic buy events used to feed the convergence detector — some
    tokens get a cluster of near-simultaneous buys from several wallets."""
    rnd = random.Random(seed)
    rows = []
    now = datetime.now(timezone.utc)

    for _, token in tokens_df.iterrows():
        cluster_size = rnd.choice([0, 0, 1, 2, 4, 6])
        anchor_time = now - timedelta(minutes=rnd.randint(5, 500))
        chosen_wallets = wallets_df.sample(min(cluster_size, len(wallets_df)), random_state=rnd.randint(0, 9999))
        for _, w in chosen_wallets.iterrows():
            jitter = timedelta(minutes=rnd.randint(-15, 15))
            rows.append({
                "wallet": w["wallet"],
                "token_mint": token["token_mint"],
                "ticker": token["ticker"],
                "block_time": int((anchor_time + jitter).timestamp()),
            })
    return pd.DataFrame(rows)


def load_csv(path_or_buffer) -> pd.DataFrame:
    return pd.read_csv(path_or_buffer)


def load_watchlist_csv(path_or_buffer):
    """Parse + validate a watchlist CSV. Returns (valid_df, errors)."""
    import re
    df = pd.read_csv(path_or_buffer)
    df.columns = [c.strip().lower() for c in df.columns]

    required = {"wallet", "token_mint", "ticker"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(sorted(missing))}")

    mint_re = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
    ticker_re = re.compile(r"^[A-Za-z0-9$]{1,15}$")

    errors = []
    valid_idx = []
    seen = set()

    for i, row in df.iterrows():
        wallet = str(row["wallet"]).strip()
        mint = str(row["token_mint"]).strip()
        ticker = str(row["ticker"]).strip()
        key = (wallet, mint)
        reason = None

        if not wallet or wallet.lower() == "nan" or not mint_re.match(wallet):
            reason = "invalid wallet"
        elif not mint or mint.lower() == "nan" or not mint_re.match(mint):
            reason = "invalid token_mint"
        elif not ticker or ticker.lower() == "nan" or not ticker_re.match(ticker):
            reason = "invalid ticker"
        elif key in seen:
            reason = "duplicate wallet+token_mint"

        if reason:
            errors.append({"row": i + 2, "wallet": wallet, "token_mint": mint, "ticker": ticker, "reason": reason})
        else:
            valid_idx.append(i)
            seen.add(key)

    valid_df = df.loc[valid_idx].reset_index(drop=True)
    return valid_df, errors
