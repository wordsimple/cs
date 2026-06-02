#!/usr/bin/env python3
# coding: utf-8
"""
ladan: Telegram mock finance bot for OTG-style testing flows.

This bot is intentionally limited to local mock execution. It does not log in to
real back-office systems or trigger real recharge/withdraw/audit actions.
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import signal
import shutil
import sys
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from telegram.error import NetworkError, TelegramError
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


BOT_NAME = "ladan"
APP_ENV = os.getenv("LADAN_ENV", "mock").strip().lower()
TG_BOT_TOKEN = os.getenv(
    "TG_BOT_TOKEN",
    "8864081579:AAGvx_5rWYDkGjB5LqJPMO5vWbCytEsidCc",
)
DEFAULT_LANGUAGE = "中文"
DEFAULT_COUNTRY = "印尼"
SUPPORTED_COUNTRIES = {
    "印尼": {"currency": "IDR", "code": "ID", "recharge_address": "IDR_AUTO_ADDRESS"},
    "泰国": {"currency": "THB", "code": "TH", "recharge_address": "THB_AUTO_ADDRESS"},
}
SUPPORTED_LANGUAGES = {"中文", "English"}
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "ladan_data"
STATE_FILE = DATA_DIR / "state.json"
VOUCHER_DIR = DATA_DIR / "vouchers"
FIXED_VOUCHER_SOURCE = BASE_DIR / "merchant_credential.png"
FALLBACK_VOUCHER_SOURCE = BASE_DIR / "zdhcsCL_final.png"

LOGIN_URL = "https://operator1.otgpaytest.com/#/login"
DEFAULT_USERNAME = "ceshi"
DEFAULT_PASSWORD = "qw123456"
DEFAULT_TOTP_SEED = "6gnkpcdu22be5pahlbvfvffmnxcqzjzv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(BOT_NAME)
logging.getLogger("telegram").setLevel(logging.CRITICAL)
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("httpcore").setLevel(logging.CRITICAL)
_shutdown_requested = False


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    VOUCHER_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_runtime_artifacts() -> None:
    ensure_data_dirs()
    try:
        store.save()
    except Exception:
        logger.exception("退出时保存状态失败。")


def request_shutdown(reason: str) -> None:
    global _shutdown_requested
    if _shutdown_requested:
        return
    _shutdown_requested = True
    print(f"\n正在停止 {BOT_NAME}，原因: {reason}")
    cleanup_runtime_artifacts()
    print("清理完成，已安全退出。")


def handle_signal(signum: int, frame: object | None) -> None:
    signal_name = signal.Signals(signum).name
    request_shutdown(signal_name)
    raise SystemExit(0)


atexit.register(cleanup_runtime_artifacts)
signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def resolve_voucher_source() -> Path | None:
    if FIXED_VOUCHER_SOURCE.exists():
        return FIXED_VOUCHER_SOURCE
    if FALLBACK_VOUCHER_SOURCE.exists():
        return FALLBACK_VOUCHER_SOURCE
    return None


def normalize_country(raw: str) -> str:
    value = raw.strip()
    aliases = {
        "cn": "印尼",
        "id": "印尼",
        "indo": "印尼",
        "indonesia": "印尼",
        "印尼盾": "印尼",
        "th": "泰国",
        "thai": "泰国",
        "thailand": "泰国",
        "泰": "泰国",
        "泰铢": "泰国",
    }
    lowered = value.lower()
    return aliases.get(lowered, value)


def normalize_language(raw: str) -> str:
    value = raw.strip()
    aliases = {
        "zh": "中文",
        "cn": "中文",
        "chinese": "中文",
        "中文": "中文",
        "en": "English",
        "english": "English",
    }
    return aliases.get(value.lower(), value)


def parse_amount(raw: str) -> str:
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("金额格式错误，请输入数字。") from exc
    if amount <= 0:
        raise ValueError("金额必须大于 0。")
    return format(amount.quantize(Decimal("0.01")), "f")


def format_record(record: dict[str, Any]) -> str:
    return (
        f"ID: {record['id']}\n"
        f"类型: {record['type']}\n"
        f"国家: {record['country']} ({record['currency']})\n"
        f"语言: {record['language']}\n"
        f"商户: {record['merchant_name']}\n"
        f"金额: {record['amount']}\n"
        f"地址: {record['address']}\n"
        f"状态: {record['status']}\n"
        f"审核原因: {record.get('audit_reason') or '-'}\n"
        f"备注: {record.get('remark') or '-'}\n"
        f"凭证: {record.get('voucher_path') or '-'}\n"
        f"创建时间: {record['created_at']}\n"
        f"更新时间: {record['updated_at']}"
    )


@dataclass
class UserContext:
    country: str = DEFAULT_COUNTRY
    language: str = DEFAULT_LANGUAGE
    last_record_id: str | None = None
    updated_at: str = field(default_factory=now_iso)


@dataclass
class State:
    users: dict[str, UserContext] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FinanceRequest:
    user_id: int
    record_type: str
    country: str
    language: str
    merchant_name: str
    amount: str
    decision: str
    address: str
    remark: str = "一步到位自动处理"


class FinanceAdapter(ABC):
    @property
    @abstractmethod
    def env_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def create_and_audit(self, req: FinanceRequest) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_record(self, record_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abstractmethod
    def list_records(
        self,
        *,
        country: str | None = None,
        record_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        ensure_data_dirs()
        self.state = self._load()

    def _load(self) -> State:
        if not self.path.exists():
            return State()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            users = {
                key: UserContext(**value)
                for key, value in data.get("users", {}).items()
            }
            records = data.get("records", [])
            return State(users=users, records=records)
        except Exception:
            logger.exception("状态文件读取失败，将使用空状态。")
            return State()

    def save(self) -> None:
        payload = {
            "users": {key: asdict(value) for key, value in self.state.users.items()},
            "records": self.state.records,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_user_context(self, user_id: int) -> UserContext:
        key = str(user_id)
        ctx = self.state.users.get(key)
        if ctx is None:
            ctx = UserContext()
            self.state.users[key] = ctx
            self.save()
        return ctx

    def update_user_context(
        self,
        user_id: int,
        *,
        country: str | None = None,
        language: str | None = None,
        last_record_id: str | None = None,
    ) -> UserContext:
        ctx = self.get_user_context(user_id)
        if country is not None:
            ctx.country = country
        if language is not None:
            ctx.language = language
        if last_record_id is not None:
            ctx.last_record_id = last_record_id
        ctx.updated_at = now_iso()
        self.save()
        return ctx

    def add_record(self, record: dict[str, Any]) -> dict[str, Any]:
        self.state.records.append(record)
        self.save()
        return record

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        for record in reversed(self.state.records):
            if record["id"] == record_id:
                return record
        return None

    def list_records(
        self,
        *,
        country: str | None = None,
        record_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        results = list(reversed(self.state.records))
        if country is not None:
            results = [item for item in results if item["country"] == country]
        if record_type is not None:
            results = [item for item in results if item["type"] == record_type]
        return results[:limit]


store = StateStore(STATE_FILE)


def user_label(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "unknown"
    parts = [str(user.id)]
    if user.username:
        parts.append(f"@{user.username}")
    return " ".join(parts)


def create_voucher_copy(record_id: str) -> str:
    source = resolve_voucher_source()
    if source is None:
        return "未找到本地固定凭证图片"
    target = VOUCHER_DIR / f"{record_id}{source.suffix.lower()}"
    shutil.copy2(source, target)
    return str(target)


def build_record(
    *,
    user_id: int,
    record_type: str,
    country: str,
    language: str,
    merchant_name: str,
    amount: str,
    address: str,
    remark: str,
) -> dict[str, Any]:
    record_id = f"{record_type[:1].upper()}{uuid4().hex[:10]}"
    created_at = now_iso()
    currency = SUPPORTED_COUNTRIES[country]["currency"]
    record = {
        "id": record_id,
        "type": record_type,
        "country": country,
        "country_code": SUPPORTED_COUNTRIES[country]["code"],
        "currency": currency,
        "language": language,
        "merchant_name": merchant_name,
        "amount": amount,
        "address": address,
        "remark": remark,
        "status": "待审核",
        "audit_reason": "",
        "voucher_path": create_voucher_copy(record_id),
        "mock_login_url": LOGIN_URL,
        "mock_operator": DEFAULT_USERNAME,
        "mock_totp_seed": DEFAULT_TOTP_SEED,
        "created_by": str(user_id),
        "created_at": created_at,
        "updated_at": created_at,
    }
    return record


def apply_audit(record: dict[str, Any], *, decision: str, reason: str) -> dict[str, Any]:
    if record["status"] != "待审核":
        raise ValueError(f"当前记录状态为 {record['status']}，不能重复审核。")
    record["status"] = "审核通过" if decision == "approve" else "审核拒绝"
    record["audit_reason"] = reason
    record["updated_at"] = now_iso()
    store.save()
    return record


class MockFinanceAdapter(FinanceAdapter):
    @property
    def env_name(self) -> str:
        return "mock"

    def create_and_audit(self, req: FinanceRequest) -> dict[str, Any]:
        record = build_record(
            user_id=req.user_id,
            record_type=req.record_type,
            country=req.country,
            language=req.language,
            merchant_name=req.merchant_name,
            amount=req.amount,
            address=req.address,
            remark=req.remark,
        )
        store.add_record(record)
        apply_audit(
            record,
            decision=req.decision,
            reason="一步到位自动审核",
        )
        return record

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        return store.get_record(record_id)

    def list_records(
        self,
        *,
        country: str | None = None,
        record_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return store.list_records(country=country, record_type=record_type, limit=limit)


class RealFinanceAdapter(FinanceAdapter):
    @property
    def env_name(self) -> str:
        return "real"

    def _unavailable(self) -> RuntimeError:
        return RuntimeError(
            "real 环境适配器仅作为内部预留接口，当前仓库未提供真实后台接入实现。"
        )

    def create_and_audit(self, req: FinanceRequest) -> dict[str, Any]:
        raise self._unavailable()

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        raise self._unavailable()

    def list_records(
        self,
        *,
        country: str | None = None,
        record_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        raise self._unavailable()


def build_finance_adapter(app_env: str) -> FinanceAdapter:
    if app_env == "mock":
        return MockFinanceAdapter()
    if app_env == "real":
        return RealFinanceAdapter()
    raise ValueError(f"不支持的 LADAN_ENV: {app_env}")


adapter = build_finance_adapter(APP_ENV)


def help_text() -> str:
    return (
        "ladan 财务机器人\n\n"
        f"当前环境: {adapter.env_name}\n\n"
        "指令:\n"
        "/start - 启动说明\n"
        "/help - 查看帮助\n"
        "/status - 查看当前上下文\n"
        "/set_country 印尼|泰国 - 切换国家\n"
        "/set_language 中文|English - 切换语言\n"
        "/recharge 国家 商户 金额 通过|拒绝 - 一步完成充值和审核\n"
        "/withdraw 国家 商户 金额 通过|拒绝 - 一步完成提现和审核\n"
        "/list [recharge|withdraw] - 查看当前国家最近记录\n"
        "/detail 记录ID - 查看记录详情\n\n"
        "示例:\n"
        "/recharge 泰国 merchantA 300 通过\n"
        "/withdraw 印尼 merchantB 120 拒绝\n"
    )


def normalize_audit_decision(raw: str) -> str:
    value = raw.strip()
    aliases = {
        "通过": "approve",
        "拒绝": "reject",
    }
    normalized = aliases.get(value)
    if normalized is None:
        raise ValueError("审核结果仅支持: 通过、拒绝")
    return normalized


def execute_local_command(user_id: int, raw_text: str) -> str:
    text = raw_text.strip()
    if not text:
        return "请输入命令，输入 /help 查看说明。"
    if not text.startswith("/"):
        return "命令需以 / 开头，输入 /help 查看说明。"

    parts = text.split()
    command = parts[0].lower()
    args = parts[1:]
    user_ctx = store.get_user_context(user_id)

    if command in {"/start", "/help"}:
        return help_text()
    if command == "/status":
        voucher = resolve_voucher_source()
        return (
            "当前 mock 上下文:\n"
            f"环境: {adapter.env_name}\n"
            f"国家: {user_ctx.country}\n"
            f"币种: {SUPPORTED_COUNTRIES[user_ctx.country]['currency']}\n"
            f"语言: {user_ctx.language}\n"
            f"最后记录ID: {user_ctx.last_record_id or '-'}\n"
            f"固定凭证源: {voucher or '未找到'}\n"
            f"后台地址(仅记录用途): {LOGIN_URL}"
        )
    if command == "/set_country":
        if not args:
            return "用法: /set_country 印尼|泰国"
        country = normalize_country(args[0])
        if country not in SUPPORTED_COUNTRIES:
            return "仅支持: 印尼、泰国"
        store.update_user_context(user_id, country=country)
        return f"国家已切换为 {country}，币种 {SUPPORTED_COUNTRIES[country]['currency']}。"
    if command == "/set_language":
        if not args:
            return "用法: /set_language 中文|English"
        language = normalize_language(args[0])
        if language not in SUPPORTED_LANGUAGES:
            return "仅支持: 中文、English"
        store.update_user_context(user_id, language=language)
        return f"语言已切换为 {language}。"
    if command in {"/recharge", "/withdraw"}:
        if len(args) != 4:
            return f"用法: {command} 国家 商户 金额 通过|拒绝"
        country = normalize_country(args[0])
        if country not in SUPPORTED_COUNTRIES:
            return "国家仅支持: 印尼、泰国"
        merchant_name = args[1]
        amount_raw = args[2]
        try:
            amount = parse_amount(amount_raw)
        except ValueError as exc:
            return str(exc)
        try:
            decision = normalize_audit_decision(args[3])
        except ValueError as exc:
            return str(exc)
        record_type = command.removeprefix("/")
        address = (
            SUPPORTED_COUNTRIES[country]["recharge_address"]
            if record_type == "recharge"
            else "123"
        )
        req = FinanceRequest(
            user_id=user_id,
            record_type=record_type,
            country=country,
            language=user_ctx.language,
            merchant_name=merchant_name,
            amount=amount,
            decision=decision,
            address=address,
        )
        try:
            record = adapter.create_and_audit(req)
        except Exception as exc:
            return f"执行失败: {exc}"
        store.update_user_context(user_id, country=country, last_record_id=record["id"])
        return (
            f"{'充值' if record_type == 'recharge' else '提现'}记录已创建并审核{'通过' if decision == 'approve' else '拒绝'}:\n"
            + format_record(record)
        )
    if command == "/list":
        record_type = None
        if args:
            candidate = args[0].strip().lower()
            if candidate not in {"recharge", "withdraw"}:
                return "可选参数仅支持: recharge 或 withdraw"
            record_type = candidate
        try:
            records = adapter.list_records(country=user_ctx.country, record_type=record_type, limit=10)
        except Exception as exc:
            return f"执行失败: {exc}"
        if not records:
            return "当前筛选条件下暂无记录。"
        lines = [
            f"{item['id']} | {item['type']} | {item['merchant_name']} | {item['amount']} {item['currency']} | {item['status']}"
            for item in records
        ]
        return f"最近记录 ({user_ctx.country}):\n" + "\n".join(lines)
    if command == "/detail":
        if not args:
            return "用法: /detail 记录ID"
        try:
            record = adapter.get_record(args[0])
        except Exception as exc:
            return f"执行失败: {exc}"
        if record is None:
            return f"未找到记录: {args[0]}"
        return format_record(record)
    if command in {"/exit", "/quit"}:
        raise EOFError
    return "未知命令，输入 /help 查看说明。"


def run_local_repl() -> None:
    ensure_data_dirs()
    print("ladan 离线交互模式已启动。")
    print("当前环境无法依赖 Telegram 时，可直接在终端输入命令。")
    print("输入 /help 查看说明，输入 /exit 退出。")
    print()
    local_user_id = 1
    while True:
        try:
            raw = input("ladan> ")
        except EOFError:
            print()
            request_shutdown("EOF")
            break
        try:
            result = execute_local_command(local_user_id, raw)
        except EOFError:
            request_shutdown("用户退出")
            break
        except Exception as exc:
            print(f"执行失败: {exc}")
            continue
        print(result)
        print()


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    user_ctx = store.get_user_context(update.effective_user.id)
    await update.message.reply_text(
        "机器人已启动。\n"
        f"当前环境: {adapter.env_name}\n"
        "当前仓库默认提供 mock 适配器；real 适配器仅预留接口。\n\n"
        f"当前国家: {user_ctx.country}\n"
        f"当前语言: {user_ctx.language}\n"
        f"固定凭证: {resolve_voucher_source() or '未找到图片'}\n\n"
        f"{help_text()}"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(help_text())


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    user_ctx = store.get_user_context(update.effective_user.id)
    voucher = resolve_voucher_source()
    await update.message.reply_text(
        "当前 mock 上下文:\n"
        f"环境: {adapter.env_name}\n"
        f"用户: {user_label(update)}\n"
        f"国家: {user_ctx.country}\n"
        f"币种: {SUPPORTED_COUNTRIES[user_ctx.country]['currency']}\n"
        f"语言: {user_ctx.language}\n"
        f"最后记录ID: {user_ctx.last_record_id or '-'}\n"
        f"固定凭证源: {voucher or '未找到'}\n"
        f"后台地址(仅记录用途): {LOGIN_URL}"
    )


async def cmd_set_country(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    if not ctx.args:
        await update.message.reply_text("用法: /set_country 印尼|泰国")
        return
    country = normalize_country(ctx.args[0])
    if country not in SUPPORTED_COUNTRIES:
        await update.message.reply_text("仅支持: 印尼、泰国")
        return
    store.update_user_context(update.effective_user.id, country=country)
    await update.message.reply_text(
        f"国家已切换为 {country}，币种 {SUPPORTED_COUNTRIES[country]['currency']}。"
    )


async def cmd_set_language(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    if not ctx.args:
        await update.message.reply_text("用法: /set_language 中文|English")
        return
    language = normalize_language(ctx.args[0])
    if language not in SUPPORTED_LANGUAGES:
        await update.message.reply_text("仅支持: 中文、English")
        return
    store.update_user_context(update.effective_user.id, language=language)
    await update.message.reply_text(f"语言已切换为 {language}。")


async def create_finance_record(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    record_type: str,
) -> None:
    if update.message is None or update.effective_user is None:
        return
    if len(ctx.args) != 4:
        await update.message.reply_text(
            f"用法: /{record_type} 国家 商户 金额 通过|拒绝"
        )
        return

    country = normalize_country(ctx.args[0])
    if country not in SUPPORTED_COUNTRIES:
        await update.message.reply_text("国家仅支持: 印尼、泰国")
        return
    merchant_name = ctx.args[1]
    amount_raw = ctx.args[2]

    try:
        amount = parse_amount(amount_raw)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    try:
        decision = normalize_audit_decision(ctx.args[3])
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    user_ctx = store.get_user_context(update.effective_user.id)
    address = (
        SUPPORTED_COUNTRIES[country]["recharge_address"]
        if record_type == "recharge"
        else "123"
    )
    req = FinanceRequest(
        user_id=update.effective_user.id,
        record_type=record_type,
        country=country,
        language=user_ctx.language,
        merchant_name=merchant_name,
        amount=amount,
        decision=decision,
        address=address,
    )
    try:
        record = adapter.create_and_audit(req)
    except Exception as exc:
        await update.message.reply_text(f"执行失败: {exc}")
        return
    store.update_user_context(
        update.effective_user.id,
        country=country,
        last_record_id=record["id"],
    )
    await update.message.reply_text(
        f"{'充值' if record_type == 'recharge' else '提现'}记录已创建并审核{'通过' if decision == 'approve' else '拒绝'}:\n"
        f"{format_record(record)}"
    )


async def cmd_recharge(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await create_finance_record(update, ctx, record_type="recharge")


async def cmd_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await create_finance_record(update, ctx, record_type="withdraw")


async def handle_audit(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    decision: str,
) -> None:
    if update.message is None:
        return
    if len(ctx.args) < 2:
        await update.message.reply_text(
            f"用法: /{'approve' if decision == 'approve' else 'reject'} 记录ID 原因"
        )
        return
    record_id = ctx.args[0]
    reason = " ".join(ctx.args[1:]).strip()
    try:
        record = adapter.get_record(record_id)
    except Exception as exc:
        await update.message.reply_text(f"执行失败: {exc}")
        return
    if record is None:
        await update.message.reply_text(f"未找到记录: {record_id}")
        return
    try:
        apply_audit(record, decision=decision, reason=reason)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_text(
        f"审核已完成: {'通过' if decision == 'approve' else '拒绝'}\n"
        f"{format_record(record)}"
    )


async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text("当前已改为一步到位模式，请直接使用 /recharge 或 /withdraw。")


async def cmd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text("当前已改为一步到位模式，请直接使用 /recharge 或 /withdraw。")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    user_ctx = store.get_user_context(update.effective_user.id)
    record_type = None
    if ctx.args:
        candidate = ctx.args[0].strip().lower()
        if candidate not in {"recharge", "withdraw"}:
            await update.message.reply_text("可选参数仅支持: recharge 或 withdraw")
            return
        record_type = candidate
    try:
        records = adapter.list_records(country=user_ctx.country, record_type=record_type, limit=10)
    except Exception as exc:
        await update.message.reply_text(f"执行失败: {exc}")
        return
    if not records:
        await update.message.reply_text("当前筛选条件下暂无记录。")
        return
    lines = [
        f"{item['id']} | {item['type']} | {item['merchant_name']} | {item['amount']} {item['currency']} | {item['status']}"
        for item in records
    ]
    await update.message.reply_text(
        f"最近记录 ({user_ctx.country}):\n" + "\n".join(lines)
    )


async def cmd_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not ctx.args:
        await update.message.reply_text("用法: /detail 记录ID")
        return
    try:
        record = adapter.get_record(ctx.args[0])
    except Exception as exc:
        await update.message.reply_text(f"执行失败: {exc}")
        return
    if record is None:
        await update.message.reply_text(f"未找到记录: {ctx.args[0]}")
        return
    await update.message.reply_text(format_record(record))


def main() -> None:
    parser = argparse.ArgumentParser(description="ladan mock finance bot")
    parser.add_argument(
        "--local-demo",
        action="store_true",
        help="不连接 Telegram，只在本地输出 mock 模式说明",
    )
    parser.add_argument(
        "--local-cli",
        action="store_true",
        help="不连接 Telegram，进入本地交互命令模式",
    )
    args = parser.parse_args()

    if args.local_demo:
        ensure_data_dirs()
        voucher = resolve_voucher_source()
        print("ladan 本地演示模式已启动。")
        print(f"数据目录: {DATA_DIR}")
        print(f"状态文件: {STATE_FILE}")
        print(f"固定凭证: {voucher or '未找到'}")
        print()
        print(help_text())
        request_shutdown("local-demo 完成")
        return
    if args.local_cli:
        run_local_repl()
        return

    app = Application.builder().token(TG_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("set_country", cmd_set_country))
    app.add_handler(CommandHandler("set_language", cmd_set_language))
    app.add_handler(CommandHandler("recharge", cmd_recharge))
    app.add_handler(CommandHandler("withdraw", cmd_withdraw))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("detail", cmd_detail))

    logger.info("ladan mock finance bot started")
    try:
        app.run_polling(drop_pending_updates=True)
        request_shutdown("轮询结束")
    except NetworkError as exc:
        print(
            "机器人启动失败：当前环境无法连接 Telegram 服务器。\n"
            "已自动切换到离线交互模式。\n"
            "如果你只想看说明，也可以运行：python3 ladan.py --local-demo\n"
            f"原始错误: {exc}\n"
        )
        run_local_repl()
    except TelegramError as exc:
        print(f"Telegram 启动失败: {exc}")
        request_shutdown("TelegramError")
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        request_shutdown("KeyboardInterrupt")
    except SystemExit:
        raise
    except Exception as exc:
        print(f"程序异常退出: {exc}")
        request_shutdown("未处理异常")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        raise exc
    except KeyboardInterrupt:
        request_shutdown("KeyboardInterrupt")
        sys.exit(0)
