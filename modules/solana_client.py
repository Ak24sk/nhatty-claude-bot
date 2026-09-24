"""
solana_client.py
-----------------
Thin, read-only wrapper around the Solana JSON-RPC API.

IMPORTANT: This module never handles private keys and never submits
transactions. It only performs read (GET-equivalent) RPC calls used to
inspect public on-chain data: token accounts, transaction history,
mint/freeze authorities, and largest holders.

By default it talks to the public Solana mainnet RPC endpoint, which is
free but rate-limited and sometimes flaky. For reliable "live mode" use,
set a dedicated RPC URL (Helius / QuickNode / Triton / your own node) in
Streamlit secrets as SOLANA_RPC_URL. See README.md.
"""

import time
import requests

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5


class SolanaRpcError(Exception):
    """Raised when the RPC endpoint returns an error or is unreachable."""
    pass


class SolanaClient:
    def __init__(self, rpc_url: str = None):
        self.rpc_url = rpc_url or DEFAULT_RPC_URL

    def _call(self, method: str, params: list):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(
                    self.rpc_url, json=payload, timeout=REQUEST_TIMEOUT
                )
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise SolanaRpcError(data["error"].get("message", "RPC error"))
                return data.get("result")
            except (requests.RequestException, SolanaRpcError) as e:
                last_err = e
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise SolanaRpcError(f"RPC call '{method}' failed after retries: {last_err}")

    # ---- Wallet inspection -------------------------------------------------

    def get_signatures_for_address(self, address: str, limit: int = 50):
        """Recent transaction signatures for a wallet, newest first."""
        return self._call(
            "getSignaturesForAddress", [address, {"limit": limit}]
        ) or []

    def get_transaction(self, signature: str):
        """Full parsed transaction details for a signature."""
        return self._call(
            "getTransaction",
            [signature, {"maxSupportedTransactionVersion": 1, "encoding": "jsonParsed"}],
        )

    def get_token_accounts_by_owner(self, owner_address: str):
        """All SPL token accounts (balances) held by a wallet."""
        return self._call(
            "getTokenAccountsByOwner",
            [
                owner_address,
                {"programId": TOKEN_PROGRAM_ID},
                {"encoding": "jsonParsed"},
            ],
        ) or {"value": []}

    def get_balance(self, address: str):
        """Native SOL balance in lamports."""
        return self._call("getBalance", [address])

    # ---- Token / mint inspection -------------------------------------------

    def get_account_info(self, address: str):
        """Raw account info, used to read mint/freeze authority flags."""
        return self._call(
            "getAccountInfo", [address, {"encoding": "jsonParsed"}]
        )

    def get_token_largest_accounts(self, mint_address: str):
        """Top holder accounts for a given token mint (holder concentration)."""
        result = self._call("getTokenLargestAccounts", [mint_address])
        return (result or {}).get("value", [])

    def get_token_supply(self, mint_address: str):
        """Total supply info for a mint: {amount, decimals, uiAmount, uiAmountString}."""
        result = self._call("getTokenSupply", [mint_address])
        return (result or {}).get("value") or {}
