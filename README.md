# Nifty weekly options machine

Automated Nifty options desk. It does not scan Nifty 50 stocks.

After 09:30 IST it buys **one** weekly ATM option when Nifty breaks the 09:15–09:30 opening range:

- break up → buy the ATM **call**
- break down → buy the ATM **put**
- still inside the range → no trade

The position is sold on a premium stop, a premium target, or at **15:15 IST**. Short options are never sold to open. One new option per day. Lot size comes from the Upstox contract (currently 65).

```powershell
python -m streamlit run app.py
```

## Modes

- **Paper trade** — local book in `data/simulated_portfolio.json`. No broker order.
- **Live trade** — Upstox MIS market order only when **Arm live option orders** is on and `.env` has `EXECUTION_MODE=LIVE`. `MOCK` blocks every live POST.

Set **starting capital** and **risk %**. A lot is taken only when the premium and the planned stop both fit that budget and the ₹3,000 daily-loss cap.

The **Backtest** tab replays the same rule on real premiums for the current weekly expiry (about three weeks on this token). Expired older weeks are not readable.

## Tests

```powershell
pytest tests/test_options.py tests/test_option_backtest.py tests/test_option_quality.py tests/test_dashboard_smoke.py tests/test_isolation.py tests/test_trade_journal.py
```
