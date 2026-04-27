import asyncio
import logging
import json
import time
import aiohttp
from datetime import datetime
from typing import Optional, List, Dict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)

# ─── НАСТРОЙКИ ───────────────────────────────────────────────────────────────
BOT_TOKEN = "8784977364:AAEQgiFJOGCNLD2Pk-uCUOmh2TsuaM6LE88"
ETHERSCAN_API_KEY = "Q6MHE3UDYVT1ZNTC6CCKCUPQMJPKCA9MWG"
BSCSCAN_API_KEY = "Q6MHE3UDYVT1ZNTC6CCKCUPQMJPKCA9MWG"

POLL_INTERVAL = 30  # Проверка каждые 30 секунд

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── БИРЖЕВЫЕ АДРЕСА ──────────────────────────────────────────────────────────
EXCHANGE_ADDRESSES = {
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance 2",
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be": "Binance 3",
    "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8": "Binance Cold",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
    "0xa7efae728d2936e78bda97dc267687568dd593f3": "OKX Deposit",
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit",
    "0x75e89d5979e4f6fba9f97c104c2f0afb3f1dcb88": "MEXC",
}
EXCHANGE_ADDRESSES = {k.lower(): v for k, v in EXCHANGE_ADDRESSES.items()}

# ─── ХРАНИЛИЩЕ ────────────────────────────────────────────────────────────────
user_watchlist: Dict[int, List[Dict]] = {}
last_blocks: Dict[str, int] = {}
temp_data: Dict[int, Dict] = {}

WAITING_CONTRACT, WAITING_AMOUNT, WAITING_CHAIN = range(3)


# ─── УТИЛИТЫ ──────────────────────────────────────────────────────────────────

def get_api_config(chain: str):
    configs = {
        "eth": ("https://etherscan.io", ETHERSCAN_API_KEY),
        "bsc": ("https://bscscan.com", BSCSCAN_API_KEY),
        "polygon": ("https://polygonscan.com", ETHERSCAN_API_KEY),
        "arbitrum": ("https://arbiscan.io", ETHERSCAN_API_KEY),
        "optimism": ("https://etherscan.io", ETHERSCAN_API_KEY),
    }
    return configs.get(chain, configs["eth"])


def get_explorer_url(chain: str, tx_hash: str) -> str:
    urls = {"eth": "etherscan.io", "bsc": "bscscan.com", "polygon": "polygonscan.com", "arbitrum": "arbiscan.io",
            "optimism": "optimistic.etherscan.io"}
    return f"https://{urls.get(chain, 'etherscan.io')}/tx/{tx_hash}"


# ─── API ЗАПРОСЫ ──────────────────────────────────────────────────────────────

async def get_token_metadata(contract: str, chain: str, session: aiohttp.ClientSession):
    """Исправляет UNKNOWN: запрашивает данные токена через историю транзакций"""
    base_url, api_key = get_api_config(chain)
    params = {"module": "account", "action": "tokentx", "contractaddress": contract, "page": 1, "offset": 1,
              "apikey": api_key}
    try:
        async with session.get(base_url, params=params) as resp:
            data = await resp.json()
            if data.get("status") == "1" and data.get("result"):
                tx = data["result"][0]
                return tx.get("tokenSymbol", "UNKNOWN"), int(tx.get("tokenDecimal", 18))
    except Exception as e:
        logger.error(f"Error fetching metadata: {e}")
    return "UNKNOWN", 18


async def get_token_price(contract: str, session: aiohttp.ClientSession) -> float:
    url = f"https://dexscreener.com{contract}"
    try:
        async with session.get(url, timeout=5) as resp:
            data = await resp.json()
            pairs = data.get("pairs", [])
            if pairs:
                top_pair = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
                return float(top_pair.get("priceUsd", 0))
    except:
        pass
    return 0.0


# ─── ОБРАБОТЧИКИ КОМАНД ───────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Бот для отслеживания китов запущен!\nИспользуй /add для мониторинга нового токена.")


async def add_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите адрес смарт-контракта (0x...):")
    return WAITING_CONTRACT


async def process_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contract = update.message.text.strip().lower()
    temp_data[update.effective_user.id] = {"contract": contract}

    keyboard = [
        [InlineKeyboardButton("ETH", callback_data="c_eth"), InlineKeyboardButton("BSC", callback_data="c_bsc")],
        [InlineKeyboardButton("Arbitrum", callback_data="c_arbitrum"),
         InlineKeyboardButton("Optimism", callback_data="c_optimism")]]
    await update.message.reply_text("Выберите сеть:", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAITING_CHAIN


async def process_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    temp_data[query.from_user.id]["chain"] = query.data.replace("c_", "")
    await query.edit_message_text("Введите мин. сумму перевода (числом):")
    return WAITING_AMOUNT


async def process_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        min_amt = float(update.message.text.replace(",", "."))
        data = temp_data[user_id]

        async with aiohttp.ClientSession() as session:
            symbol, decimals = await get_token_metadata(data['contract'], data['chain'], session)

        token_info = {"contract": data['contract'], "chain": data['chain'], "min_amount": min_amt, "symbol": symbol,
                      "decimals": decimals}
        user_watchlist.setdefault(user_id, []).append(token_info)

        await update.message.reply_text(f"✅ Добавлено: {symbol} ({data['chain'].upper()})\nМинимум: {min_amt} {symbol}")
    except:
        await update.message.reply_text("❌ Ошибка в сумме. Попробуй /add снова.")
    return ConversationHandler.END


async def list_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tokens = user_watchlist.get(update.effective_user.id, [])
    if not tokens: return await update.message.reply_text("Список пуст.")
    msg = "📋 Ваши токены:\n" + "\n".join(
        [f"{i + 1}. {t['symbol']} | Мин: {t['min_amount']}" for i, t in enumerate(tokens)])
    await update.message.reply_text(msg)


# ─── МОНИТОРИНГ ───────────────────────────────────────────────────────────────

async def monitor_task(context: ContextTypes.DEFAULT_TYPE):
    async with aiohttp.ClientSession() as session:
        for user_id, tokens in user_watchlist.items():
            for token in tokens:
                base_url, api_key = get_api_config(token['chain'])
                key = f"{token['chain']}:{token['contract']}"

                # Инициализация блока
                if key not in last_blocks:
                    try:
                        async with session.get(base_url, params={"module": "proxy", "action": "eth_blockNumber",
                                                                 "apikey": api_key}) as r:
                            res = await r.json()
                            last_blocks[key] = int(res['result'], 16)
                    except:
                        continue
                    continue

                # Запрос трансферов
                params = {"module": "account", "action": "tokentx", "contractaddress": token['contract'],
                          "startblock": last_blocks[key] + 1, "sort": "asc", "apikey": api_key}
                try:
                    async with session.get(base_url, params=params) as resp:
                        data = await resp.json()
                        if data.get("status") == "1":
                            price = await get_token_price(token['contract'], session)
                            for tx in data["result"]:
                                last_blocks[key] = max(last_blocks[key], int(tx['blockNumber']))
                                amt = int(tx['value']) / (10 ** int(tx.get('tokenDecimal', 18)))

                                if amt >= token['min_amount']:
                                    from_ex = EXCHANGE_ADDRESSES.get(tx['from'].lower(), tx['from'][:6])
                                    to_ex = EXCHANGE_ADDRESSES.get(tx['to'].lower(), tx['to'][:6])

                                    msg = (f"🚨 **Крупный перевод {token['symbol']}**\n"
                                           f"💰 Сумма: {amt:,.2f} ({amt * price:,.2f}$)\n"
                                           f"От: `{from_ex}`\nКому: `{to_ex}`\n"
                                           f"[Explorer]({get_explorer_url(token['chain'], tx['hash'])})")
                                    await context.bot.send_message(user_id, msg, parse_mode="Markdown")
                except:
                    pass


# ─── ЗАПУСК ───────────────────────────────────────────────────────────────────

def main():
    # Исправлено: добавлено подавление ворнингов и инициализация JobQueue
    application = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_token)],
        states={
            WAITING_CONTRACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_contract)],
            WAITING_CHAIN: [CallbackQueryHandler(process_chain)],
            WAITING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_amount)],
        },
        fallbacks=[],
        per_message=False
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_tokens))
    application.add_handler(conv)

    # Исправлено: проверка наличия JobQueue перед запуском
    if application.job_queue:
        application.job_queue.run_repeating(monitor_task, interval=POLL_INTERVAL, first=10)
    else:
        print("ОШИБКА: JobQueue не найдена. Установите pip install python-telegram-bot[job-queue]")

    print("Бот запущен...")
    application.run_polling()


if __name__ == "__main__":
    main()
