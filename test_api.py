import requests

tests = {
    "BITGET":
        "https://api.bitget.com/api/v2/mix/market/ticker?symbol=BTCUSDT&productType=USDT-FUTURES",

    "OKX":
        "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP",
}

for name, url in tests.items():
    try:
        r = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": "BTCSignalBot/3.0"
            }
        )

        print(
            name,
            "STATUS:",
            r.status_code
        )

        print(
            name,
            "DATA:",
            r.text[:300]
        )

    except Exception as e:
        print(
            name,
            "ERROR:",
            e
        )
