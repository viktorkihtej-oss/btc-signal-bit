send_message(
                    f"{signal} BTC\n\n"
                    f"Ціна: ${price:,.2f}\n"
                    f"EMA20: {ema20:,.2f}\n"
                    f"EMA50: {ema50:,.2f}\n"
                    f"RSI14: {rsi14:.1f}\n"
                    f"Volume: {current_volume:.2f}\n"
                    f"Свічка: {time_text}"
                )

            last_candle_time = candle_time

    except Exception as e:
        print(
            "ERROR:",
            repr(e),
            flush=True
        )

    time.sleep(60)
