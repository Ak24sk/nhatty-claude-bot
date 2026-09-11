"""
Dev / deployer history lookup.

Given a token's creator (deployer) wallet, fetch every token that wallet
has previously launched, and tally how many graduated (migrated to a
DEX) vs. did not.

Uses Mobula's "Get Wallet Deployer Tokens" endpoint.
Requires a free Mobula API key, stored in Streamlit secrets as:

    MOBULA_API_KEY = "your-key-here"
"""

import requests
import streamlit as st

MOBULA_BASE_URL = "https://api.mobula.io/api/2"

def get_dev_history(creator_wallet: str) -> dict:
    """
    Returns a summary dict:
        {
            "total": int,
            "migrated": int,
            "not_migrated": int,
            "migration_rate": float,  # 0-100
            "tokens": [ ... raw token entries ... ],
            "error": str | None
        }
    """
    api_key = st.secrets.get("MOBULA_API_KEY")
    if not api_key:
        return {
            "total": 0, "migrated": 0, "not_migrated": 0,
            "migration_rate": 0.0, "tokens": [],
            "error": "MOBULA_API_KEY not set in secrets.",
        }

    try:
        resp = requests.get(
            f"{MOBULA_BASE_URL}/wallet/deployer",
            params={"wallet": creator_wallet, "blockchain": "solana"},
            headers={"Authorization": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        return {
            "total": 0, "migrated": 0, "not_migrated": 0,
            "migration_rate": 0.0, "tokens": [],
            "error": f"Mobula request failed: {e}",
        }

    tokens = data.get("data", []) if isinstance(data, dict) else data
    if not isinstance(tokens, list):
        tokens = []

    total = len(tokens)
    migrated = sum(1 for t in tokens if (t.get("token") or {}).get("bonded"))
    not_migrated = total - migrated
    migration_rate = (migrated / total * 100) if total else 0.0

    return {
        "total": total,
        "migrated": migrated,
        "not_migrated": not_migrated,
        "migration_rate": round(migration_rate, 1),
        "tokens": tokens,
        "error": None,
    }


def render_dev_history(creator_wallet: str):
    """Renders a compact dev-history stat block in the current Streamlit context."""
    if not creator_wallet:
        st.info("No creator/deployer address available for this token.")
        return

    result = get_dev_history(creator_wallet)

    if result["error"]:
        st.warning(f"Dev history unavailable: {result['error']}")
        return

    if result["total"] == 0:
        st.info("This wallet has no other tracked token launches.")
        return

    st.markdown(
        f"**Dev history:** {result['total']} token(s) launched · "
        f"{result['migrated']} graduated ({result['migration_rate']}%) · "
        f"{result['not_migrated']} did not"
    )

    if result["migration_rate"] < 20:
        st.error("⚠️ Low graduation rate — pattern consistent with serial rug behavior.")
    elif result["migration_rate"] < 50:
        st.warning("Below-average graduation rate — proceed with caution.")
    else:
        st.success("Healthy graduation rate for this dev.")
