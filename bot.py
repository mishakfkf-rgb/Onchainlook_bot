"""
OnChain Alert Bot — отслеживает переводы токенов между ончейн кошельками и биржами
Автор: сгенерирован для ончейн аналитики
"""

import asyncio
import logging
import json
import time
from datetime import datetime
from typing import Optional
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── КОНФИГ ───────────────────────────────────────────────────────────────────
BOT_TOKEN = "8784977364:AAEQgiFJOGCNLD2Pk-uCUOmh2TsuaM6LE88"  # @BotFather
ETHERSCAN_API_KEY = "Q6MHE3UDYVT1ZNTC6CCKCUPQMJPKCA9MWG"  # etherscan.io/myapikey
BSCSCAN_API_KEY = "Q6MHE3UDYVT1ZNTC6CCKCUPQMJPKCA9MWG"  # bscscan.com/myapikey (опционально)
POLL_INTERVAL = 10  # секунд между проверками

# ─── ИЗВЕСТНЫЕ БИРЖЕВЫЕ АДРЕСА ────────────────────────────────────────────────
# Это ключевое — бот будет определять является ли адрес биржей
EXCHANGE_ADDRESSES = {
    # BINANCE
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance Hot Wallet",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance Hot Wallet 2",
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be": "Binance Hot Wallet 3",
    "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8": "Binance Cold Wallet",
    "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance Hot Wallet 4",
    "0x001866ae5b3de6caa5a51543fd9fb64f524f5478": "Binance Deposit",
    "0x85b931a32a0725be14285b66f1a22178c672d69b": "Binance Deposit 2",
    # OKX
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX Hot Wallet",
    "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3": "OKX Hot Wallet 2",
    "0xa7efae728d2936e78bda97dc267687568dd593f3": "OKX Deposit",
    # BYBIT
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit Hot Wallet",
    "0x1db92e2eebc8e0c075a02bea49a2935bcd2dfcf4": "Bybit Hot Wallet 2",
    # KRAKEN
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0": "Kraken Hot Wallet",
    "0x53d284357ec70ce289d6d64134dfac8e511c8a3d": "Kraken Cold Wallet",
    # COINBASE
    "0x503828976d22510aad0201ac7ec88293211d23da": "Coinbase Hot Wallet",
    "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740": "Coinbase Hot Wallet 2",
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase Hot Wallet 3",
    # KUCOIN
    "0x2b5634c42055806a59e9107ed44d43c426e58258": "KuCoin Hot Wallet",
    "0xa1d8d972560c2f8144af871db508f0b0b10a3fbf": "KuCoin Hot Wallet 2",
    # MEXC
    "0x75e89d5979e4f6fba9f97c104c2f0afb3f1dcb88": "MEXC Hot Wallet",
    # GATE.IO
    "0x0d0707963952f2fba59dd06f2b425ace40b492fe": "Gate.io Hot Wallet",
    # BITFINEX
    "0x1151314c646ce4e0efd76d1af4760ae66a9fe30f": "Bitfinex Hot Wallet",
}

# Нормализуем адреса в нижний регистр
EXCHANGE_ADDRESSES = {k.lower(): v for k, v in EXCHANGE_ADDRESSES.items()}

# ─── ХРАНИЛИЩЕ СОСТОЯНИЯ ──────────────────────────────────────────────────────
# user_id -> список отслеживаемых токенов
user_watchlist: dict[int, list[dict]] = {}
# user_id -> последний обработанный блок для каждого токена
last_blocks: dict[str, int] = {}

# Состояния для ConversationHandler
WAITING_CONTRACT, WAITING_AMOUNT, WAITING_CHAIN = range(3)
temp_data: dict[int, dict] = {}


# ─── УТИЛИТЫ ──────────────────────────────────────────────────────────────────

def get_explorer_url(chain: str, tx_hash: str) -> str:
    explorers = {
        "eth": f"https://etherscan.io/tx/{tx_hash}",
        "bsc": f"https://bscscan.com/tx/{tx_hash}",
        "polygon": f"https://polygonscan.com/tx/{tx_hash}",
        "arbitrum": f"https://arbiscan.io/tx/{tx_hash}",
        "optimism": f"https://optimistic.etherscan.io/tx/{tx_hash}",
    }
    return explorers.get(chain, f"https://etherscan.io/tx/{tx_hash}")


def get_address_url(chain: str, address: str) -> str:
    explorers = {
        "eth": f"https://etherscan.io/address/{address}",
        "bsc": f"https://bscscan.com/address/{address}",
        "polygon": f"https://polygonscan.com/address/{address}",
        "arbitrum": f"https://arbiscan.io/address/{address}",
        "optimism": f"https://optimistic.etherscan.io/address/{address}",
    }
    return explorers.get(chain, f"https://etherscan.io/address/{address}")


def is_exchange(address: str) -> Optional[str]:
    """Возвращает название биржи если адрес известен, иначе None"""
    return EXCHANGE_ADDRESSES.get(address.lower())


def format_amount(value: float, symbol: str) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M {symbol}"
    elif value >= 1_000:
        return f"{value / 1_000:.1f}K {symbol}"
    else:
        return f"{value:.2f} {symbol}"


def format_usd(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    elif value >= 1_000:
        return f"${value / 1_000:.1f}K"
    else:
        return f"${value:.2f}"


def short_addr(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}"


# ─── API ЗАПРОСЫ ──────────────────────────────────────────────────────────────

async def get_token_info(contract: str, chain: str, session: aiohttp.ClientSession) -> dict:
    """Получаем информацию о токене"""
    api_urls = {
        "eth": "https://api.etherscan.io/api",
        "bsc": "https://api.bscscan.com/api",
        "polygon": "https://api.polygonscan.com/api",
        "arbitrum": "https://api.arbiscan.io/api",
        "optimism": "https://api-optimistic.etherscan.io/api",
    }
    api_keys = {
        "eth": ETHERSCAN_API_KEY,
        "bsc": BSCSCAN_API_KEY,
        "polygon": ETHERSCAN_API_KEY,
        "arbitrum": ETHERSCAN_API_KEY,
        "optimism": ETHERSCAN_API_KEY,
    }

    url = api_urls.get(chain, api_urls["eth"])
    api_key = api_keys.get(chain, ETHERSCAN_API_KEY)

    params = {
        "module": "token",
        "action": "tokeninfo",
        "contractaddress": contract,
        "apikey": api_key
    }
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if data.get("status") == "1" and data.get("result"):
                result = data["result"]
                if isinstance(result, list) and len(result) > 0:
                    return result[0]
    except Exception as e:
        logger.error(f"Error getting token info: {e}")
    return {}


async def get_token_price(contract: str, chain: str, session: aiohttp.ClientSession) -> float:
    """Получаем цену токена через DexScreener (бесплатно, без API ключа)"""
    chain_map = {
        "eth": "ethereum",
        "bsc": "bsc",
        "polygon": "polygon",
        "arbitrum": "arbitrum",
        "optimism": "optimism",
    }
    dex_chain = chain_map.get(chain, "ethereum")
    url = f"https://api.dexscreener.com/latest/dex/tokens/{contract}"

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            pairs = data.get("pairs", [])
            if pairs:
                # Берём пару с наибольшей ликвидностью
                pairs_sorted = sorted(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0),
                                      reverse=True)
                price_str = pairs_sorted[0].get("priceUsd", "0")
                return float(price_str) if price_str else 0.0
    except Exception as e:
        logger.error(f"Error getting price: {e}")
    return 0.0


async def get_token_transfers(contract: str, chain: str, from_block: int, session: aiohttp.ClientSession) -> list:
    """Получаем трансферы токена начиная с блока"""
    api_urls = {
        "eth": "https://api.etherscan.io/api",
        "bsc": "https://api.bscscan.com/api",
        "polygon": "https://api.polygonscan.com/api",
        "arbitrum": "https://api.arbiscan.io/api",
        "optimism": "https://api-optimistic.etherscan.io/api",
    }
    api_keys = {
        "eth": ETHERSCAN_API_KEY,
        "bsc": BSCSCAN_API_KEY,
        "polygon": ETHERSCAN_API_KEY,
        "arbitrum": ETHERSCAN_API_KEY,
        "optimism": ETHERSCAN_API_KEY,
    }

    url = api_urls.get(chain, api_urls["eth"])
    api_key = api_keys.get(chain, ETHERSCAN_API_KEY)

    params = {
        "module": "account",
        "action": "tokentx",
        "contractaddress": contract,
        "startblock": from_block,
        "endblock": 99999999,
        "sort": "asc",
        "apikey": api_key,
        "offset": 100,
        "page": 1,
    }

    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if data.get("status") == "1":
                return data.get("result", [])
    except Exception as e:
        logger.error(f"Error getting transfers: {e}")
    return []


async def get_current_block(chain: str, session: aiohttp.ClientSession) -> int:
    """Получаем текущий блок"""
    api_urls = {
        "eth": "https://api.etherscan.io/api",
        "bsc": "https://api.bscscan.com/api",
        "polygon": "https://api.polygonscan.com/api",
        "arbitrum": "https://api.arbiscan.io/api",
        "optimism": "https://api-optimistic.etherscan.io/api",
    }
    api_keys = {
        "eth": ETHERSCAN_API_KEY,
        "bsc": BSCSCAN_API_KEY,
        "polygon": ETHERSCAN_API_KEY,
        "arbitrum": ETHERSCAN_API_KEY,
        "optimism": ETHERSCAN_API_KEY,
    }

    url = api_urls.get(chain, api_urls["eth"])
    api_key = api_keys.get(chain, ETHERSCAN_API_KEY)

    params = {
        "module": "proxy",
        "action": "eth_blockNumber",
        "apikey": api_key
    }
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            return int(data.get("result", "0x0"), 16)
    except:
        return 0


# ─── МОНИТОРИНГ ───────────────────────────────────────────────────────────────

async def check_transfers(app: Application):
    """Основной цикл мониторинга — проверяет все токены всех пользователей"""
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Собираем уникальные токены для проверки
                tokens_to_check = {}
                for user_id, watchlist in user_watchlist.items():
                    for token in watchlist:
                        key = f"{token['contract']}_{token['chain']}"
                        if key not in tokens_to_check:
                            tokens_to_check[key] = {**token, "users": []}
                        tokens_to_check[key]["users"].append(user_id)

                for key, token_data in tokens_to_check.items():
                    contract = token_data["contract"]
                    chain = token_data["chain"]

                    # Определяем с какого блока проверять
                    if key not in last_blocks:
                        current_block = await get_current_block(chain, session)
                        last_blocks[key] = max(0, current_block - 10)  # последние ~10 блоков при старте

                    from_block = last_blocks[key] + 1
                    transfers = await get_token_transfers(contract, chain, from_block, session)

                    if transfers:
                        # Обновляем последний блок
                        last_blocks[key] = max(int(t["blockNumber"]) for t in transfers)

                        # Получаем цену токена один раз для всех трансферов
                        price = await get_token_price(contract, chain, session)

                        for tx in transfers:
                            await process_transfer(app, tx, token_data, price, chain)

                    await asyncio.sleep(0.3)  # rate limit между запросами

            except Exception as e:
                logger.error(f"Monitor error: {e}")

            await asyncio.sleep(POLL_INTERVAL)


async def process_transfer(app: Application, tx: dict, token_data: dict, price: float, chain: str):
    """Обрабатываем каждый трансфер и решаем стоит ли слать алерт"""
    from_addr = tx.get("from", "").lower()
    to_addr = tx.get("to", "").lower()

    # Определяем тип движения
    from_exchange = is_exchange(from_addr)
    to_exchange = is_exchange(to_addr)

    # Нас интересует только:
    # 1. ОНЧЕЙН → БИРЖА (потенциальная продажа)
    # 2. БИРЖА → ОНЧЕЙН (накопление / вывод)
    if not from_exchange and not to_exchange:
        return  # обычный перевод между кошельками — игнорируем
    if from_exchange and to_exchange:
        return  # внутренний перевод между биржами — игнорируем

    # Считаем сумму в USD
    decimals = int(tx.get("tokenDecimal", 18))
    raw_value = int(tx.get("value", 0))
    amount = raw_value / (10 ** decimals)
    usd_value = amount * price if price > 0 else 0

    symbol = tx.get("tokenSymbol", token_data.get("symbol", "???"))
    tx_hash = tx.get("hash", "")
    timestamp = int(tx.get("timeStamp", 0))
    dt = datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M UTC")

    # Отправляем алерт пользователям которые следят за этим токеном
    for user_id in token_data["users"]:
        # Проверяем порог пользователя
        user_tokens = user_watchlist.get(user_id, [])
        user_token = next((t for t in user_tokens if t["contract"].lower() == token_data["contract"].lower()), None)
        if not user_token:
            continue

        min_usd = user_token.get("min_usd", 100_000)

        # Фильтруем по сумме
        if usd_value > 0 and usd_value < min_usd:
            continue
        if usd_value == 0 and amount < 1000:  # если нет цены — фильтруем по кол-ву токенов
            continue

        # Формируем сообщение
        if to_exchange:
            # ОНЧЕЙН → БИРЖА (потенциальная продажа — сигнал на шорт)
            direction = "🔴 ОНЧЕЙН → БИРЖА"
            action = "⚠️ Возможная ПРОДАЖА"
            from_label = f"[{short_addr(from_addr)}]({get_address_url(chain, from_addr)})"
            to_label = f"🏦 {to_exchange}"
        else:
            # БИРЖА → ОНЧЕЙН (вывод / накопление — сигнал на лонг)
            direction = "🟢 БИРЖА → ОНЧЕЙН"
            action = "✅ Возможное НАКОПЛЕНИЕ"
            from_label = f"🏦 {from_exchange}"
            to_label = f"[{short_addr(to_addr)}]({get_address_url(chain, to_addr)})"

        usd_str = format_usd(usd_value) if usd_value > 0 else "цена неизвестна"
        amount_str = format_amount(amount, symbol)

        msg = (
            f"{direction}\n"
            f"{action}\n\n"
            f"🪙 Токен: **{symbol}**\n"
            f"💰 Сумма: **{amount_str}** ({usd_str})\n"
            f"📤 От: {from_label}\n"
            f"📥 Кому: {to_label}\n"
            f"🕐 Время: {dt}\n"
            f"🔗 [Смотреть TX]({get_explorer_url(chain, tx_hash)})"
        )

        try:
            await app.bot.send_message(
                chat_id=user_id,
                text=msg,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Error sending alert to {user_id}: {e}")


# ─── КОМАНДЫ БОТА ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"👋 Привет, {user.first_name}!\n\n"
        f"Я — OnChain Alert Bot 🔍\n"
        f"Слежу за переводами токенов между ончейн кошельками и биржами.\n\n"
        f"📌 *Команды:*\n"
        f"/add — добавить токен для отслеживания\n"
        f"/list — список отслеживаемых токенов\n"
        f"/remove — удалить токен\n"
        f"/help — как пользоваться\n\n"
        f"*Что я отслеживаю:*\n"
        f"🔴 Ончейн → Биржа (сигнал продажи)\n"
        f"🟢 Биржа → Ончейн (сигнал накопления)\n\n"
        f"Биржи: Binance, OKX, Bybit, Coinbase, Kraken, KuCoin, MEXC, Gate.io"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Как пользоваться:*\n\n"
        "1️⃣ Напиши /add\n"
        "2️⃣ Вставь контракт токена (например: `0x...`)\n"
        "3️⃣ Выбери сеть (ETH, BSC, Polygon и т.д.)\n"
        "4️⃣ Укажи минимальную сумму в USD (например: `100000` = $100K)\n\n"
        "После этого я начну слать алерты когда:\n"
        "🔴 Кто-то переводит токен *на биржу* (возможная продажа)\n"
        "🟢 Кто-то выводит токен *с биржи* (накопление)\n\n"
        "💡 *Совет:* После алерта открывай адрес отправителя в Arkham (intel.arkm.com) "
        "чтобы узнать кто это и сколько у него осталось токенов."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    temp_data[user_id] = {}
    await update.message.reply_text(
        "📝 *Шаг 1/3 — Контракт токена*\n\n"
        "Вставь адрес контракта токена:\n"
        "Пример: `0x1234...abcd`\n\n"
        "Найти контракт можно на CoinGecko, CoinMarketCap или Etherscan.",
        parse_mode="Markdown"
    )
    return WAITING_CONTRACT


async def received_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    contract = update.message.text.strip()

    if not contract.startswith("0x") or len(contract) != 42:
        await update.message.reply_text(
            "❌ Неверный формат адреса.\n"
            "Адрес должен начинаться с `0x` и содержать 42 символа.\n"
            "Попробуй ещё раз:",
            parse_mode="Markdown"
        )
        return WAITING_CONTRACT

    temp_data[user_id]["contract"] = contract.lower()

    keyboard = [
        [
            InlineKeyboardButton("Ethereum", callback_data="chain_eth"),
            InlineKeyboardButton("BSC", callback_data="chain_bsc"),
        ],
        [
            InlineKeyboardButton("Polygon", callback_data="chain_polygon"),
            InlineKeyboardButton("Arbitrum", callback_data="chain_arbitrum"),
        ],
        [
            InlineKeyboardButton("Optimism", callback_data="chain_optimism"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "✅ Контракт принят!\n\n"
        "⛓ *Шаг 2/3 — Выбери сеть:*",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return WAITING_CHAIN


async def received_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    chain = query.data.replace("chain_", "")
    temp_data[user_id]["chain"] = chain

    chain_names = {
        "eth": "Ethereum", "bsc": "BSC",
        "polygon": "Polygon", "arbitrum": "Arbitrum", "optimism": "Optimism"
    }

    await query.edit_message_text(
        f"✅ Сеть: {chain_names.get(chain, chain)}\n\n"
        f"💵 *Шаг 3/3 — Минимальная сумма*\n\n"
        f"Укажи минимальную сумму в USD для алертов.\n"
        f"Примеры:\n"
        f"• `100000` = $100K\n"
        f"• `500000` = $500K\n"
        f"• `1000000` = $1M\n\n"
        f"Рекомендую от $100,000 чтобы не было спама.",
        parse_mode="Markdown"
    )
    return WAITING_AMOUNT


async def received_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip().replace(",", "").replace("$", "").replace("K", "000").replace("M", "000000")

    try:
        min_usd = float(text)
        if min_usd < 100:
            await update.message.reply_text("❌ Минимум $100. Введи число побольше:")
            return WAITING_AMOUNT
    except ValueError:
        await update.message.reply_text("❌ Введи число, например: `100000`", parse_mode="Markdown")
        return WAITING_AMOUNT

    temp_data[user_id]["min_usd"] = min_usd
    contract = temp_data[user_id]["contract"]
    chain = temp_data[user_id]["chain"]

    # Получаем инфо о токене
    await update.message.reply_text("🔍 Проверяю токен...")

    async with aiohttp.ClientSession() as session:
        token_info = await get_token_info(contract, chain, session)
        price = await get_token_price(contract, chain, session)

    symbol = token_info.get("symbol", "???")
    name = token_info.get("name", "Unknown")

    if symbol == "???":
        # Пробуем взять из DexScreener
        symbol = "UNKNOWN"

    temp_data[user_id]["symbol"] = symbol
    temp_data[user_id]["name"] = name

    # Добавляем в watchlist
    if user_id not in user_watchlist:
        user_watchlist[user_id] = []

    # Проверяем нет ли уже такого токена
    existing = next((t for t in user_watchlist[user_id] if t["contract"].lower() == contract.lower()), None)
    if existing:
        existing.update(temp_data[user_id])
        action = "обновлён"
    else:
        user_watchlist[user_id].append(dict(temp_data[user_id]))
        action = "добавлен"

    chain_names = {"eth": "Ethereum", "bsc": "BSC", "polygon": "Polygon", "arbitrum": "Arbitrum",
                   "optimism": "Optimism"}
    price_str = f"${price:.6f}" if price > 0 else "неизвестна"

    await update.message.reply_text(
        f"✅ *Токен {action}!*\n\n"
        f"🪙 Токен: **{symbol}** ({name})\n"
        f"⛓ Сеть: {chain_names.get(chain, chain)}\n"
        f"📍 Контракт: `{contract[:10]}...{contract[-6:]}`\n"
        f"💵 Текущая цена: {price_str}\n"
        f"🔔 Минимум алерта: {format_usd(min_usd)}\n\n"
        f"Я слежу за переводами на/с бирж:\n"
        f"Binance, OKX, Bybit, Coinbase, Kraken, KuCoin, MEXC, Gate.io\n\n"
        f"📲 Алерты будут приходить сюда автоматически.",
        parse_mode="Markdown"
    )

    del temp_data[user_id]
    return ConversationHandler.END


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    watchlist = user_watchlist.get(user_id, [])

    if not watchlist:
        await update.message.reply_text(
            "📋 Список пуст.\n\nДобавь токен командой /add"
        )
        return

    chain_names = {"eth": "ETH", "bsc": "BSC", "polygon": "MATIC", "arbitrum": "ARB", "optimism": "OP"}

    text = "📋 *Твои токены:*\n\n"
    for i, token in enumerate(watchlist, 1):
        symbol = token.get("symbol", "???")
        chain = chain_names.get(token.get("chain", "eth"), token.get("chain", "eth"))
        min_usd = token.get("min_usd", 0)
        contract = token.get("contract", "")
        text += (
            f"{i}. **{symbol}** ({chain})\n"
            f"   Контракт: `{contract[:10]}...{contract[-6:]}`\n"
            f"   Мин. сумма: {format_usd(min_usd)}\n\n"
        )

    text += "Чтобы удалить токен: /remove"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    watchlist = user_watchlist.get(user_id, [])

    if not watchlist:
        await update.message.reply_text("📋 Список пуст.")
        return

    keyboard = []
    for i, token in enumerate(watchlist):
        symbol = token.get("symbol", "???")
        chain = token.get("chain", "eth").upper()
        keyboard.append([InlineKeyboardButton(
            f"❌ {symbol} ({chain})",
            callback_data=f"remove_{i}"
        )])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выбери токен для удаления:", reply_markup=reply_markup)


async def handle_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    idx = int(query.data.replace("remove_", ""))
    watchlist = user_watchlist.get(user_id, [])

    if 0 <= idx < len(watchlist):
        removed = watchlist.pop(idx)
        symbol = removed.get("symbol", "???")
        await query.edit_message_text(f"✅ Токен **{symbol}** удалён.", parse_mode="Markdown")
    else:
        await query.edit_message_text("❌ Токен не найден.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in temp_data:
        del temp_data[user_id]
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


# ─── ЗАПУСК ───────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler для добавления токена
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            WAITING_CONTRACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_contract)],
            WAITING_CHAIN: [CallbackQueryHandler(received_chain, pattern="^chain_")],
            WAITING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CallbackQueryHandler(handle_remove, pattern="^remove_"))
    app.add_handler(conv_handler)

    # Запускаем мониторинг в фоне
    async def post_init(app: Application):
        asyncio.create_task(check_transfers(app))

    app.post_init = post_init

    logger.info("🚀 Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
