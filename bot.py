"""
OnChainlook Bot
- Tracks ERC-20 transfers for configured tokens
- Detects CEX + on-chain flows
- Sends Telegram alerts when transfer USD value is above threshold
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("onchainlook")

WAITING_CONTRACT, WAITING_CHAIN, WAITING_AMOUNT, WAITING_WALLET_ADDRESS, WAITING_WALLET_CHAIN = range(5)


def load_dotenv_file(path: str = ".env") -> None:
    """Minimal .env loader (no external dependency required)."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as exc:
        logger.warning("Could not parse .env file: %s", exc)


load_dotenv_file()


@dataclass(frozen=True)
class ChainConfig:
    key: str
    title: str
    chain_id: Optional[str]
    use_v2: bool
    api_url: str
    explorer_tx: str
    explorer_address: str
    api_key: str


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


ETHERSCAN_KEY = _env("ETHERSCAN_API_KEY") # Ключик апискана до 5 запросов в минуту!

CHAINS: dict[str, ChainConfig] = {   # Добавил сети для отслеживания
    "eth": ChainConfig(
        key="eth",
        title="Ethereum",
        chain_id="1",
        use_v2=True,
        api_url="https://api.etherscan.io/v2/api",
        explorer_tx="https://etherscan.io/tx/{tx_hash}",
        explorer_address="https://etherscan.io/address/{address}",
        api_key=ETHERSCAN_KEY,
    ),
    "bsc": ChainConfig(
        key="bsc",
        title="BSC",
        chain_id="56",
        use_v2=True,
        api_url="https://api.bscscan.com/v2/api",
        explorer_tx="https://bscscan.com/tx/{tx_hash}",
        explorer_address="https://bscscan.com/address/{address}",
        api_key=_env("BSCSCAN_API_KEY", ETHERSCAN_KEY),
    ),
    "polygon": ChainConfig(
        key="polygon",
        title="Polygon",
        chain_id="137",
        use_v2=True,
        api_url="https://api.polygonscan.com/v2/api",
        explorer_tx="https://polygonscan.com/tx/{tx_hash}",
        explorer_address="https://polygonscan.com/address/{address}",
        api_key=_env("POLYGONSCAN_API_KEY", ETHERSCAN_KEY),
    ),
    "arbitrum": ChainConfig(
        key="arbitrum",
        title="Arbitrum",
        chain_id="42161",
        use_v2=True,
        api_url="https://api.arbiscan.io/v2/api",
        explorer_tx="https://arbiscan.io/tx/{tx_hash}",
        explorer_address="https://arbiscan.io/address/{address}",
        api_key=_env("ARBISCAN_API_KEY", ETHERSCAN_KEY),
    ),
    "optimism": ChainConfig(
        key="optimism",
        title="Optimism",
        chain_id="10",
        use_v2=True,
        api_url="https://api-optimistic.etherscan.io/v2/api",
        explorer_tx="https://optimistic.etherscan.io/tx/{tx_hash}",
        explorer_address="https://optimistic.etherscan.io/address/{address}",
        api_key=_env("OPTIMISMSCAN_API_KEY", ETHERSCAN_KEY),
    ),
}

BOT_TOKEN = _env("BOT_TOKEN")
POLL_INTERVAL = int(_env("POLL_INTERVAL_SECONDS", "12"))
DB_PATH = _env("DB_PATH", "onchainlook.db")

EXCHANGE_LABELS = {     #коши горячих кошельков популярных чтобы не было фейк транз
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance Hot Wallet",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance Hot Wallet 2",
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be": "Binance Hot Wallet 3",
    "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8": "Binance Cold Wallet",
    "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance Hot Wallet 4",
    "0x001866ae5b3de6caa5a51543fd9fb64f524f5478": "Binance Deposit",
    "0x85b931a32a0725be14285b66f1a22178c672d69b": "Binance Deposit 2",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX Hot Wallet",
    "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3": "OKX Hot Wallet 2",
    "0xa7efae728d2936e78bda97dc267687568dd593f3": "OKX Deposit",
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit Hot Wallet",
    "0x1db92e2eebc8e0c075a02bea49a2935bcd2dfcf4": "Bybit Hot Wallet 2",
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0": "Kraken Hot Wallet",
    "0x53d284357ec70ce289d6d64134dfac8e511c8a3d": "Kraken Cold Wallet",
    "0x503828976d22510aad0201ac7ec88293211d23da": "Coinbase Hot Wallet",
    "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740": "Coinbase Hot Wallet 2",
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase Hot Wallet 3",
    "0x2b5634c42055806a59e9107ed44d43c426e58258": "KuCoin Hot Wallet",
    "0xa1d8d972560c2f8144af871db508f0b0b10a3fbf": "KuCoin Hot Wallet 2",
    "0x75e89d5979e4f6fba9f97c104c2f0afb3f1dcb88": "MEXC Hot Wallet",
    "0x0d0707963952f2fba59dd06f2b425ace40b492fe": "Gate.io Hot Wallet",
    "0x1151314c646ce4e0efd76d1af4760ae66a9fe30f": "Bitfinex Hot Wallet",
}

price_cache: dict[tuple[str, str], tuple[float, float]] = {}
temp_data: dict[int, dict] = {}

NATIVE_SYMBOL = {
    "eth": "ETH",
    "bsc": "BNB",
    "polygon": "MATIC",
    "arbitrum": "ETH",
    "optimism": "ETH",
}


class Storage:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watches (
                user_id INTEGER NOT NULL,
                contract TEXT NOT NULL,
                chain TEXT NOT NULL,
                min_usd REAL NOT NULL,
                symbol TEXT,
                name TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, contract, chain)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cursors (
                contract TEXT NOT NULL,
                chain TEXT NOT NULL,
                last_block INTEGER NOT NULL,
                PRIMARY KEY (contract, chain)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS delivered_alerts (
                user_id INTEGER NOT NULL,
                tx_hash TEXT NOT NULL,
                log_index INTEGER NOT NULL,
                contract TEXT NOT NULL,
                chain TEXT NOT NULL,
                delivered_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, tx_hash, log_index, contract, chain)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_watches (
                user_id INTEGER NOT NULL,
                wallet TEXT NOT NULL,
                chain TEXT NOT NULL,
                last_block INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, wallet, chain)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_delivered_alerts (
                user_id INTEGER NOT NULL,
                wallet TEXT NOT NULL,
                chain TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                delivered_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, wallet, chain, tx_hash)
            )
            """
        )
        self.conn.commit()

    def upsert_watch(self, user_id: int, contract: str, chain: str, min_usd: float, symbol: str, name: str) -> None:
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO watches(user_id, contract, chain, min_usd, symbol, name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, contract, chain) DO UPDATE SET
                min_usd=excluded.min_usd,
                symbol=excluded.symbol,
                name=excluded.name,
                updated_at=excluded.updated_at
            """,
            (user_id, contract, chain, min_usd, symbol, name, now, now),
        )
        self.conn.commit()

    def list_user_watches(self, user_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT user_id, contract, chain, min_usd, symbol, name FROM watches WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()

    def remove_user_watch(self, user_id: int, contract: str, chain: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM watches WHERE user_id = ? AND contract = ? AND chain = ?",
            (user_id, contract, chain),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_all_watches(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT user_id, contract, chain, min_usd, symbol, name FROM watches"
        ).fetchall()

    def get_cursor(self, contract: str, chain: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT last_block FROM cursors WHERE contract = ? AND chain = ?",
            (contract, chain),
        ).fetchone()
        return int(row["last_block"]) if row else None

    def set_cursor(self, contract: str, chain: str, last_block: int) -> None:
        self.conn.execute(
            """
            INSERT INTO cursors(contract, chain, last_block)
            VALUES (?, ?, ?)
            ON CONFLICT(contract, chain) DO UPDATE SET
                last_block = excluded.last_block
            """,
            (contract, chain, last_block),
        )
        self.conn.commit()

    def was_delivered(self, user_id: int, tx_hash: str, log_index: int, contract: str, chain: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM delivered_alerts
            WHERE user_id = ? AND tx_hash = ? AND log_index = ? AND contract = ? AND chain = ?
            """,
            (user_id, tx_hash, log_index, contract, chain),
        ).fetchone()
        return row is not None

    def mark_delivered(self, user_id: int, tx_hash: str, log_index: int, contract: str, chain: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO delivered_alerts(user_id, tx_hash, log_index, contract, chain, delivered_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, tx_hash, log_index, contract, chain, int(time.time())),
        )
        self.conn.commit()

    def upsert_wallet_watch(self, user_id: int, wallet: str, chain: str) -> None:
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO wallet_watches(user_id, wallet, chain, last_block, created_at, updated_at)
            VALUES (?, ?, ?, NULL, ?, ?)
            ON CONFLICT(user_id, wallet, chain) DO UPDATE SET
                updated_at=excluded.updated_at
            """,
            (user_id, wallet, chain, now, now),
        )
        self.conn.commit()

    def list_user_wallet_watches(self, user_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT user_id, wallet, chain, last_block FROM wallet_watches WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()

    def list_all_wallet_watches(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT user_id, wallet, chain, last_block FROM wallet_watches"
        ).fetchall()

    def set_wallet_last_block(self, user_id: int, wallet: str, chain: str, last_block: int) -> None:
        self.conn.execute(
            """
            UPDATE wallet_watches
            SET last_block = ?, updated_at = ?
            WHERE user_id = ? AND wallet = ? AND chain = ?
            """,
            (last_block, int(time.time()), user_id, wallet, chain),
        )
        self.conn.commit()

    def remove_wallet_watch(self, user_id: int, wallet: str, chain: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM wallet_watches WHERE user_id = ? AND wallet = ? AND chain = ?",
            (user_id, wallet, chain),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def wallet_alert_sent(self, user_id: int, wallet: str, chain: str, tx_hash: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM wallet_delivered_alerts
            WHERE user_id = ? AND wallet = ? AND chain = ? AND tx_hash = ?
            """,
            (user_id, wallet, chain, tx_hash),
        ).fetchone()
        return row is not None

    def mark_wallet_alert_sent(self, user_id: int, wallet: str, chain: str, tx_hash: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO wallet_delivered_alerts(user_id, wallet, chain, tx_hash, delivered_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, wallet, chain, tx_hash, int(time.time())),
        )
        self.conn.commit()


storage = Storage(DB_PATH)


def format_usd(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.2f}"


def format_amount(value: float, symbol: str) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M {symbol}"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K {symbol}"
    return f"{value:.4f} {symbol}"


def short_addr(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}"


def chain_by_key(chain: str) -> ChainConfig:
    return CHAINS[chain]


def is_exchange(address: str) -> Optional[str]:
    return EXCHANGE_LABELS.get(address.lower())


def _parse_scanner_error(data: dict) -> Optional[str]:
    if not isinstance(data, dict):
        return "Scanner response is not JSON object"
    if isinstance(data.get("message"), str) and "deprecated" in data.get("message", "").lower():
        return data["message"]
    result = data.get("result")
    if isinstance(result, str) and "deprecated" in result.lower():
        return result
    if isinstance(result, str) and "free api access is not supported" in result.lower():
        return result
    return None


def _scan_params(cfg: ChainConfig, params: dict) -> dict:
    out = dict(params)
    if cfg.use_v2 and cfg.chain_id:
        out["chainid"] = cfg.chain_id
    return out


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict, timeout_sec: int = 12) -> dict:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
        try:
            return await resp.json(content_type=None)
        except Exception:
            raw = await resp.text()
            snippet = raw[:180].replace("\n", " ").strip()
            raise RuntimeError(f"Non-JSON scanner response (status={resp.status}): {snippet}")


async def get_current_block(chain: str, session: aiohttp.ClientSession) -> int:
    cfg = chain_by_key(chain)
    params = _scan_params(
        cfg,
        {
            "module": "proxy",
            "action": "eth_blockNumber",
            "apikey": cfg.api_key,
        },
    )
    data = await _get_json(session, cfg.api_url, params, timeout_sec=12)
    maybe_error = _parse_scanner_error(data)
    if maybe_error:
        raise RuntimeError(f"Scanner API error: {maybe_error}")
    raw = data.get("result", "0x0")
    if not isinstance(raw, str) or not raw.startswith("0x"):
        raise RuntimeError(f"Unexpected blockNumber payload: {raw!r}")
    return int(raw, 16)


async def get_token_transfers(
    contract: str,
    chain: str,
    start_block: int,
    session: aiohttp.ClientSession,
    max_pages: int = 3,
) -> list[dict]:
    cfg = chain_by_key(chain)
    transfers: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {
            "module": "account",
            "action": "tokentx",
            "contractaddress": contract,
            "startblock": start_block,
            "endblock": 99999999,
            "sort": "asc",
            "offset": 100,
            "page": page,
            "apikey": cfg.api_key,
        }
        params = _scan_params(cfg, params)
        data = await _get_json(session, cfg.api_url, params, timeout_sec=20)
        maybe_error = _parse_scanner_error(data)
        if maybe_error:
            raise RuntimeError(f"Scanner API error: {maybe_error}")

        result = data.get("result")
        if isinstance(result, str):
            # e.g. "No transactions found"
            break
        if not isinstance(result, list) or not result:
            break

        transfers.extend(result)
        if len(result) < 100:
            break

        await asyncio.sleep(0.2)

    return transfers


async def get_wallet_native_txs(wallet: str, chain: str, start_block: int, session: aiohttp.ClientSession) -> list[dict]:
    cfg = chain_by_key(chain)
    params = _scan_params(
        cfg,
        {
            "module": "account",
            "action": "txlist",
            "address": wallet,
            "startblock": start_block,
            "endblock": 99999999,
            "sort": "asc",
            "offset": 100,
            "page": 1,
            "apikey": cfg.api_key,
        },
    )
    data = await _get_json(session, cfg.api_url, params, timeout_sec=20)
    maybe_error = _parse_scanner_error(data)
    if maybe_error:
        raise RuntimeError(f"Scanner API error: {maybe_error}")
    result = data.get("result")
    if isinstance(result, list):
        return result
    return []


async def get_wallet_token_txs(wallet: str, chain: str, start_block: int, session: aiohttp.ClientSession) -> list[dict]:
    cfg = chain_by_key(chain)
    params = _scan_params(
        cfg,
        {
            "module": "account",
            "action": "tokentx",
            "address": wallet,
            "startblock": start_block,
            "endblock": 99999999,
            "sort": "asc",
            "offset": 100,
            "page": 1,
            "apikey": cfg.api_key,
        },
    )
    data = await _get_json(session, cfg.api_url, params, timeout_sec=20)
    maybe_error = _parse_scanner_error(data)
    if maybe_error:
        raise RuntimeError(f"Scanner API error: {maybe_error}")
    result = data.get("result")
    if isinstance(result, list):
        return result
    return []


async def get_token_price(contract: str, chain: str, session: aiohttp.ClientSession) -> float:
    cache_key = (contract.lower(), chain)
    cached = price_cache.get(cache_key)
    now = time.time()
    if cached and now - cached[1] < 90:
        return cached[0]

    chain_map = {
        "eth": "ethereum",
        "bsc": "bsc",
        "polygon": "polygon",
        "arbitrum": "arbitrum",
        "optimism": "optimism",
    }
    api_chain = chain_map.get(chain)

    url = f"https://api.dexscreener.com/latest/dex/tokens/{contract}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            data = await resp.json(content_type=None)
        pairs = data.get("pairs") or []
        if api_chain:
            pairs = [p for p in pairs if (p.get("chainId") or "").lower() == api_chain]
        if not pairs:
            price_cache[cache_key] = (0.0, now)
            return 0.0

        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0))
        price = float(best.get("priceUsd") or 0.0)
        price_cache[cache_key] = (price, now)
        return price
    except Exception as exc:
        logger.warning("Price lookup failed for %s on %s: %s", contract, chain, exc)
        return cached[0] if cached else 0.0


async def get_token_meta(contract: str, chain: str, session: aiohttp.ClientSession) -> tuple[str, str]:
    cfg = chain_by_key(chain)
    # V2-only token metadata endpoint
    if cfg.use_v2:
        tokeninfo_params = _scan_params(
            cfg,
            {
                "module": "token",
                "action": "tokeninfo",
                "contractaddress": contract,
                "apikey": cfg.api_key,
            },
        )
        try:
            data = await _get_json(session, cfg.api_url, tokeninfo_params, timeout_sec=12)
            maybe_error = _parse_scanner_error(data)
            if not maybe_error:
                result = data.get("result")
                if isinstance(result, list) and result:
                    row = result[0]
                    if isinstance(row, dict):
                        symbol = (row.get("symbol") or "UNKNOWN").strip() or "UNKNOWN"
                        name = (row.get("tokenName") or row.get("name") or "Unknown Token").strip() or "Unknown Token"
                        return symbol, name
        except Exception as exc:
            logger.warning("tokeninfo lookup failed, fallback to tokentx metadata: %s", exc)

    # Fallback: get metadata from transfer feed
    params = {
        "module": "account",
        "action": "tokentx",
        "contractaddress": contract,
        "page": 1,
        "offset": 1,
        "sort": "desc",
        "apikey": cfg.api_key,
    }
    params = _scan_params(cfg, params)
    try:
        data = await _get_json(session, cfg.api_url, params, timeout_sec=12)
        maybe_error = _parse_scanner_error(data)
        if maybe_error:
            logger.warning("Metadata fallback API error: %s", maybe_error)
            return "UNKNOWN", "Unknown Token"
        result = data.get("result")
        if isinstance(result, list) and result and isinstance(result[0], dict):
            symbol = (result[0].get("tokenSymbol") or "UNKNOWN").strip() or "UNKNOWN"
            name = (result[0].get("tokenName") or "Unknown Token").strip() or "Unknown Token"
            return symbol, name
    except Exception as exc:
        logger.warning("Could not fetch token metadata: %s", exc)

    # Last fallback: DexScreener token profile
    try:
        dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{contract}"
        async with session.get(dex_url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            data = await resp.json(content_type=None)
        pairs = data.get("pairs") or []
        if pairs and isinstance(pairs[0], dict):
            base = pairs[0].get("baseToken") or {}
            if isinstance(base, dict):
                symbol = (base.get("symbol") or "UNKNOWN").strip() or "UNKNOWN"
                name = (base.get("name") or "Unknown Token").strip() or "Unknown Token"
                return symbol, name
    except Exception as exc:
        logger.warning("DexScreener metadata fallback failed: %s", exc)
    return "UNKNOWN", "Unknown Token"


def normalize_contract(addr: str) -> Optional[str]:
    value = addr.strip().lower()
    if value.startswith("0x") and len(value) == 42:
        return value
    return None


def parse_min_usd(raw: str) -> Optional[float]:
    value = raw.strip().upper().replace("$", "").replace(",", "")
    if value.endswith("K"):
        value = value[:-1] + "000"
    elif value.endswith("M"):
        value = value[:-1] + "000000"
    try:
        number = float(value)
    except ValueError:
        return None
    if number < 100:
        return None
    return number


def make_tx_links(chain: str, tx_hash: str, address: str) -> tuple[str, str]:
    cfg = chain_by_key(chain)
    return cfg.explorer_tx.format(tx_hash=tx_hash), cfg.explorer_address.format(address=address)


def build_alert_message(tx: dict, chain: str, symbol: str, amount: float, usd_value: float) -> str:
    from_addr = tx["from"].lower()
    to_addr = tx["to"].lower()

    from_ex = is_exchange(from_addr)
    to_ex = is_exchange(to_addr)

    tx_url, from_addr_url = make_tx_links(chain, tx["hash"], from_addr)
    _, to_addr_url = make_tx_links(chain, tx["hash"], to_addr)

    timestamp = int(tx.get("timeStamp") or 0)
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if to_ex and not from_ex:
        direction = "🔴 ON-CHAIN → EXCHANGE"
        thesis = "Potential distribution / sell-side flow"
        from_label = f'<a href="{from_addr_url}">{short_addr(from_addr)}</a>'
        to_label = f"🏦 {html.escape(to_ex)}"
    else:
        direction = "🟢 EXCHANGE → ON-CHAIN"
        thesis = "Potential accumulation / withdrawal"
        from_label = f"🏦 {html.escape(from_ex or 'Exchange')}"
        to_label = f'<a href="{to_addr_url}">{short_addr(to_addr)}</a>'

    usd_text = format_usd(usd_value) if usd_value > 0 else "USD price unavailable"

    return (
        f"{direction}\n"
        f"<b>{html.escape(thesis)}</b>\n\n"
        f"🪙 Token: <b>{html.escape(symbol)}</b>\n"
        f"💸 Amount: <b>{html.escape(format_amount(amount, symbol))}</b> ({html.escape(usd_text)})\n"
        f"📤 From: {from_label}\n"
        f"📥 To: {to_label}\n"
        f"⛓ Chain: {html.escape(chain_by_key(chain).title)}\n"
        f"🕒 Time: {html.escape(dt)}\n"
        f"🔗 <a href=\"{tx_url}\">Open transaction</a>"
    )


def build_wallet_activity_message(wallet: str, chain: str, tx: dict, tx_type: str) -> str:
    wallet_l = wallet.lower()
    from_addr = (tx.get("from") or "").lower()
    to_addr = (tx.get("to") or "").lower()
    tx_hash = tx.get("hash") or ""
    tx_url, _ = make_tx_links(chain, tx_hash, wallet_l)
    timestamp = int(tx.get("timeStamp") or 0)
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    direction = "OUT" if from_addr == wallet_l else "IN"

    if tx_type == "token":
        symbol = (tx.get("tokenSymbol") or "TOKEN").strip() or "TOKEN"
        decimals = int(tx.get("tokenDecimal") or 18)
        amount = int(tx.get("value") or 0) / (10**decimals)
        amount_str = format_amount(amount, symbol)
        subtitle = f"🪙 {symbol} transfer"
    else:
        symbol = NATIVE_SYMBOL.get(chain, "NATIVE")
        amount = int(tx.get("value") or 0) / (10**18)
        amount_str = format_amount(amount, symbol)
        subtitle = f"⛽ Native transfer ({symbol})"

    return (
        f"👛 Wallet activity ({chain_by_key(chain).title})\n"
        f"{subtitle}\n\n"
        f"Direction: <b>{direction}</b>\n"
        f"Amount: <b>{html.escape(amount_str)}</b>\n"
        f"From: <code>{html.escape(from_addr)}</code>\n"
        f"To: <code>{html.escape(to_addr)}</code>\n"
        f"Time: {html.escape(dt)}\n"
        f"🔗 <a href=\"{tx_url}\">Open transaction</a>"
    )


async def check_transfers(app: Application) -> None:
    await asyncio.sleep(2)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                all_watches = storage.list_all_watches()
                grouped: dict[tuple[str, str], dict] = {}

                for row in all_watches:
                    key = (row["contract"], row["chain"])
                    grouped.setdefault(key, {"contract": row["contract"], "chain": row["chain"], "users": []})
                    grouped[key]["users"].append(
                        {
                            "user_id": int(row["user_id"]),
                            "min_usd": float(row["min_usd"]),
                            "symbol": row["symbol"] or "UNKNOWN",
                        }
                    )

                for (contract, chain), data in grouped.items():
                    try:
                        cursor = storage.get_cursor(contract, chain)
                        if cursor is None:
                            current_block = await get_current_block(chain, session)
                            storage.set_cursor(contract, chain, max(0, current_block - 20))
                            continue

                        transfers = await get_token_transfers(contract, chain, cursor + 1, session)
                        if not transfers:
                            await asyncio.sleep(0.15)
                            continue

                        latest_block = max(int(tx.get("blockNumber") or 0) for tx in transfers)
                        storage.set_cursor(contract, chain, latest_block)

                        price = await get_token_price(contract, chain, session)

                        for tx in transfers:
                            from_addr = (tx.get("from") or "").lower()
                            to_addr = (tx.get("to") or "").lower()
                            if not from_addr or not to_addr:
                                continue

                            from_ex = is_exchange(from_addr)
                            to_ex = is_exchange(to_addr)
                            if (from_ex and to_ex) or (not from_ex and not to_ex):
                                continue

                            decimals = int(tx.get("tokenDecimal") or 18)
                            raw_value = int(tx.get("value") or 0)
                            amount = raw_value / (10**decimals)
                            usd_value = amount * price if price > 0 else 0.0

                            symbol = (tx.get("tokenSymbol") or data["users"][0]["symbol"] or "UNKNOWN").strip() or "UNKNOWN"
                            tx_hash = tx.get("hash") or ""
                            log_index = int(tx.get("logIndex") or 0)

                            for sub in data["users"]:
                                user_id = sub["user_id"]
                                threshold = sub["min_usd"]
                                if usd_value <= 0 or usd_value < threshold:
                                    continue
                                if storage.was_delivered(user_id, tx_hash, log_index, contract, chain):
                                    continue

                                message = build_alert_message(tx, chain, symbol, amount, usd_value)
                                try:
                                    await app.bot.send_message(
                                        chat_id=user_id,
                                        text=message,
                                        parse_mode="HTML",
                                        disable_web_page_preview=True,
                                    )
                                    storage.mark_delivered(user_id, tx_hash, log_index, contract, chain)
                                except Exception as exc:
                                    logger.error("Failed to send alert to %s: %s", user_id, exc)

                        await asyncio.sleep(0.2)
                    except Exception as exc:
                        logger.warning(
                            "Skipping %s on %s this cycle due to API/runtime issue: %s",
                            contract,
                            chain,
                            exc,
                        )

                wallet_watches = storage.list_all_wallet_watches()
                for watch in wallet_watches:
                    user_id = int(watch["user_id"])
                    wallet = watch["wallet"].lower()
                    chain = watch["chain"]
                    last_block = watch["last_block"]
                    try:
                        if last_block is None:
                            current_block = await get_current_block(chain, session)
                            storage.set_wallet_last_block(user_id, wallet, chain, max(0, current_block - 20))
                            continue

                        start_block = int(last_block) + 1
                        native_txs = await get_wallet_native_txs(wallet, chain, start_block, session)
                        token_txs = await get_wallet_token_txs(wallet, chain, start_block, session)
                        combined = [(tx, "native") for tx in native_txs] + [(tx, "token") for tx in token_txs]
                        if not combined:
                            continue

                        latest_block = max(int((tx.get("blockNumber") or 0)) for tx, _ in combined)
                        storage.set_wallet_last_block(user_id, wallet, chain, latest_block)

                        combined.sort(key=lambda item: int((item[0].get("timeStamp") or 0)))
                        for tx, tx_type in combined:
                            tx_hash = tx.get("hash") or ""
                            if not tx_hash:
                                continue
                            if storage.wallet_alert_sent(user_id, wallet, chain, tx_hash):
                                continue
                            msg = build_wallet_activity_message(wallet, chain, tx, tx_type)
                            await app.bot.send_message(
                                chat_id=user_id,
                                text=msg,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                            storage.mark_wallet_alert_sent(user_id, wallet, chain, tx_hash)
                    except Exception as exc:
                        logger.warning(
                            "Skipping wallet %s on %s this cycle due to API/runtime issue: %s",
                            wallet,
                            chain,
                            exc,
                        )

            except Exception as exc:
                logger.exception("Monitor cycle failed: %s", exc)

            await asyncio.sleep(POLL_INTERVAL)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to OnChainlook Bot\n\n"
        "I monitor CEX-related token flows and send alerts for large transfers.\n"
        "Commands:\n"
        "/add - add token monitoring\n"
        "/list - show tracked tokens\n"
        "/remove - remove tracked token\n"
        "/addwallet - add wallet activity tracking\n"
        "/wallets - list tracked wallets\n"
        "/removewallet - remove tracked wallet\n"
        "/help - usage info"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "How to configure:\n"
        "1) /add\n"
        "2) Paste token contract (0x...)\n"
        "3) Pick one or several chains\n"
        "4) Set minimum alert in USD (e.g. 1000000 for $1M)\n\n"
        "Alert example: random wallet -> Binance with $1M+ flow.\n\n"
        "Wallet tracking:\n"
        "/addwallet - track any wallet activity on one selected chain\n"
        "/wallets - list tracked wallets\n"
        "/removewallet - remove wallet tracking"
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    temp_data[user_id] = {}
    await update.message.reply_text("Send token contract address (0x...).")
    return WAITING_CONTRACT


def build_single_chain_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("Ethereum", callback_data="wallet_chain_eth"), InlineKeyboardButton("BSC", callback_data="wallet_chain_bsc")],
        [InlineKeyboardButton("Polygon", callback_data="wallet_chain_polygon"), InlineKeyboardButton("Arbitrum", callback_data="wallet_chain_arbitrum")],
        [InlineKeyboardButton("Optimism", callback_data="wallet_chain_optimism")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def cmd_addwallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    temp_data[user_id] = {"flow": "wallet"}
    await update.message.reply_text("Send wallet address to track (0x...).")
    return WAITING_WALLET_ADDRESS


async def received_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    wallet = normalize_contract(update.message.text)
    if not wallet:
        await update.message.reply_text("Invalid wallet address. Expected 42 chars and 0x prefix.")
        return WAITING_WALLET_ADDRESS
    temp_data[user_id]["wallet"] = wallet
    await update.message.reply_text(
        "Select one chain for wallet monitoring:",
        reply_markup=build_single_chain_keyboard(),
    )
    return WAITING_WALLET_CHAIN


async def received_wallet_chain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in temp_data or "wallet" not in temp_data[user_id]:
        await query.edit_message_text("Session expired. Use /addwallet again.")
        return ConversationHandler.END
    chain = query.data.replace("wallet_chain_", "")
    if chain not in CHAINS:
        await query.answer("Unsupported chain", show_alert=True)
        return WAITING_WALLET_CHAIN

    wallet = temp_data[user_id]["wallet"]
    storage.upsert_wallet_watch(user_id, wallet, chain)
    await query.edit_message_text(
        f"✅ Wallet tracking enabled\nWallet: {wallet}\nChain: {CHAINS[chain].title}\n\n"
        "I will send alerts for every native/token transaction of this wallet."
    )
    temp_data.pop(user_id, None)
    return ConversationHandler.END


async def cmd_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = storage.list_user_wallet_watches(user_id)
    if not rows:
        await update.message.reply_text("No tracked wallets. Use /addwallet")
        return
    lines = ["Tracked wallets:"]
    for idx, row in enumerate(rows, start=1):
        lines.append(f"{idx}) {row['wallet']} on {CHAINS[row['chain']].title}")
    await update.message.reply_text("\n".join(lines))


async def cmd_removewallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = storage.list_user_wallet_watches(user_id)
    if not rows:
        await update.message.reply_text("No tracked wallets.")
        return
    keyboard = []
    for row in rows:
        keyboard.append(
            [InlineKeyboardButton(
                f"❌ {short_addr(row['wallet'])} ({CHAINS[row['chain']].title})",
                callback_data=f"wallet_remove::{row['wallet']}::{row['chain']}",
            )]
        )
    await update.message.reply_text("Select wallet to remove:", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_wallet_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, wallet, chain = query.data.split("::")
    ok = storage.remove_wallet_watch(user_id, wallet, chain)
    if ok:
        await query.edit_message_text("Wallet tracking removed.")
    else:
        await query.edit_message_text("Wallet watch not found.")


def build_chain_selector_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    def label(chain_key: str) -> str:
        mark = "✅ " if chain_key in selected else ""
        return f"{mark}{CHAINS[chain_key].title}"

    keyboard = [
        [
            InlineKeyboardButton(label("eth"), callback_data="chain_toggle_eth"),
            InlineKeyboardButton(label("bsc"), callback_data="chain_toggle_bsc"),
        ],
        [
            InlineKeyboardButton(label("polygon"), callback_data="chain_toggle_polygon"),
            InlineKeyboardButton(label("arbitrum"), callback_data="chain_toggle_arbitrum"),
        ],
        [
            InlineKeyboardButton(label("optimism"), callback_data="chain_toggle_optimism"),
        ],
        [InlineKeyboardButton("✅ Done", callback_data="chain_done")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def received_contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    contract = normalize_contract(update.message.text)
    if not contract:
        await update.message.reply_text("Invalid contract. Expected 42 chars and 0x prefix.")
        return WAITING_CONTRACT

    temp_data[user_id]["contract"] = contract
    temp_data[user_id]["chains"] = set()
    await update.message.reply_text(
        "Choose one or more chains, then press ✅ Done:",
        reply_markup=build_chain_selector_keyboard(temp_data[user_id]["chains"]),
    )
    return WAITING_CHAIN


async def received_chain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id not in temp_data:
        await query.edit_message_text("Session expired. Use /add again.")
        return ConversationHandler.END

    selected = temp_data[user_id].setdefault("chains", set())
    data = query.data

    if data == "chain_done":
        if not selected:
            await query.answer("Select at least one chain", show_alert=True)
            return WAITING_CHAIN
        selected_names = ", ".join(CHAINS[c].title for c in sorted(selected))
        await query.edit_message_text(
            f"Selected chains: {selected_names}\n\nEnter minimum USD threshold. Examples: 100000, 500K, 1M"
        )
        return WAITING_AMOUNT

    if not data.startswith("chain_toggle_"):
        await query.answer("Unsupported action", show_alert=True)
        return WAITING_CHAIN

    chain = data.replace("chain_toggle_", "")
    if chain not in CHAINS:
        await query.answer("Unsupported chain", show_alert=True)
        return WAITING_CHAIN

    if chain in selected:
        selected.remove(chain)
    else:
        selected.add(chain)

    await query.edit_message_reply_markup(
        reply_markup=build_chain_selector_keyboard(selected)
    )
    return WAITING_CHAIN


async def received_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    min_usd = parse_min_usd(update.message.text)
    if min_usd is None:
        await update.message.reply_text("Invalid amount. Use number >= 100. Example: 1000000")
        return WAITING_AMOUNT

    contract = temp_data[user_id]["contract"]
    chains = sorted(temp_data[user_id].get("chains", []))
    if not chains:
        await update.message.reply_text("No chains selected. Use /add again.")
        temp_data.pop(user_id, None)
        return ConversationHandler.END

    await update.message.reply_text("Checking token metadata and price on selected chains...")
    async with aiohttp.ClientSession() as session:
        summary_lines = []
        for chain in chains:
            symbol, name = await get_token_meta(contract, chain, session)
            price = await get_token_price(contract, chain, session)
            storage.upsert_watch(
                user_id=user_id,
                contract=contract,
                chain=chain,
                min_usd=min_usd,
                symbol=symbol,
                name=name,
            )
            summary_lines.append(
                f"• {CHAINS[chain].title}: {symbol} ({name}), price {f'${price:.6f}' if price > 0 else 'n/a'}"
            )

    await update.message.reply_text(
        "✅ Tracking saved for selected chains\n"
        f"Contract: {contract}\n"
        f"Min alert: {format_usd(min_usd)}\n\n"
        + "\n".join(summary_lines)
    )
    temp_data.pop(user_id, None)
    return ConversationHandler.END


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = storage.list_user_watches(user_id)
    if not rows:
        await update.message.reply_text("No tracked tokens. Use /add")
        return

    lines = ["Tracked tokens:"]
    for idx, row in enumerate(rows, start=1):
        lines.append(
            f"{idx}) {row['symbol'] or 'UNKNOWN'} on {CHAINS[row['chain']].title} | {format_usd(float(row['min_usd']))}\n"
            f"   {row['contract']}"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = storage.list_user_watches(user_id)
    if not rows:
        await update.message.reply_text("No tracked tokens.")
        return

    keyboard = []
    for row in rows:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"❌ {(row['symbol'] or 'UNKNOWN')} ({CHAINS[row['chain']].title})",
                    callback_data=f"remove::{row['contract']}::{row['chain']}",
                )
            ]
        )
    await update.message.reply_text("Select token to remove:", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    _, contract, chain = query.data.split("::")
    ok = storage.remove_user_watch(user_id, contract, chain)
    if ok:
        await query.edit_message_text("Removed.")
    else:
        await query.edit_message_text("Nothing to remove.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    temp_data.pop(user_id, None)
    await update.message.reply_text("Canceled.")
    return ConversationHandler.END


def validate_env() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    if not ETHERSCAN_KEY:
        raise RuntimeError("ETHERSCAN_API_KEY is not set")


def main() -> None:
    validate_env()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            WAITING_CONTRACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_contract)],
            WAITING_CHAIN: [CallbackQueryHandler(received_chain, pattern=r"^chain_")],
            WAITING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    wallet_conv = ConversationHandler(
        entry_points=[CommandHandler("addwallet", cmd_addwallet)],
        states={
            WAITING_WALLET_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_wallet_address)],
            WAITING_WALLET_CHAIN: [CallbackQueryHandler(received_wallet_chain, pattern=r"^wallet_chain_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("wallets", cmd_wallets))
    app.add_handler(CommandHandler("removewallet", cmd_removewallet))
    app.add_handler(CallbackQueryHandler(handle_remove, pattern=r"^remove::"))
    app.add_handler(CallbackQueryHandler(handle_wallet_remove, pattern=r"^wallet_remove::"))
    app.add_handler(conv)
    app.add_handler(wallet_conv)

    async def post_init(application: Application) -> None:
        asyncio.create_task(check_transfers(application))

    app.post_init = post_init

    logger.info("Starting bot polling")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
