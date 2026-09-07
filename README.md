# Solana Memecoin Signal Bot — v2.2

A research/alerting dashboard for Solana memecoins. **It only alerts — it
never trades.**

- ✅ No private keys requested or stored, anywhere
- ✅ No trade execution — read-only Solana RPC calls only
- ✅ Every score is a heuristic research signal, not financial advice

## What it does

| Feature | Where |
|---|---|
| Live Solana wallet scanner | `modules/solana_client.py`, Wallet Scanner tab |
| Wallet transaction history | same |
| Likely buy/sell detection | `modules/buy_sell.py` |
| Wallet intelligence scoring | `modules/wallet_intel.py` |
| Early-runner trader scoring | `modules/wallet_intel.py` |
| Wallet convergence detection | `modules/convergence.py` |
| X attention engine | `modules/x_attention.py` |
| Safety Gate (mint/freeze authority, holder concentration, LP lock, honeypot) | `modules/safety_gate.py` |
| Unified BLOCKED / WATCH / BUILDING / ALERT status | `modules/safety_gate.py::unified_status` |
| Demo + CSV modes | `modules/demo_data.py`, sidebar mode switch |
| Live token inspection | Token Inspector tab |

## Quick start (local)

```bash
git clone <your-repo-url>
cd memecoin-signal-bot
python -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
streamlit run app.py
```

Open the URL Streamlit prints (usually `http://localhost:8501`). Pick
**Demo Mode** in the sidebar first — it needs zero setup and generates
synthetic wallets, tokens, and buy events so you can click through every tab.

## Deploying to Streamlit Community Cloud

1. Push this folder to a **public or private GitHub repo**.
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**.
3. Pick your repo, branch `main`, and main file path `app.py`.
4. Click **Deploy**. That's it for Demo Mode / CSV Mode.
5. For **Live Mode**, open the deployed app's *Settings → Secrets* and paste:

   ```toml
   SOLANA_RPC_URL = "https://your-rpc-provider.com/your-key"
   X_BEARER_TOKEN = ""
   ```

   Never commit a real `secrets.toml` — it's gitignored on purpose. Use
   `.streamlit/secrets.toml.example` as a template for local testing only.

## The three modes

- **Demo Mode** — fully synthetic data (`modules/demo_data.py`). Good for
  demoing the UI or developing new features without hitting any API.
- **CSV Upload** — bring your own `wallets.csv` / `tokens.csv` /
  `buy_events.csv` (matching the sample files in `data/`). Good for
  batch-analyzing wallets/tokens you've already collected some other way
  (e.g. exported from a spreadsheet, a paid data provider, or a prior run).
- **Live (Solana RPC)** — calls the real public Solana JSON-RPC API (or your
  own RPC URL) for wallet scanning and token inspection. The public endpoint
  (`api.mainnet-beta.solana.com`) is free but rate-limited and not meant for
  serious volume — get a free tier key from **Helius**, **QuickNode**, or
  **Triton** for anything beyond light testing.

## Honest limitations (read before treating any status as ground truth)

A few features are intentionally stubbed with clear extension points,
because they require either a paid API or a bespoke on-chain integration
that goes beyond generic RPC reads:

- **LP lock detection** (`safety_gate.check_lp_lock`) — real LP-lock status
  lives inside a specific locker program's account state (e.g. Streamflow,
  Team Finance, Bagsapp). This function accepts the lock status as a plain
  `True`/`False`/`None` input so you can wire it up to whichever locker
  reader or aggregator API (e.g. RugCheck) you trust.
- **Honeypot / sell-simulation** (`safety_gate.check_honeypot_risk`) —
  detecting a honeypot reliably requires simulating a sell transaction
  against the token's actual DEX pool (or calling a dedicated
  honeypot-checker API). This function takes the simulated result as an
  input for the same reason.
- **X attention engine** (`modules/x_attention.py`) — the official X API v2
  "recent counts" endpoint requires a paid API tier and a bearer token. Without
  one, the app uses a clearly-labeled synthetic timeline so the UI is always
  explorable. We do **not** scrape X/Twitter — that would violate its Terms
  of Service.
- **Wallet intelligence / early-runner scores** are heuristics built purely
  from observable on-chain behavior (trade frequency, buy/sell
  follow-through, entry timing) — they are not a P&L or "smart money" label
  from any paid analytics vendor. Treat them as a triage aid, not a verdict.

## Extending live mode

The natural next integrations, in rough order of value:

1. Wire `check_lp_lock` / `check_honeypot_risk` to a token-safety aggregator
   (e.g. RugCheck's public API) instead of passing `None`.
2. Add a persistent watchlist (e.g. a small SQLite file or a Google Sheet)
   so Live Mode doesn't require re-uploading a CSV every session.
3. Add a scheduled background refresh (e.g. via GitHub Actions hitting a
   webhook, or `streamlit-autorefresh`) plus a notification channel
   (Discord/Telegram webhook) for when a token flips to `ALERT`.
4. Swap the public Solana RPC calls for a paid provider's enhanced APIs
5. Fix build_real_dataset.py's wallet sampling: pull real recent buyer wallets from the pool's own recent swap transactions instead of from "top holders" - this is the single biggest lever for getting a real, statistically usable sample size into the Score Validation tab.
   (e.g. Helius's parsed transaction history endpoint) to avoid manually
   walking `getSignaturesForAddress` + `getTransaction` one at a time.

## Project structure

```
memecoin-signal-bot/
├── app.py                     # Streamlit UI — all tabs
├── requirements.txt
├── .streamlit/
│   ├── config.toml            # theme
│   └── secrets.toml.example   # template — copy to secrets.toml locally
├── modules/
│   ├── solana_client.py       # read-only Solana RPC wrapper
│   ├── buy_sell.py            # buy/sell classification from tx history
│   ├── wallet_intel.py        # wallet intelligence + early-runner scoring
│   ├── convergence.py         # multi-wallet convergence detection
│   ├── x_attention.py         # X attention engine (demo + live hook)
│   ├── safety_gate.py         # safety checks + unified status
│   └── demo_data.py           # synthetic data generator + CSV loader
└── data/
    ├── sample_wallets.csv
    ├── sample_tokens.csv
    └── sample_buy_events.csv
```

## Disclaimer

This tool surfaces publicly observable on-chain and social data through
heuristic scoring. It does not execute trades, does not custody funds, and
does not constitute financial advice. Memecoins are extremely high risk;
always do your own research.
