"""
robinhood_inspector.py
----------------------
A scoped-down equivalent of Token Inspector, for tokens on Robinhood Chain
(an EVM-compatible Arbitrum Orbit L2, chain ID 4663) instead of Solana.

Uses:
    - Robinhood Chain's free public RPC (rpc.mainnet.chain.robinhood.com)
      for raw eth_call reads (e.g. checking ownership renouncement)
    - Its Blockscout explorer's free public API (robinhoodchain.blockscout.com)
      for token metadata, holder distribution, and contract verification status

IMPORTANT LIMITATIONS (read before trusting this for a buy decision):
    - No honeypot / sell-simulation check exists for this chain yet in any
      free service we could find. A token can pass every check here and
      still be unsellable.
    - No LP-lock equivalent check - we don't yet know which (if any) locker
      services are used on this chain.
    - "Ownership renounced" only means the owner() function returns a burn
      address; some contracts don't use Ownable at all, in which case this
      shows as unknown rather than pass/fail.
    - Best-effort, undocumented-edge-case tolerant. Treat every field as a
      data point, not a verdict.
"""

from typing import Optional

import requests

RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
BLOCKSCOUT_BASE = "https://robinhoodchain.blockscout.com/api/v2"
REQUEST_TIMEOUT = 10

BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000000000000000000000000000",  # padded zero (32 bytes)
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}

OWNER_SELECTOR = "0x8da5cb5b"  # owner()


def _eth_call(to_address: str, data: str) -> Optional[str]:
    try:
        resp = requests.post(
            RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": to_address, "data": data}, "latest"],
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json().get("result")
        return result
    except (requests.RequestException, ValueError):
        return None


def get_ownership_status(contract_address: str) -> str:
    """Returns 'renounced', 'not_renounced', or 'unknown'."""
    result = _eth_call(contract_address, OWNER_SELECTOR)
    if not result or result == "0x":
        return "unknown"
    # owner() returns a 32-byte word; the address is the last 20 bytes
    owner_hex = "0x" + result[-40:]
    if owner_hex.lower() in {a.lower() for a in BURN_ADDRESSES if len(a) == 42}:
        return "renounced"
    return "not_renounced"


def get_token_info(contract_address: str) -> dict:
    """Fetches token metadata from Blockscout's public API."""
    try:
        resp = requests.get(
            f"{BLOCKSCOUT_BASE}/tokens/{contract_address}",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        return {"error": str(e)}


def get_top_holders(contract_address: str, limit: int = 10) -> list:
    """Fetches top holders from Blockscout's public API."""
    try:
        resp = requests.get(
            f"{BLOCKSCOUT_BASE}/tokens/{contract_address}/holders",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        return items[:limit]
    except (requests.RequestException, ValueError):
        return []


def get_contract_verification(contract_address: str) -> dict:
    """Returns {'is_verified': bool, 'error': str|None}."""
    try:
        resp = requests.get(
            f"{BLOCKSCOUT_BASE}/smart-contracts/{contract_address}",
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            return {"is_verified": False, "error": None}
        resp.raise_for_status()
        data = resp.json()
        return {"is_verified": bool(data.get("is_verified", False)), "error": None}
    except (requests.RequestException, ValueError) as e:
        return {"is_verified": False, "error": str(e)}


def inspect_token(contract_address: str) -> dict:
    """
    One-call convenience function bundling everything Token Inspector's
    Robinhood Chain tab needs. Returns a dict with token_info, top_holders,
    verification, and ownership_status - each independently best-effort,
    so a failure in one doesn't block the others.
    """
    token_info = get_token_info(contract_address)
    top_holders = get_top_holders(contract_address)
    verification = get_contract_verification(contract_address)
    ownership_status = get_ownership_status(contract_address)

    total_supply = None
    try:
        total_supply = float(token_info.get("total_supply", 0)) / (10 ** int(token_info.get("decimals", 18)))
    except (TypeError, ValueError):
        pass

    top10_share = None
    if total_supply and top_holders:
        try:
            top10_amount = sum(
                float(h.get("value", 0)) / (10 ** int(token_info.get("decimals", 18)))
                for h in top_holders
            )
            top10_share = top10_amount / total_supply if total_supply else None
        except (TypeError, ValueError):
            pass

    return {
        "contract_address": contract_address,
        "name": token_info.get("name", "?"),
        "symbol": token_info.get("symbol", "?"),
        "total_supply": total_supply,
        "holders_count": token_info.get("holders_count"),
        "top10_share": top10_share,
        "is_verified": verification.get("is_verified", False),
        "ownership_status": ownership_status,
        "token_info_error": token_info.get("error"),
    }
