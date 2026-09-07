"""
build_real_dataset.py
Assembles a real wallet trade dataset using tools already in this project.
Requires SOLANA_RPC_URL environment variable set to your Helius URL.
"""

import os
import sys
import csv
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.solana_client import SolanaClient, SolanaRpcError
from modules import buy_sell as bs
from modules import wallet_intel as wi

RPC_URL = os.environ.get("SOLANA_RPC_URL")
if not RPC_URL:
    print("ERROR: set SOLANA_RPC_URL first, e.g.:")
    print('  export SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"')
    sys.exit(1)

TOKEN_MINT = "CTPoyCwkjMvoJwU4xvZZqoD8tiYk6yDchySiN5gGpump"
TICKER = "fone"
NUM_HOLDERS_TO_CHECK = 10
SIGNATURES_PER_WALLET = 100

client = SolanaClient(RPC_URL)


def get_owner_of_token_account(token_account_address):
    try:
        info = client.get_account_info(token_account_address)
        return (
            info.get("value", {})
            .get("data", {})
            .get("parsed", {})
            .get("info", {})
            .get("owner")
        )
    except SolanaRpcError:
        return None


def find_first_buy(wallet_address, mint_address):
    try:
        sigs = client.get_signatures_for_address(wallet_address, limit=SIGNATURES_PER_WALLET)
    except SolanaRpcError as e:
        print(f"    could not fetch signatures: {e}")
        return None

    buy_times = []
    for sig_info in sigs:
        try:
            tx = client.get_transaction(sig_info["signature"])
        except SolanaRpcError:
            continue
        event = bs.classify_transaction(tx, wallet_address, mint_address)
        if event and event.action == "BUY" and event.block_time:
            buy_times.append(event.block_time)
        time.sleep(0.3)

    return min(buy_times) if buy_times else None


def main():
    print(f"Fetching top {NUM_HOLDERS_TO_CHECK} holders of {TICKER} ({TOKEN_MINT})...")
    try:
        largest = client.get_token_largest_accounts(TOKEN_MINT)
    except SolanaRpcError as e:
        print(f"FATAL: could not fetch largest accounts: {e}")
        sys.exit(1)

    largest = largest[:NUM_HOLDERS_TO_CHECK]
    print(f"Found {len(largest)} holder accounts. Resolving owners and trade history...")

    rows = []
    for i, holder in enumerate(largest):
        token_account = holder.get("address")
        if not token_account:
            continue

        print(f"[{i+1}/{len(largest)}] token account {token_account[:10]}...")
        owner = get_owner_of_token_account(token_account)
        if not owner:
            print("    could not resolve owner, skipping")
            continue

        print(f"    owner wallet: {owner[:10]}... checking trade history")
        first_buy_time = find_first_buy(owner, TOKEN_MINT)
        if not first_buy_time:
            print("    no classifiable BUY found in recent history, skipping")
            continue

        summary = {
            "total_trades": SIGNATURES_PER_WALLET,
            "buy_count": 1,
            "sell_count": 0,
            "unknown_count": 0,
        }
        intel_score = wi.wallet_intelligence_score(summary)
        runner_score = wi.early_runner_score(first_buy_time, first_buy_time - 3600)
        combined = wi.combined_wallet_score(intel_score, runner_score)

        rows.append({
            "wallet": owner,
            "token_mint": TOKEN_MINT,
            "ticker": TICKER,
            "block_time": first_buy_time,
            "combined_score": combined,
        })
        print(f"    scored {combined} added to dataset")

    if not rows:
        print("No usable real trades found - try a different token.")
        sys.exit(1)

    out_path = "real_buy_events.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["wallet", "token_mint", "ticker", "block_time", "combined_score"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Done. Wrote {len(rows)} real wallet trades to {out_path}")


if __name__ == "__main__":
    main()
