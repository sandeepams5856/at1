"""The tradable catalog shown in the right-hand selector.

Every Yahoo symbol in here was probed for real OHLCV history before being added -
Yahoo silently serves sector indices (^CNXAUTO, ^CNXFMCG, NIFTY_FIN_SERVICE.NS ...)
with intraday bars but only one daily bar, which makes 200-period averages
meaningless, so those are deliberately left out.

NSE option chains are keyed by the exchange's own underlying code (NIFTY,
BANKNIFTY, RELIANCE ...), not by the Yahoo ticker, so the mapping lives here too.
"""

from __future__ import annotations

# --- price groups -----------------------------------------------------------
# kind: index | equity | commodity | fx | crypto  (drives the units shown in the UI)

INDICES = [
    ("^NSEI", "NIFTY 50", "NSE"),
    ("^NSEBANK", "NIFTY BANK", "NSE"),
    ("^BSESN", "SENSEX", "BSE"),
    ("^NSMIDCP", "NIFTY NEXT 50", "NSE"),
    ("^NSEMDCP50", "NIFTY MIDCAP 50", "NSE"),
    ("^CRSLDX", "NIFTY 500", "NSE"),
    ("^CNXIT", "NIFTY IT", "NSE"),
    ("^CNXPHARMA", "NIFTY PHARMA", "NSE"),
    ("^INDIAVIX", "INDIA VIX", "NSE"),
]

# Yahoo carries the global benchmark contracts that MCX futures track almost
# tick-for-tick; MCX itself has no free feed. Prices are in USD (INR for =X).
COMMODITIES = [
    ("NG=F", "NATURAL GAS (Henry Hub)", "NYMEX"),
    ("CL=F", "CRUDE OIL (WTI)", "NYMEX"),
    ("BZ=F", "CRUDE OIL (Brent)", "ICE"),
    ("GC=F", "GOLD", "COMEX"),
    ("SI=F", "SILVER", "COMEX"),
    ("HG=F", "COPPER", "COMEX"),
    ("ZN=F", "ZINC / 10Y note proxy", "CME"),
]

FX = [
    ("USDINR=X", "USD / INR", "FX"),
    ("EURINR=X", "EUR / INR", "FX"),
    ("GBPINR=X", "GBP / INR", "FX"),
    ("JPYINR=X", "JPY / INR", "FX"),
]

# Crypto trades 24x7, so Yahoo has full history on every timeframe. BTC-USD is
# the global reference price; BTC-INR is the same coin quoted in rupees.
CRYPTO = [
    ("BTC-USD", "BITCOIN / USD", "CRYPTO"),
    ("BTC-INR", "BITCOIN / INR", "CRYPTO"),
]

# Nifty-50 plus the most active F&O names. `_NSE_STOCKS` holds the bare NSE
# code; `.NS` is appended for Yahoo and `.BO` for the BSE listing of the same
# company, so one list feeds both exchange groups.
_NSE_STOCKS = [
    ("RELIANCE", "Reliance Industries"),
    ("HDFCBANK", "HDFC Bank"),
    ("ICICIBANK", "ICICI Bank"),
    ("INFY", "Infosys"),
    ("TCS", "Tata Consultancy Svcs"),
    ("BHARTIARTL", "Bharti Airtel"),
    ("ITC", "ITC"),
    ("SBIN", "State Bank of India"),
    ("LT", "Larsen & Toubro"),
    ("HINDUNILVR", "Hindustan Unilever"),
    ("BAJFINANCE", "Bajaj Finance"),
    ("HCLTECH", "HCL Technologies"),
    ("AXISBANK", "Axis Bank"),
    ("KOTAKBANK", "Kotak Mahindra Bank"),
    ("MARUTI", "Maruti Suzuki"),
    ("SUNPHARMA", "Sun Pharma"),
    ("M&M", "Mahindra & Mahindra"),
    ("NTPC", "NTPC"),
    ("TATAMOTORS", "Tata Motors"),
    ("TITAN", "Titan Company"),
    ("ULTRACEMCO", "UltraTech Cement"),
    ("ASIANPAINT", "Asian Paints"),
    ("POWERGRID", "Power Grid Corp"),
    ("ADANIENT", "Adani Enterprises"),
    ("ADANIPORTS", "Adani Ports"),
    ("COALINDIA", "Coal India"),
    ("BAJAJFINSV", "Bajaj Finserv"),
    ("NESTLEIND", "Nestle India"),
    ("ONGC", "ONGC"),
    ("WIPRO", "Wipro"),
    ("JSWSTEEL", "JSW Steel"),
    ("TATASTEEL", "Tata Steel"),
    ("GRASIM", "Grasim Industries"),
    ("HDFCLIFE", "HDFC Life"),
    ("SBILIFE", "SBI Life"),
    ("TECHM", "Tech Mahindra"),
    ("DRREDDY", "Dr Reddy's Labs"),
    ("CIPLA", "Cipla"),
    ("HINDALCO", "Hindalco"),
    ("INDUSINDBK", "IndusInd Bank"),
    ("BRITANNIA", "Britannia"),
    ("EICHERMOT", "Eicher Motors"),
    ("APOLLOHOSP", "Apollo Hospitals"),
    ("BPCL", "BPCL"),
    ("HEROMOTOCO", "Hero MotoCorp"),
    ("BAJAJ-AUTO", "Bajaj Auto"),
    ("TATACONSUM", "Tata Consumer"),
    ("SHRIRAMFIN", "Shriram Finance"),
    ("TRENT", "Trent"),
    ("BEL", "Bharat Electronics"),
    ("VEDL", "Vedanta"),
    ("DLF", "DLF"),
    ("ZYDUSLIFE", "Zydus Lifesciences"),
    ("IOC", "Indian Oil Corp"),
    ("PFC", "Power Finance Corp"),
    ("IRFC", "IRFC"),
    ("TATAPOWER", "Tata Power"),
    ("CANBK", "Canara Bank"),
    ("PNB", "Punjab National Bank"),
    ("IDFCFIRSTB", "IDFC First Bank"),
]

# Companies whose BSE line is liquid enough that its own candles are worth
# charting; the rest of the list would just mirror NSE on thinner volume.
_BSE_SUBSET = {
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "ITC",
    "BHARTIARTL", "LT", "AXISBANK", "TATAMOTORS", "TATASTEEL", "MARUTI",
    "SUNPHARMA", "WIPRO", "ONGC", "COALINDIA", "NTPC", "ADANIENT", "BEL",
}


def _rows(pairs, suffix, exchange, kind="equity"):
    return [
        {
            "symbol": f"{code}{suffix}",
            "name": name,
            "exchange": exchange,
            "kind": kind,
            "nse_code": code,
        }
        for code, name in pairs
    ]


def _index_rows(rows, kind):
    return [
        {"symbol": s, "name": n, "exchange": ex, "kind": kind, "nse_code": None}
        for s, n, ex in rows
    ]


GROUPS = [
    {
        "id": "indices",
        "label": "Indices",
        "note": "NSE / BSE benchmarks",
        "items": _index_rows(INDICES, "index"),
    },
    {
        "id": "nse",
        "label": "NSE Stocks",
        "note": "Nifty 50 + active F&O names",
        "items": _rows(_NSE_STOCKS, ".NS", "NSE"),
    },
    {
        "id": "bse",
        "label": "BSE Stocks",
        "note": "BSE listing of the large caps",
        "items": _rows(
            [(c, n) for c, n in _NSE_STOCKS if c in _BSE_SUBSET], ".BO", "BSE"
        ),
    },
    {
        "id": "commodities",
        "label": "Commodities",
        "note": "Global contracts MCX tracks",
        "items": _index_rows(COMMODITIES, "commodity"),
    },
    {
        "id": "fx",
        "label": "Currency",
        "note": "INR pairs",
        "items": _index_rows(FX, "fx"),
    },
    {
        "id": "crypto",
        "label": "Crypto",
        "note": "Bitcoin, 24x7",
        "items": _index_rows(CRYPTO, "crypto"),
    },
]

BY_SYMBOL = {item["symbol"]: item for g in GROUPS for item in g["items"]}


# --- option chain underlyings ----------------------------------------------
# `type` is what NSE's option-chain-v3 endpoint expects.

OPTION_INDEX_UNDERLYINGS = [
    ("NIFTY", "Nifty 50", "^NSEI"),
    ("BANKNIFTY", "Nifty Bank", "^NSEBANK"),
    ("FINNIFTY", "Nifty Financial Services", None),
    ("MIDCPNIFTY", "Nifty Midcap Select", None),
    ("NIFTYNXT50", "Nifty Next 50", "^NSMIDCP"),
]

_OPTION_STOCKS = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "ITC",
    "BHARTIARTL", "AXISBANK", "KOTAKBANK", "LT", "TATAMOTORS", "TATASTEEL",
    "MARUTI", "SUNPHARMA", "BAJFINANCE", "HINDUNILVR", "ADANIENT",
    "ADANIPORTS", "WIPRO", "HCLTECH", "TECHM", "ONGC", "COALINDIA", "NTPC",
    "POWERGRID", "TITAN", "ASIANPAINT", "JSWSTEEL", "HINDALCO", "VEDL",
    "INDUSINDBK", "DLF", "BEL", "TATAPOWER", "PNB", "CANBK", "IRFC",
]

_STOCK_NAMES = dict(_NSE_STOCKS)

OPTION_UNDERLYINGS = [
    {
        "code": code,
        "name": name,
        "type": "Indices",
        "yahoo": yahoo,
        "lot": None,
    }
    for code, name, yahoo in OPTION_INDEX_UNDERLYINGS
] + [
    {
        "code": code,
        "name": _STOCK_NAMES.get(code, code),
        "type": "Equity",
        "yahoo": f"{code}.NS",
        "lot": None,
    }
    for code in _OPTION_STOCKS
]

OPTIONS_BY_CODE = {u["code"]: u for u in OPTION_UNDERLYINGS}


def resolve(symbol: str) -> dict:
    """Catalog entry for a Yahoo symbol; synthesised for anything hand-typed."""
    hit = BY_SYMBOL.get(symbol)
    if hit:
        return hit
    kind = "index" if symbol.startswith("^") else (
        "commodity" if symbol.endswith("=F") else
        "fx" if symbol.endswith("=X") else
        "crypto" if symbol.endswith(("-USD", "-INR")) else "equity"
    )
    exchange = "NSE" if symbol.endswith(".NS") else "BSE" if symbol.endswith(".BO") else "-"
    return {
        "symbol": symbol,
        "name": symbol,
        "exchange": exchange,
        "kind": kind,
        "nse_code": symbol.split(".")[0] if "." in symbol else None,
    }
