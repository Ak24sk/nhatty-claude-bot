"""
Solana Memecoin Signal Bot – v2.2
==================================
A research / alerting dashboard for Solana memecoins. This tool is
informational only:

  - No private keys are ever requested, stored, or used.
  - No trades are ever executed by this app.
  - Every score is a heuristic proxy, not financial advice.

Run modes:
  - Demo Mode: fully synthetic data, works with zero setup / zero API keys.
  - CSV Mode: bring your own wallets / tokens / buy-events CSVs.
  - Live Mode: pulls real data from the public Solana RPC (and, if you
    provide a bearer token in secrets, the X API) — slower and rate-limited.

See README.md for setup, architecture, and how to wire up live mode.
"""

import time
import pandas as pd
import streamlit as st

from modules import demo_data as dd
from modules import gecko_price as gpr
from modules import dev_history as dh
from modules import wallet_intel as wi
from modules import buy_sell as bs
from modules import convergence as conv
from modules import x_attention as xa
from modules import safety_gate as sg
from modules import rugcheck as rc
from modules import backtest as bt
from modules.solana_client import SolanaClient, SolanaRpcError

st.set_page_config(
    page_title="Solana Memecoin Signal Bot v2.2",
    page_icon="🛰️",
    layout="wide",
)

THEMES = {
    "Violet (default)": """
<style>
h1, h2, h3 {
    background: linear-gradient(90deg, #7C3AED, #06B6D4);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-weight: 800 !important;
    letter-spacing: 0.5px;
}
</style>
""",
    "Terminal Green": """
<style>
body, .stApp {
    background-color: #0d1117 !important;
}
h1, h2, h3 {
    background: linear-gradient(90deg, #00FF9C, #00B368);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-weight: 800 !important;
    letter-spacing: 0.5px;
    font-family: monospace;
}
</style>
""",
    "Light Mode": """
<style>
body, .stApp {
    background-color: #FAFAFA !important;
    color: #111111 !important;
}
h1, h2, h3 {
    background: linear-gradient(90deg, #7C3AED, #06B6D4);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-weight: 800 !important;
}
</style>
""",
}

selected_theme = st.sidebar.selectbox("Theme", list(THEMES.keys()))
CUSTOM_CSS = THEMES[selected_theme]
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

STATUS_COLORS = {
    "BLOCKED": "#7f1d1d",
    "WATCH": "#78716c",
    "BUILDING": "#a16207",
    "ALERT": "#15803d",
}


def status_badge(status: str) -> str:
    color = STATUS_COLORS.get(status, "#334155")
    return (
        f'<span style="background-color:{color};color:white;padding:3px 10px;'
        f'border-radius:6px;font-weight:600;font-size:0.85rem;">{status}</span>'
    )


# --------------------------------------------------------------------------
# Sidebar — mode + settings
# --------------------------------------------------------------------------

st.sidebar.title("🛰️ Signal Bot v2.2")
mode = "Live (Solana RPC)"
st.sidebar.caption("Data source: Live (Solana RPC) only.")

st.sidebar.markdown("---")
st.sidebar.caption(
    "⚠️ No private keys are ever requested. This app never places trades. "
    "All scores are heuristic research signals, not financial advice."
)

def _get_secret(key: str, default: str = "") -> str:
    """Safely read from st.secrets even when no secrets.toml exists locally."""
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


rpc_url = None
x_bearer = None
if mode == "Live (Solana RPC)":
    st.sidebar.subheader("Live mode settings")
    rpc_url = st.sidebar.text_input(
        "Solana RPC URL",
        value=_get_secret("SOLANA_RPC_URL"),
        placeholder="https://your-rpc-provider.com/...",
        help="Public RPC is heavily rate-limited. For reliable use, get a "
             "free/paid endpoint from Helius, QuickNode, or Triton.",
    )
    x_bearer = st.sidebar.text_input(
        "X (Twitter) API bearer token (optional)",
        value=_get_secret("X_BEARER_TOKEN"),
        type="password",
        help="Leave blank to keep using simulated attention data even in Live Mode.",
    )

st.title("Solana Memecoin Signal Bot")
st.caption("Wallet intelligence · convergence detection · X attention · safety gate — alerts only, no auto-trading")

tabs = st.tabs([
    "📊 Overview",
    "👛 Wallet Scanner",
    "🧠 Wallet Intelligence",
    "🔗 Convergence",
    "📣 X Attention",
    "🛡️ Safety Gate",
    "🔍 Token Inspector",
    "📈 Score Validation",
    "🐋 Smart Money",
])


# --------------------------------------------------------------------------
# Data loading per-mode
# --------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _demo_data():
    wallets = dd.generate_demo_wallets()
    tokens = dd.generate_demo_tokens()
    buys = dd.generate_demo_buy_events(wallets, tokens)
    return wallets, tokens, buys


def load_data():
    if mode == "Demo Mode":
        return _demo_data()

    if mode == "CSV Upload":
        st.sidebar.subheader("Upload CSVs")
        w_file = st.sidebar.file_uploader("Wallets CSV", type="csv", key="w")
        t_file = st.sidebar.file_uploader("Tokens CSV", type="csv", key="t")
        b_file = st.sidebar.file_uploader("Buy events CSV", type="csv", key="b")

        wallets = dd.load_csv(w_file) if w_file else pd.DataFrame()
        tokens = dd.load_csv(t_file) if t_file else pd.DataFrame()
        buys = dd.load_csv(b_file) if b_file else pd.DataFrame()

        if wallets.empty or tokens.empty:
            st.info(
                "Upload wallets + tokens CSVs in the sidebar to begin, or "
                "download the sample files below as a template.",
                icon="📄",
            )
            for name in ["sample_wallets.csv", "sample_tokens.csv", "sample_buy_events.csv"]:
                try:
                    with open(f"data/{name}", "rb") as f:
                        st.sidebar.download_button(f"Download {name}", f, file_name=name)
                except FileNotFoundError:
                    pass
        return wallets, tokens, buys

    # Live mode: wallets/buys still come from CSV upload or demo scaffolding,
    # because discovering *which* wallets to watch from scratch requires a
    # curated watchlist — this app scores whatever wallets/tokens you feed it,
    # live, against the real chain. See README "Live mode" section.
    st.sidebar.subheader("Watchlist input")
    wl_file = st.sidebar.file_uploader(
        "Watchlist CSV (columns: wallet, token_mint, ticker)", type="csv", key="wl"
    )
    if wl_file is None:
        st.info(
            "Live Mode scores a watchlist you provide against the real "
            "chain. Upload a CSV with columns: wallet, token_mint, ticker.",
            icon="ℹ️",
        )
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    watchlist, csv_errors = dd.load_watchlist_csv(wl_file)
    if csv_errors:
        error_lines = [
            f"Row {e['row']}: wallet=`{e['wallet']}` mint=`{e['token_mint']}` ticker=`{e['ticker']}` — {e['reason']}"
            for e in csv_errors
        ]
        st.sidebar.warning(f"Skipped {len(csv_errors)} row(s):\n\n" + "\n\n".join(error_lines))
    if watchlist.empty:
        st.warning("No valid rows in watchlist CSV.")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    return watchlist, pd.DataFrame(), pd.DataFrame()

wallets_df, tokens_df, buys_df = load_data()


# --------------------------------------------------------------------------
# Tab 1: Overview
# --------------------------------------------------------------------------

with tabs[0]:
    st.subheader("Unified status board")
    st.write(
        "Every token gets one combined status, driven by the safety gate "
        "first, then wallet + attention signals:"
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(f"{status_badge('BLOCKED')}", unsafe_allow_html=True)
    c1.caption("Failed a hard safety check. Never alerted, regardless of other signals.")
    c2.markdown(f"{status_badge('WATCH')}", unsafe_allow_html=True)
    c2.caption("Passes safety. Signals still early/weak.")
    c3.markdown(f"{status_badge('BUILDING')}", unsafe_allow_html=True)
    c3.caption("Passes safety. Multiple signals trending up.")
    c4.markdown(f"{status_badge('ALERT')}", unsafe_allow_html=True)
    c4.caption("Passes safety. Strong convergence + attention spike.")

    st.markdown("---")

    if mode != "Live (Solana RPC)" and (wallets_df.empty or tokens_df.empty):
        st.warning("No data loaded yet.")
    elif not tokens_df.empty:
        overview_rows = []
        for _, token in tokens_df.iterrows():
            largest_accounts = [
                {"uiAmount": token["total_supply"] * s}
                for s in [0.15, 0.08, 0.05, 0.04, 0.03, 0.02, 0.02, 0.01, 0.01, 0.01]
            ]
            # Scale synthetic holders so top10 share roughly matches the demo column
            scale = token["top10_holder_share"] / 0.42
            largest_accounts = [{"uiAmount": a["uiAmount"] * scale} for a in largest_accounts]

            safety = sg.run_safety_gate(
                mint_account_info={"value": {"data": {"parsed": {"info": {
                    "mintAuthority": None if token["mint_authority_renounced"] else "SomeAuthorityPubkey",
                    "freezeAuthority": None if token["freeze_authority_renounced"] else "SomeAuthorityPubkey",
                }}}}},
                largest_accounts=largest_accounts,
                total_supply=token["total_supply"],
                lp_locked=token["lp_locked"],
                simulated_sell_ok=token["sell_simulation_ok"],
            )

            token_buys = buys_df[buys_df["token_mint"] == token["token_mint"]] if not buys_df.empty else pd.DataFrame()
            wallet_count = token_buys["wallet"].nunique() if not token_buys.empty else 0

            timeline = xa.get_attention_timeline(token["ticker"], demo_mode=True)
            accel = xa.compute_acceleration(timeline)

            avg_score = 55  # placeholder aggregate; see Wallet Intelligence tab for real per-wallet scores
            status = sg.unified_status(safety, wallet_count, accel["label"], avg_score)

            overview_rows.append({
                "Ticker": token["ticker"],
                "Status": status,
                "Safety": "✅ Pass" if safety["passed"] else f"❌ {', '.join(safety['failed_checks'])}",
                "Converging wallets": wallet_count,
                "X attention": accel["label"],
                "Age (min)": token["created_minutes_ago"],
            })

        overview_df = pd.DataFrame(overview_rows)
        for _, row in overview_df.iterrows():
            cols = st.columns([1.2, 1, 2, 1.3, 1.3, 1])
            cols[0].markdown(f"**{row['Ticker']}**")
            cols[1].markdown(status_badge(row["Status"]), unsafe_allow_html=True)
            cols[2].write(row["Safety"])
            cols[3].write(f"{row['Converging wallets']} wallets")
            cols[4].write(row["X attention"])
            cols[5].write(f"{row['Age (min)']}m")
    else:
        st.info("Load a watchlist in the sidebar to see live results here.")


# --------------------------------------------------------------------------
# Tab 2: Wallet Scanner
# --------------------------------------------------------------------------

with tabs[1]:
    st.subheader("Wallet scanner")
    st.caption("Transaction history + likely buy/sell detection for a single wallet.")

    if mode == "Live (Solana RPC)":
        SAVED_WALLETS_PATH_WS = "data/smart_money_wallets.txt"
        saved_wallet_options = ["Manual entry"]
        saved_wallet_map = {}
        try:
            with open(SAVED_WALLETS_PATH_WS, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if "," in line:
                        label, address = line.split(",", 1)
                        label, address = label.strip(), address.strip()
                    else:
                        label, address = line[:6], line
                    display = f"{label} ({address[:4]}...{address[-4:]})"
                    saved_wallet_options.append(display)
                    saved_wallet_map[display] = address
        except FileNotFoundError:
            pass

        chosen = st.selectbox("Pick a saved wallet (from Smart Money list) or enter manually", saved_wallet_options)
        if chosen != "Manual entry":
            addr = saved_wallet_map[chosen]
            st.caption(f"Using: `{addr}`")
        else:
            addr = st.text_input("Wallet address")

        any_token_mode = st.checkbox(
            "Check ANY token bought (ignore mint field below - buy-detection only, no sell tracking in this mode)"
        )
        mint = st.text_input("Token mint to check buys/sells against", disabled=any_token_mode)
        limit = st.slider("How many recent signatures to pull", 5, 100, 25)

        if st.button("Scan wallet", type="primary") and addr:
            if not rpc_url:
                st.error("Set a Solana RPC URL in the sidebar first.")
            else:
                client = SolanaClient(rpc_url)
                with st.spinner("Pulling recent signatures..."):
                    try:
                        sigs = client.get_signatures_for_address(addr, limit=limit)
                    except SolanaRpcError as e:
                        st.error(f"RPC error: {e}")
                        sigs = []

                if sigs:
                    st.write(f"Found {len(sigs)} recent signatures. Classifying trades...")

                    if any_token_mode:
                        all_buys = []
                        progress = st.progress(0.0)
                        for i, s in enumerate(sigs):
                            try:
                                tx = client.get_transaction(s["signature"])
                                buys = bs.detect_buys_any_token(tx, addr)
                                for b in buys:
                                    b["signature"] = s["signature"]
                                    b["block_time"] = tx.get("blockTime")
                                    all_buys.append(b)
                            except SolanaRpcError:
                                pass
                            progress.progress((i + 1) / len(sigs))
                            time.sleep(0.05)
                        if all_buys:
                            st.metric("Distinct tokens bought", len(set(b["token_mint"] for b in all_buys)))
                            st.dataframe(pd.DataFrame(all_buys), use_container_width=True)
                        else:
                            st.info("No buys of any token detected in this window.")
                    else:
                        trades = []
                        progress = st.progress(0.0)
                        for i, s in enumerate(sigs):
                            try:
                                tx = client.get_transaction(s["signature"])
                                if mint:
                                    event = bs.classify_transaction(tx, addr, mint)
                                    if event:
                                        trades.append(event)
                            except SolanaRpcError:
                                pass
                            progress.progress((i + 1) / len(sigs))
                            time.sleep(0.05)  # be gentle with public RPC rate limits

                        if mint and trades:
                            summary = bs.summarize_trades(trades)
                            net_token_flow = round(sum(
                                t.token_delta if t.action == "BUY" else -t.token_delta if t.action == "SELL" else 0
                                for t in trades
                            ), 6)
                            c1, c2, c3, c4 = st.columns(4)
                            c1.metric("Buys", summary["buy_count"])
                            c2.metric("Sells", summary["sell_count"])
                            c3.metric("SOL spent (buys)", summary["total_sol_spent_on_buys"])
                            c4.metric("Net token flow", net_token_flow)
                            st.markdown("---")
                            trades_df = pd.DataFrame([t.__dict__ for t in trades])
                            st.dataframe(trades_df, use_container_width=True)
                        elif mint:
                            st.info("No classifiable buy/sell events found for that mint in this window.")
                        else:
                            st.info("Enter a token mint above to classify buys/sells; showing raw signatures instead.")
                            st.dataframe(pd.DataFrame(sigs), use_container_width=True)
    else:
        if wallets_df.empty:
            st.info("No wallet data loaded.")
        else:
            st.dataframe(wallets_df, use_container_width=True)
            st.caption(
                "This is trade-summary data (Demo/CSV mode). Switch to Live "
                "mode to pull real transaction history for one wallet at a time."
            )


# --------------------------------------------------------------------------
# Tab 3: Wallet Intelligence
# --------------------------------------------------------------------------

with tabs[2]:
    st.subheader("Wallet intelligence & early-runner scoring")
    st.caption(
        "Wallet Intelligence Score: proxy for skilled/informed trading behavior. "
        "Early-Runner Score: how early this wallet tends to enter relative to a token's early window."
    )

    if wallets_df.empty:
        st.info("No wallet data loaded.")
    else:
        rows = []
        for _, w in wallets_df.iterrows():
            summary = {
                "total_trades": w.get("total_trades", 0),
                "buy_count": w.get("buy_count", 0),
                "sell_count": w.get("sell_count", 0),
                "unknown_count": w.get("unknown_count", 0),
            }
            intel = wi.wallet_intelligence_score(summary)

            token_earliest = int(tokens_df["created_minutes_ago"].min()) if not tokens_df.empty else 0
            runner = wi.early_runner_score(
                wallet_first_buy_time=w.get("first_trade_time"),
                token_earliest_seen_time=w.get("first_trade_time", 0) - token_earliest * 60
                if w.get("first_trade_time") else None,
            )
            rows.append({
                "wallet": w["wallet"][:10] + "…",
                "intel_score": intel,
                "runner_score": runner,
            })

        ranked = wi.rank_wallets(rows)
        st.dataframe(pd.DataFrame(ranked), use_container_width=True)


# --------------------------------------------------------------------------
# Tab 4: Convergence
# --------------------------------------------------------------------------

with tabs[3]:
    st.subheader("Wallet convergence detection")
    st.caption("Multiple independent wallets buying the same token within a short window.")

    window_min = st.slider("Convergence window (minutes)", 5, 120, 30)
    min_wallets = st.slider("Minimum wallets to flag a cluster", 2, 10, 3)

    if buys_df.empty:
        st.info("No buy-event data loaded.")
    else:
        events = buys_df.copy()
        events["combined_score"] = 50  # simple default; wire to Tab 3 scores if desired
        clusters = conv.detect_convergence(
            events.to_dict("records"),
            window_seconds=window_min * 60,
            min_wallets=min_wallets,
        )
        if clusters:
            for c in clusters:
                ticker_lookup = tokens_df.set_index("token_mint")["ticker"].to_dict() if not tokens_df.empty else {}
                ticker = ticker_lookup.get(c["token_mint"], c["token_mint"][:8] + "…")
                st.markdown(
                    f"**{ticker}** — {c['wallet_count']} wallets converged "
                    f"(avg score {c['avg_wallet_score']})"
                )
                st.caption(f"{len(c['wallets'])} wallets: " + ", ".join(w[:8] + "…" for w in c["wallets"][:6]))
        else:
            st.info("No convergence clusters found at the current thresholds.")


# --------------------------------------------------------------------------
# Tab 5: X Attention
# --------------------------------------------------------------------------

with tabs[4]:
    st.subheader("X (Twitter) attention engine")
    query = st.text_input("Ticker / query to check", value="WOJAK2")

    if st.button("Check attention", type="primary"):
        use_live = mode == "Live (Solana RPC)" and bool(x_bearer)
        timeline = xa.get_attention_timeline(query, bearer_token=x_bearer, demo_mode=not use_live)
        accel = xa.compute_acceleration(timeline)
        bot_share = xa.estimate_bot_share(seed=query)

        col1, col2, col3 = st.columns(3)
        col1.metric("Acceleration ratio", f"{accel['acceleration_ratio']}x", help="Recent hourly avg vs. prior baseline")
        col2.metric("Label", accel["label"])
        col3.metric("Est. bot/coordinated share", f"{bot_share:.0%}")

        st.line_chart(pd.DataFrame(timeline).set_index("hour_start")["post_count"])
        if not use_live:
            st.caption("Showing simulated attention data (Demo Mode, or no X bearer token set).")


# --------------------------------------------------------------------------
# Tab 6: Safety Gate
# --------------------------------------------------------------------------

with tabs[5]:
    st.subheader("Safety gate")
    st.caption("Hard checks that BLOCK a token regardless of how good other signals look.")

    if tokens_df.empty:
        st.info("No token data loaded.")
    else:
        pick = st.selectbox("Token", tokens_df["ticker"].tolist())
        token = tokens_df[tokens_df["ticker"] == pick].iloc[0]

        largest_accounts = [
            {"uiAmount": token["total_supply"] * s}
            for s in [0.15, 0.08, 0.05, 0.04, 0.03, 0.02, 0.02, 0.01, 0.01, 0.01]
        ]
        scale = token["top10_holder_share"] / 0.42
        largest_accounts = [{"uiAmount": a["uiAmount"] * scale} for a in largest_accounts]

        safety = sg.run_safety_gate(
            mint_account_info={"value": {"data": {"parsed": {"info": {
                "mintAuthority": None if token["mint_authority_renounced"] else "SomeAuthorityPubkey",
                "freezeAuthority": None if token["freeze_authority_renounced"] else "SomeAuthorityPubkey",
            }}}}},
            largest_accounts=largest_accounts,
            total_supply=token["total_supply"],
            lp_locked=token["lp_locked"],
            simulated_sell_ok=token["sell_simulation_ok"],
        )

        for name, result in safety["checks"].items():
            icon = "✅" if result["passed"] else "❌"
            st.write(f"{icon} **{name.replace('_', ' ').title()}** — {result['reason']}")

        st.markdown("---")
        st.markdown(
            f"**Overall: {status_badge('PASS' if safety['passed'] else 'BLOCKED')}**",
            unsafe_allow_html=True,
        )


# --------------------------------------------------------------------------
# Tab 7: Token Inspector
# --------------------------------------------------------------------------

@st.cache_data(ttl=180, show_spinner=False)
def _fetch_token_inspection_data(mint_addr, rpc_url_cached):
    client = SolanaClient(rpc_url_cached)
    mint_info = supply_info = None
    largest = []
    try:
        mint_info = client.get_account_info(mint_addr)
        supply_info = client.get_token_supply(mint_addr)
        largest = client.get_token_largest_accounts(mint_addr)
    except SolanaRpcError as e:
        return {"error": str(e)}

    if not mint_info:
        return {"error": "no mint info"}

    rc_data = rc.get_lp_lock_and_honeypot(mint_addr)
    creator_addr = rc.get_creator_address(mint_addr)
    market_data = gpr.get_token_market_data(mint_addr)

    # Resolve owners of the top 10 largest token accounts (extra RPC calls,
    # kept small since this is a one-off single-token inspection).
    top_holder_owners = []
    for acc in (largest or [])[:10]:
        acc_addr = acc.get("address")
        if not acc_addr:
            continue
        try:
            info = client.get_account_info(acc_addr)
            owner = ((info or {}).get("data", {}).get("parsed", {}) or {}).get("info", {}).get("owner")
        except SolanaRpcError:
            owner = None
        top_holder_owners.append({
            "token_account": acc_addr,
            "owner": owner,
            "uiAmount": float(acc.get("uiAmount") or 0),
        })

    return {
        "error": None,
        "mint_info": mint_info,
        "supply_info": supply_info,
        "largest": largest,
        "rc_data": rc_data,
        "market_data": market_data,
        "top_holder_owners": top_holder_owners,
"creator_addr": creator_addr,
    }


with tabs[6]:
    st.subheader("Live token inspection")

    if mode == "Live (Solana RPC)":
        mint_addr = st.text_input("Token mint address")
        if st.button("Inspect token", type="primary") and mint_addr:
            if not rpc_url:
                st.error("Set a Solana RPC URL in the sidebar first.")
            else:
                with st.spinner("Fetching (cached 3 min per token)..."):
                    fetched = _fetch_token_inspection_data(mint_addr, rpc_url)

                if fetched.get("error"):
                    st.error(f"RPC error: {fetched['error']}")
                else:
                    mint_info = fetched["mint_info"]
                    supply_info = fetched["supply_info"]
                    largest = fetched["largest"]
                    rc_data = fetched["rc_data"]
                    market_data = fetched["market_data"]
                    top_holder_owners = fetched["top_holder_owners"]
                    creator_addr = fetched.get("creator_addr")

                    total_supply = float((supply_info or {}).get("uiAmount") or 0)
                    largest_amounts = [
                        {"uiAmount": float(a.get("uiAmount") or 0)} for a in largest
                    ]

                    safety = sg.run_safety_gate(
                        mint_account_info=mint_info,
                        largest_accounts=largest_amounts,
                        total_supply=total_supply,
                        lp_locked=rc_data["lp_locked"],
                        simulated_sell_ok=rc_data["honeypot_proxy"],
                    )

                    # Composite risk score: weighted, LP lock + honeypot heaviest.
                    WEIGHTS = {
                        "mint_authority": 15,
                        "freeze_authority": 15,
                        "holder_concentration": 20,
                        "lp_locked": 30,
                        "honeypot_proxy": 20,
                    }
                    score = sum(
                        WEIGHTS.get(name, 0)
                        for name, result in safety["checks"].items()
                        if result["passed"]
                    )
                    score_color = "🟢" if score >= 80 else "🟡" if score >= 50 else "🔴"
                    st.metric(f"{score_color} Composite Safety Score", f"{score}/100")
                    st.caption("Weighted: LP lock (30) + honeypot (20) heaviest, then holder concentration (20), mint/freeze authority (15 each).")
                    st.markdown("---")

                    for name, result in safety["checks"].items():
                        icon = "✅" if result["passed"] else "❌"
                        st.write(f"{icon} **{name.replace('_', ' ').title()}** — {result['reason']}")

                    if top_holder_owners:
                        SAVED_WALLETS_PATH_TI = "data/smart_money_wallets.txt"
                        known_wallets = {}
                        try:
                            with open(SAVED_WALLETS_PATH_TI, "r", encoding="utf-8") as f:
                                for line in f:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    if "," in line:
                                        label, address = line.split(",", 1)
                                        known_wallets[address.strip()] = label.strip()
                        except FileNotFoundError:
                            pass

                        with st.expander(f"Top {len(top_holder_owners)} holders (tap to view)"):
                            for h in top_holder_owners:
                                owner = h["owner"] or "unknown"
                                flag = f" 🐋 **known: {known_wallets[owner]}**" if owner in known_wallets else ""
                                st.write(f"- `{owner}` — {h['uiAmount']:,.0f} tokens{flag}")

                    if market_data.get("name") or market_data.get("symbol"):
                        st.markdown(f"### {market_data.get('name') or 'Unknown'} ({market_data.get('symbol') or '?'})")
                    if market_data.get("price_usd") is not None:
                        c1, c2, c3 = st.columns(3)
                        c1.metric("Price (USD)", f"${float(market_data['price_usd']):.8f}")
                        if market_data.get("market_cap_usd"):
                            c2.metric("Market Cap", f"${float(market_data['market_cap_usd']):,.0f}")
                        if market_data.get("liquidity_usd"):
                            c3.metric("Liquidity", f"${float(market_data['liquidity_usd']):,.0f}")
                    if market_data.get("volume_24h_usd") is not None:
                        st.caption(f"24h volume: ${float(market_data['volume_24h_usd']):,.0f}")
                        if market_data.get("liquidity_usd"):
                            vol_liq_ratio = float(market_data["volume_24h_usd"]) / float(market_data["liquidity_usd"])
                            ratio_flag = " ⚠️ high — thin liquidity relative to volume, price may be easy to move" if vol_liq_ratio > 10 else ""
                            st.caption(f"Volume/Liquidity ratio: {vol_liq_ratio:.1f}x{ratio_flag}")
                    if market_data.get("dex"):
                        st.caption(f"Traded on: {market_data['dex'].title()} (best liquidity pool)")
                    if market_data.get("pool_created_at"):
                        st.caption(f"Pool created: {market_data['pool_created_at']} (pool age, not necessarily token launch time)")
                    socials = []
                    if market_data.get("twitter"):
                        socials.append(f"[Twitter](https://twitter.com/{market_data['twitter']})")
                    if market_data.get("telegram"):
                        socials.append(f"[Telegram](https://t.me/{market_data['telegram']})")
                    if market_data.get("discord"):
                        socials.append(f"[Discord]({market_data['discord']})")
                    for site in (market_data.get("websites") or [])[:2]:
                        socials.append(f"[Website]({site})")
                    if socials:
                        st.markdown(" · ".join(socials))
                    st.caption("Market data via GeckoTerminal's free public API. Launchpad and exact token creation time are not directly available; DEX and pool-creation time shown above are the closest proxies.")
                    if rc_data.get("error"):
                        st.caption(f"⚠️ RugCheck lookup failed ({rc_data['error']}) — LP lock and honeypot proxy show as unknown/blocked above rather than a guess.")
                    else:
                        if rc_data.get("score") is not None:
                            st.caption(f"RugCheck aggregate risk score: {rc_data['score']}")
                        if rc_data.get("risks"):
                            with st.expander(f"RugCheck flagged {len(rc_data['risks'])} risk(s) — tap to view"):
                                for risk in rc_data["risks"]:
                                    if isinstance(risk, dict):
                                        st.write(f"- **{risk.get('name', 'Unknown risk')}** ({risk.get('level', 'n/a')})")
                        st.caption(
                            "Honeypot check is a proxy based on RugCheck's own risk flags, "
                            "not a live sell-simulation — treat it as one more data point."
                        )
                        st.markdown("---")
                        dh.render_dev_history(creator_addr)
    else:
        if tokens_df.empty:
            st.info("No token data loaded.")
        else:
            st.dataframe(tokens_df, use_container_width=True)
def _score_validation_tab():
    pass


with tabs[7]:
    st.subheader("Score validation")
    st.caption(
        "Checks scores against real token prices afterward using free "
        "GeckoTerminal data. Requires REAL mint addresses - Demo Mode's "
        "synthetic tokens won't return data."
    )
    st.info(
        "A correlation near zero is a legitimate result, not a bug. "
        "Small samples can look misleadingly strong by chance.",
        icon="⚠️",
    )

    if buys_df.empty:
        st.warning("No buy-event data loaded. Use CSV Upload with real mint addresses.")
    else:
        horizon_label = st.selectbox(
            "Check price this long after each buy",
            ["1 hour", "6 hours", "24 hours", "3 days"], index=0,
        )
        horizon_map = {"1 hour": 3600, "6 hours": 6*3600, "24 hours": 24*3600, "3 days": 3*24*3600}
        horizon_seconds = horizon_map[horizon_label]

        max_trades = st.slider("Max trades to check", min_value=3, max_value=30, value=10)
        st.caption(f"Roughly {max_trades*2}-{max_trades*3} API calls, rate-limited - may take a minute.")


    if st.button("Run validation", type="primary"):
                events = buys_df.copy()
                if "combined_score" not in events.columns:
                    events["combined_score"] = 50

                progress = st.progress(0.0, text="Fetching real prices...")

                def _update_progress(done, total):
                    progress.progress(done / total, text=f"Checked {done}/{total} trades...")

                evaluated = bt.evaluate_trades(
                    events.to_dict("records"), horizon_seconds=horizon_seconds,
                    max_trades=max_trades, progress_callback=_update_progress,
                )
                summary = bt.summarize_validation(evaluated)

                st.markdown("---")
                c1, c2, c3 = st.columns(3)
                c1.metric("Trades with price data", f"{summary['trades_with_price_data']}/{summary['total_trades']}")
                c2.metric("Correlation", summary["correlation"] if summary["correlation"] is not None else "n/a")
                c3.metric("Avg forward return", f"{summary['avg_forward_return_pct']}%" if summary["avg_forward_return_pct"] is not None else "n/a")
                st.write(summary["interpretation"])

                results_df = pd.DataFrame(evaluated)
                if not results_df.empty:
                    cols = [c for c in ["wallet", "ticker", "token_mint", "combined_score", "forward_return_pct"] if c in results_df.columns]
                    st.dataframe(results_df[cols], use_container_width=True)
                    chartable = results_df.dropna(subset=["forward_return_pct"])
                    if len(chartable) >= 2:
                        st.scatter_chart(chartable, x="combined_score", y="forward_return_pct")


# --------------------------------------------------------------------------
# Tab 9: Smart Money Convergence
# --------------------------------------------------------------------------

with tabs[8]:
    st.subheader("Smart money convergence")
    st.caption(
        "Paste a list of wallet addresses (KOLs, smart money, early catchers - "
        "one per line, or 'label,address'). We scan each wallet's recent "
        "activity for buys of ANY token, then flag tokens that multiple "
        "wallets bought within the same short window - a much stronger "
        "signal than any single wallet's activity."
    )

    if mode != "Live (Solana RPC)":
        st.info("Smart Money Convergence requires Live (Solana RPC) mode.")
    else:
        SAVED_WALLETS_PATH = "data/smart_money_wallets.txt"
        try:
            with open(SAVED_WALLETS_PATH, "r", encoding="utf-8") as f:
                saved_wallets_default = f.read()
        except FileNotFoundError:
            saved_wallets_default = ""

        wallet_list_text = st.text_area(
            "Wallet addresses (one per line, optional 'label,address' format)",
            value=saved_wallets_default,
            height=150,
            placeholder="ansem,ADDRESS_HERE\nAnotherWalletAddressHere...",
        )

        if st.button("💾 Save wallet list"):
            import os
            os.makedirs("data", exist_ok=True)
            with open(SAVED_WALLETS_PATH, "w", encoding="utf-8") as f:
                f.write(wallet_list_text)
            st.success("Wallet list saved. It will auto-load next time you open this tab.")
        sig_count = st.slider("Recent signatures to check per wallet", min_value=5, max_value=50, value=15)
        window_minutes = st.slider("Convergence window (minutes)", min_value=5, max_value=180, value=30)
        min_wallets_sm = st.slider("Minimum wallets to flag a cluster", min_value=2, max_value=10, value=2)

        BUY_EVENTS_LOG_PATH = "data/smart_money_buy_events.json"

        if st.button("Scan smart money wallets", type="primary"):
            raw_lines = [l.strip() for l in wallet_list_text.splitlines() if l.strip()]
            parsed_wallets = []
            for line in raw_lines:
                if "," in line:
                    label, addr = line.split(",", 1)
                    parsed_wallets.append((label.strip(), addr.strip()))
                else:
                    parsed_wallets.append((line.strip()[:6], line.strip()))

            if not parsed_wallets:
                st.warning("Paste at least one wallet address above.")
            else:
                client = SolanaClient(rpc_url)
                new_buy_events = []
                progress = st.progress(0.0, text="Scanning wallets...")

                for i, (label, addr) in enumerate(parsed_wallets):
                    progress.progress((i) / len(parsed_wallets), text=f"Scanning {label} ({i+1}/{len(parsed_wallets)})...")
                    try:
                        sigs = client.get_signatures_for_address(addr, limit=sig_count)
                    except SolanaRpcError:
                        continue
                    for sig_info in sigs or []:
                        sig = sig_info.get("signature") if isinstance(sig_info, dict) else sig_info
                        if not sig:
                            continue
                        try:
                            tx = client.get_transaction(sig)
                        except SolanaRpcError:
                            continue
                        buys = bs.detect_buys_any_token(tx, addr)
                        for b in buys:
                            new_buy_events.append({
                                "signature": sig,
                                "wallet": f"{label} ({addr[:4]}...{addr[-4:]})",
                                "token_mint": b["token_mint"],
                                "block_time": tx.get("blockTime"),
                                "combined_score": 50,
                            })

                progress.progress(1.0, text="Done.")

                import json
                import os
                os.makedirs("data", exist_ok=True)
                try:
                    with open(BUY_EVENTS_LOG_PATH, "r", encoding="utf-8") as f:
                        existing_events = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    existing_events = []

                seen_signatures = {e.get("signature") for e in existing_events}
                combined_events = existing_events + [e for e in new_buy_events if e["signature"] not in seen_signatures]

                with open(BUY_EVENTS_LOG_PATH, "w", encoding="utf-8") as f:
                    json.dump(combined_events, f)

                st.caption(
                    f"This scan found {len(new_buy_events)} buy events. Combined with prior scans, "
                    f"{len(combined_events)} total buy events are being checked for convergence "
                    f"(accumulated history persists across scans)."
                )

                if not combined_events:
                    st.info("No buy activity of any token found across these wallets yet.")
                else:
                    all_clusters = conv.detect_convergence(
                        combined_events,
                        window_seconds=window_minutes * 60,
                        min_wallets=2,
                    )
                    real_clusters = [c for c in all_clusters if c["wallet_count"] >= min_wallets_sm]
                    near_misses = [c for c in all_clusters if c["wallet_count"] < min_wallets_sm]

                    if not real_clusters:
                        st.info(
                            f"No token was bought by {min_wallets_sm}+ wallets within a {window_minutes}-minute "
                            "window yet. Try lowering the minimum wallets, widening the window, checking more "
                            "signatures, or just scanning again later - accumulated history grows each scan."
                        )
                    else:
                        st.success(f"Found {len(real_clusters)} convergence cluster(s)!")
                        for c in real_clusters:
                            st.markdown(f"### Token: `{c['token_mint']}`")
                            st.write(f"**{c['wallet_count']} wallets** bought within the window:")
                            for w in c["wallets"]:
                                st.write(f"- {w}")
                            st.caption(f"Window: {c['window_start']} to {c['window_end']} (unix time)")
                            st.markdown("---")

                    if near_misses:
                        with st.expander(f"{len(near_misses)} near-miss cluster(s) below your {min_wallets_sm}-wallet threshold"):
                            for c in near_misses:
                                st.write(f"**Token `{c['token_mint']}`** — {c['wallet_count']} wallet(s): {', '.join(c['wallets'])}")
