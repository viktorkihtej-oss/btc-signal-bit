ema20_1h = ema(closes_1h, 20)
    ema50_1h = ema(closes_1h, 50)

    current_rsi = rsi(closes_15m)

    candle_time = candles_15m[-1]["time"]

    logging.info(
        "BTC %.2f | RSI %.2f | EMA20 %.2f | EMA50 %.2f",
        price,
        current_rsi,
        ema20,
        ema50
    )

    # LONG: напрямок на обох таймфреймах
    # і підтвердження імпульсу

    long_signal = (
        ema20 > ema50
        and ema20_1h > ema50_1h
        and price > ema20
        and 52 <= current_rsi <= 68
    )

    # SHORT: напрямок на обох таймфреймах
    # і підтвердження імпульсу

    short_signal = (
        ema20 < ema50
        and ema20_1h < ema50_1h
        and price < ema20
        and 32 <= current_rsi <= 48
    )

    if long_signal:
        direction = "LONG"

        stop = price * (1 - STOP_PERCENT)

        take = price + (
            price - stop
        ) * RISK_REWARD

    elif short_signal:
        direction = "SHORT"

        stop = price * (1 + STOP_PERCENT)

        take = price - (
            stop - price
        ) * RISK_REWARD

    else:
        return None

    return {
        "direction": direction,
        "price": price,
        "stop": stop,
        "take": take,
        "rsi": current_rsi,
        "candle_time": candle_time
    }


# ===== ФОРМУВАННЯ СИГНАЛУ =====

def format_signal(signal):
    direction = signal["direction"]

    if direction == "LONG":
        icon = "🟢"
    else:
        icon = "🔴"

    return (
        f"{icon} BTC SIGNAL\n\n"
        f"Напрямок: {direction}\n"
        f"Вхід орієнтовно: "
        f"${signal['price']:,.2f}\n"
        f"Stop Loss: "
        f"${signal['stop']:,.2f}\n"
        f"Take Profit: "
        f"${signal['take']:,.2f}\n\n"
        f"RSI: {signal['rsi']:.1f}\n"
        f"Risk/Reward: 1:2\n\n"
        f"Сигнал за закритими свічками.\n"
        f"Перевір актуальну ціну перед входом.\n"
        f"Не гарантує прибутку."
    )


# ===== ОСНОВНИЙ ЦИКЛ =====

def main():
    global last_signal

    logging.info("BTC Signal Bot starting")

    send_message(
        "✅ BTC Signal Bot запущений!\n\n"
        "Моніторинг BTC активний.\n"
        "Таймфрейми: 15m та 1h.\n"
        "Перевірка кожні 5 хвилин.\n"
        "Сигнали: LONG / SHORT."
    )

    while True:
        try:
            signal = analyze()

            if signal:
                signal_id = (
                    signal["direction"],
                    signal["candle_time"]
                )

                if signal_id != last_signal:
                    message = format_signal(signal)

                    if send_message(message):
                        last_signal = signal_id

            else:
                logging.info(
                    "No confirmed signal"
                )

        except Exception as error:
            logging.exception(
                "Analysis error: %s",
                error
            )

        time.sleep(CHECK_SECONDS)


if name == "main":
    main()
