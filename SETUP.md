# AlpacaTradingAgent — Setup Guide

## Prerequisites

- Python 3.11+
- An [Alpaca Markets](https://alpaca.markets) account (paper trading is free)
- An [Anthropic](https://console.anthropic.com) API key (for gem research & post-mortem analysis)

---

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

---

## 2. Configure API keys

```bash
cp config.example.py config.py
```

Open `config.py` and fill in:

```python
ALPACA_API_KEY    = "your-api-key-id"      # From Alpaca Dashboard → API Keys
ALPACA_SECRET_KEY = "your-secret-key"
ALPACA_PAPER      = True                    # False for live trading

# Optional — for AI features:
ANTHROPIC_API_KEY = "sk-ant-..."
```

> **Important:** `config.py` is in `.gitignore` and will never be committed.
> Never share or publish your API keys.

### Where to get Alpaca keys

1. Sign up at [alpaca.markets](https://alpaca.markets)
2. Go to **Paper Trading** → **API Keys** for paper keys
3. Go to **Live Trading** → **API Keys** for live keys (requires funding + approval)
4. Copy the Key ID and Secret Key into `config.py`

---

## 3. Test connectivity

```bash
python connect_alpaca.py
```

Expected output:
```
[Alpaca] Connected — Account XXXXXXXX  status=ACTIVE  bp=$100,000.00  cash=$100,000.00
[Alpaca] Options level: 2  Paper: True
```

---

## 4. Run the agent

```bash
# Autonomous trading loop (scan + execute + monitor, repeating every 30 min)
python trade_main.py --loop

# Single scan + execute only
python trade_main.py --scan

# Monitor open trades only
python trade_main.py --monitor

# Status dashboard
python trade_main.py --status
```

---

## Key Differences from MoomooTrader

| Feature | MoomooTrader | AlpacaTradingAgent |
|---------|-------------|-------------------|
| Broker | Moomoo (requires OpenD gateway) | Alpaca (direct REST API) |
| Options spreads | Two separate legs; naked margin | **Single atomic multi-leg order; spread margin** |
| Markets | US + HK | US only |
| Setup | Install OpenD + run gateway | Just API keys in config.py |
| Paper trading | Moomoo simulator | Alpaca paper trading environment |
