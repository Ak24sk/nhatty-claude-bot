"""
Solana Memecoin Signal Bot — v2.2
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

CUSTOM_CSS = """
<style>
/* Gradient header glow */
h1, h2, h3 {
    background: linear-gradient(90deg, #7C3AED, #06B6D4);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-weight: 800 !important;
    letter-spacing: 0.5px;
}

/* Sidebar polish */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #10142240, #0B0E17);
    border-right: 1px solid #7C3AED33;
}

/* Buttons: gradient + glow on hover */
.stButton > button {
    background: linear-gradient(90deg, #7C3AED, #06B6D4);
    color: white;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    transition: 0.2s ease-in-out;
    box-shadow: 0 0 0px #7C3AED00;
}
.stButton > button:hover {
    box-shadow: 0 0 16px #7C3AEDaa;
    transform: translateY(-1px);
}

/* Metric cards */
div[data-testid="stMetric"] {
    background: #151A2B;
    border: 1px solid #7C3AED33;
    border-radius: 12px;
    padding: 12px 16px;
}

/* Tabs underline glow */
button[data-baseweb="tab"][aria-selected="true"] {
    border-bottom: 2px solid #06B6D4 !important;
    color: #06B6D4 !important;
}

/* Success/warning/error boxes: subtle glow border */
div[data-testid="stAlert"] {
    border-radius: 10px;
    border-left: 3px solid #06B6D4;
}
</style>
"""
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
            "Live Mode scores a watchlist you provide against the real Solana "
            "chain. Upload a CSV with columns: wallet, token_mint, ticker.",
            icon="🛰️",
        )
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    watchlist = dd.load_csv(wl_file)
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
        addr = st.text_input("Wallet address")
        mint = st.text_input("Token mint to check buys/sells against")
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
                        trades_df = pd.DataFrame([t.__dict__ for t in trades])
                        st.dataframe(trades_df, use_container_width=True)
                        st.json(bs.summarize_trades(trades))
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

with tabs[6]:
    st.subheader("Live token inspection")

    if mode == "Live (Solana RPC)":
        mint_addr = st.text_input("Token mint address")
        if st.button("Inspect token", type="primary") and mint_addr:
            if not rpc_url:
                st.error("Set a Solana RPC URL in the sidebar first.")
            else:
                client = SolanaClient(rpc_url)
                with st.spinner("Reading mint + holder data..."):
                    try:
                        mint_info = client.get_account_info(mint_addr)
                        supply_info = client.get_token_supply(mint_addr)
                        largest = client.get_token_largest_accounts(mint_addr)
                    except SolanaRpcError as e:
                        st.error(f"RPC error: {e}")
                        mint_info, supply_info, largest = None, None, []

                if mint_info:
                    total_supply = float((supply_info or {}).get("uiAmount") or 0)
                    largest_amounts = [
                        {"uiAmount": float(a.get("uiAmount") or 0)} for a in largest
                    ]

                    with st.spinner("Checking RugCheck for LP-lock + risk signals..."):
                        rc_data = rc.get_lp_lock_and_honeypot(mint_addr)
                        with st.spinner("Fetching market data..."):
                            market_data = gpr.get_token_market_data(mint_addr)

                    safety = sg.run_safety_gate(
                        mint_account_info=mint_info,
                        largest_accounts=largest_amounts,
                        total_supply=total_supply,
                        lp_locked=rc_data["lp_locked"],
                        simulated_sell_ok=rc_data["honeypot_proxy"],
                    )
                    for name, result in safety["checks"].items():
                        icon = "✅" if result["passed"] else "❌"
                        st.write(f"{icon} **{name.replace('_', ' ').title()}** — {result['reason']}")

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
