import asyncio
import logging
import time
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.client.default import DefaultBotProperties
import aiohttp
import os
from aiogram import Bot, Dispatcher, executor

# ─── КОНФІГ ───────────────────────────────────────────────
BOT_TOKEN = "8165011916:AAFIgE8wSNk1Z7SlcCCXOe28pGNG4_LlJ98"
RSI_PERIOD = 14
KLINE_LIMIT = 50

_global_fg_cache = {"value": -1, "label": "недоступно", "updated": 0}


# ─── ІНДИКАТОРИ ───────────────────────────────────────────
def calc_rsi(closes: list[float], period: int = RSI_PERIOD) -> float:
    if len(closes) < period + 1:
        return 50.0

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_volatility(closes: list[float], window: int = 12) -> float:
    if len(closes) < window + 1:
        return 0.0

    pct_changes = []
    for i in range(-window, 0):
        pct = (closes[i] - closes[i - 1]) / closes[i - 1] * 100
        pct_changes.append(pct)

    mean = sum(pct_changes) / len(pct_changes)
    variance = sum((x - mean) ** 2 for x in pct_changes) / len(pct_changes)
    return variance ** 0.5


def analyze_volume(volumes: list[float], closes: list[float]) -> tuple[str, float]:
    if len(volumes) < 20:
        return ("недостатньо даних", 1.0)

    avg_vol = sum(volumes[-20:-1]) / 19
    last_vol = volumes[-1]
    ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    price_change = ((closes[-1] - closes[-4]) / closes[-4] * 100) if len(closes) > 4 else 0

    if ratio > 1.8 and price_change > 0:
        return (f"x{ratio:.1f} — сильний імпульс вгору 🔥", ratio)
    elif ratio > 1.8 and price_change < 0:
        return (f"x{ratio:.1f} — сильний продаж 📉", ratio)
    elif ratio > 1.3:
        return (f"x{ratio:.1f} — вище середнього", ratio)
    else:
        return (f"x{ratio:.1f} — у нормі", ratio)


def calc_coin_fear_greed(rsi: float, vol_ratio: float, volatility: float) -> tuple[int, str, str]:
    if rsi > 75:
        base = 85
    elif rsi > 65:
        base = 72
    elif rsi > 55:
        base = 60
    elif rsi > 45:
        base = 50
    elif rsi > 35:
        base = 38
    elif rsi > 25:
        base = 25
    else:
        base = 12

    if vol_ratio > 1.8:
        base = base + (10 if base > 50 else -10)
    elif vol_ratio > 1.3:
        base = base + (5 if base > 50 else -5)

    if volatility > 3.0:
        base = max(0, base - 8)
    elif volatility > 2.0:
        base = max(0, base - 4)

    base = max(0, min(100, base))

    if base >= 75:
        return (base, "Крайня жадібність", "🔴")
    elif base >= 60:
        return (base, "Жадібність", "🟠")
    elif base >= 40:
        return (base, "Нейтрально", "⚪️")
    elif base >= 25:
        return (base, "Страх", "🟡")
    else:
        return (base, "Крайній страх", "🟢")


# ─── API ТА КЕШ (ДЛЯ RENDER ПЕРЕВЕДЕНО НА BYBIT) ───────────
async def get_cached_global_fg() -> tuple[int, str]:
    now = time.time()
    if now - _global_fg_cache["updated"] < 600 and _global_fg_cache["value"] != -1:
        return _global_fg_cache["value"], _global_fg_cache["label"]

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://api.alternative.me/fng/") as resp:
                if resp.status == 200:
                    res = await resp.json()
                    if "data" in res and len(res["data"]) > 0:
                        val = int(res["data"][0]["value"])
                        label = res["data"][0]["value_classification"]
                        _global_fg_cache["value"] = val
                        _global_fg_cache["label"] = label
                        _global_fg_cache["updated"] = now
        return _global_fg_cache["value"], _global_fg_cache["label"]
    except Exception as e:
        logging.error(f"Помилка отримання глобального індексу: {e}")
        return _global_fg_cache["value"], _global_fg_cache["label"]


async def fetch_klines(symbol: str) -> dict:
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": "60",  # 1 година
        "limit": KLINE_LIMIT
    }

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    raise ValueError(f"Сервер Bybit відповів кодом {resp.status}")
                res = await resp.json()

        if "result" not in res or "list" not in res["result"] or len(res["result"]["list"]) == 0:
            raise ValueError("Такої монети не існує або на біржі немає торгової пари (наприклад, треба вводити BTCUSDT)")

        raw_klines = res["result"]["list"]
        raw_klines.reverse()

        closes = [float(k[4]) for k in raw_klines]
        volumes = [float(k[5]) for k in raw_klines]
        current_price = closes[-1]

        rsi = calc_rsi(closes)
        vol_comment, vol_ratio = analyze_volume(volumes, closes)
        volatility = calc_volatility(closes)
        fg_score, fg_label, fg_emoji = calc_coin_fear_greed(rsi, vol_ratio, volatility)

        return {
            "price": current_price,
            "rsi": rsi,
            "volume_info": vol_comment,
            "vol_ratio": vol_ratio,
            "volatility": volatility,
            "fg_score": fg_score,
            "fg_label": fg_label,
            "fg_emoji": fg_emoji,
            "symbol": symbol,
        }
    except Exception as e:
        logging.error(f"Помилка fetch_klines для {symbol}: {e}")
        raise e


# ─── ПОРАДА ───────────────────────────────────────────────
def generate_advice(data: dict, global_fg: tuple[int, str]) -> str:
    rsi = data["rsi"]
    price = data["price"]
    vol = data["volume_info"]
    vol_ratio = data["vol_ratio"]
    fg_score = data["fg_score"]
    fg_label = data["fg_label"]
    fg_emoji = data["fg_emoji"]
    volatility = data["volatility"]

    if rsi > 75:
        signal = "🔴 ПЕРЕКУПЛЕНО — високий ризик відкату"
    elif rsi > 65:
        signal = "🟠 RSI високий — можлива консолідація"
    elif rsi < 25:
        signal = "🟢 ПЕРЕПРОДАНО — можливе відновлення"
    elif rsi < 35:
        signal = "🟡 RSI низький — шукай розворот"
    elif 45 <= rsi <= 55:
        signal = "⚪️ Нейтральний — без чіткого напрямку"
    elif rsi > 55:
        signal = "🟢 Позитивний імпульс"
    else:
        signal = "🔻 Ведмежий тиск"

    global_fg_text = ""
    if global_fg[0] >= 0:
        g_val, g_class = global_fg
        if g_val >= 75:
            g_emoji = "🔴"
        elif g_val >= 60:
            g_emoji = "🟠"
        elif g_val >= 40:
            g_emoji = "⚪️"
        elif g_val >= 25:
            g_emoji = "🟡"
        else:
            g_emoji = "🟢"
        global_fg_text = f"🌍 <b>Ринок:</b> {g_emoji} {g_class} ({g_val})\n"

    if fg_score >= 70 and rsi > 65:
        tip = "⚠️ Жадібність + перекупленість — обережно з лонгами"
    elif fg_score <= 30 and rsi < 35:
        tip = "💡 Страх + перепроданість — можлива точка входу"
    elif fg_score >= 60 and vol_ratio > 1.5:
        tip = "🔥 Жадібність підтверджена об'ємом — імпульс сильний"
    elif fg_score <= 40 and volatility > 2.5:
        tip = "🌊 Страх + висока волатильність — ринок нервує"
    else:
        tip = "📊 Спостерігай за підтвердженням сигналу"

    return (
        f"💰 <b>{data['symbol']}</b>\n"
        f"Ціна: <code>${price:,.2f}</code>\n"
        f"RSI(14): <code>{rsi:.1f}</code>\n"
        f"Волатильність: <code>{volatility:.2f}%</code>\n"
        f"Об'єм: {vol}\n\n"
        f"{fg_emoji} <b>Індекс монети:</b> {fg_score}/100 — {fg_label}\n"
        f"{global_fg_text}"
        f"📊 <b>Сигнал:</b> {signal}\n\n"
        f"💬 {tip}\n\n"
        f"⏱️ <i>Прогноз на 6 годин. Не фінансова порада — завжди роби власний аналіз (DYOR).</i>"
    )


# ─── БОТ ──────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN, default_properties=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


# Створюємо зручні шаблони кнопок внизу екрана, щоб користувач міг клікнути, або ввести свою
def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="BTCUSDT"), KeyboardButton(text="ETHUSDT")],
        [KeyboardButton(text="TONUSDT"), KeyboardButton(text="SOLUSDT")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    global_fg = await get_cached_global_fg()
    fg_text = ""
    if global_fg[0] >= 0:
        fg_text = f"\n🌍 Ринок зараз: {global_fg[1]} ({global_fg[0]}/100)"

    await message.answer(
        f"🤖 <b>Trading Advisor Bot</b>{fg_text}\n\n"
        f"✍️ <b>Введи назву будь-якої монети текстом</b> (наприклад: <code>sol</code>, <code>btc</code>, <code>ada</code> або повну пару <code>ETHUSDT</code>):\n\n"
        f"Або обери швидкий варіант із меню нижче 👇",
        reply_markup=get_main_reply_keyboard()
    )


# Обробник БУДЬ-ЯКОГО тексту, який надсилає користувач
@dp.message(F.text)
async def handle_any_coin(message: types.Message):
     user_input = message.text.strip().upper()

     if not user_input.endswith("USDT") and not user_input.endswith("USDC"):
         symbol = f"{user_input}USDT"
     else:
         symbol = user_input

     status_msg = await message.answer(f"⏳ Аналізую торгову пару <b>{symbol}</b>...", parse_mode="HTML")

     try:
         data = await fetch_klines(symbol)
         global_fg = await get_cached_global_fg()
         advice = generate_advice(data, global_fg)

         # Надсилаємо аналіз і повністю стираємо кнопки знизу
         await message.answer(advice, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
     except Exception as e:
         # У разі помилки також ховаємо кнопки
         await message.answer(
             f"❌ <b>Помилка аналізу {symbol}</b>\n\n"
             f"Переконайся, що ти ввів назву правильно.\n"
             f"<i>Опис помилки: {e}</i>",
             reply_markup=ReplyKeyboardRemove(),
             parse_mode="HTML"
         )
     finally:
         try:
             await status_msg.delete()
         except Exception:
             pass


async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)


if __name__ == '__main__':
    # Render автоматично дає порт у змінну оточення PORT. Якщо її немає — беремо 10000
    PORT = int(os.environ.get("PORT", 10000))
    
    # Замість start_polling використовуємо інший запуск, який відкриває порт для Render
    # але все одно працює як звичайний бот
    print(f"Запуск бота на порту {PORT}...")
    executor.start_polling(dp, skip_updates=True)
