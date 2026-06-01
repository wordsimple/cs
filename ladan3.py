#!/usr/bin/env python3
# coding: utf-8
"""
ladan3: Telegram finance bot with real backend submit/audit automation.

该机器人保留原有 /recharge /withdraw /set_country /set_language /list /detail
等指令风格，但实际执行走 OTG 后台浏览器自动化，支持固定上传商户凭证，
并对新增记录执行审核通过/拒绝流程。
"""

from __future__ import annotations

import argparse
import atexit
import asyncio
import json
import logging
import os
import signal
import sys
import time
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

import pyotp
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from telegram import Update
from telegram.error import Conflict, NetworkError, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


BOT_NAME = "ladan3"
APP_ENV = os.getenv("LADAN_ENV", "real").strip().lower()
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
DEFAULT_LANGUAGE = "中文"
DEFAULT_COUNTRY = "印尼"
DEFAULT_HEADLESS = True
SUPPORTED_COUNTRIES = {
    "印尼": {
        "currency": "IDR",
        "currency_label": "印尼盾",
        "code": "ID",
        "recharge_address": "IDR_AUTO_ADDRESS",
    },
    "泰国": {
        "currency": "THB",
        "currency_label": "泰铢",
        "code": "TH",
        "recharge_address": "THB_AUTO_ADDRESS",
    },
}
SUPPORTED_LANGUAGES = {"中文", "English"}
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "ladan3_data"
STATE_FILE = DATA_DIR / "state.json"
LOGIN_URL = "https://operator1.otgpaytest.com/#/login"
BASE_URL = "https://operator1.otgpaytest.com/#"
RECHARGE_URL = "/operate/recharge/list"
WITHDRAW_URL = "/operate/withdraw/list"
DEFAULT_USERNAME = os.getenv("OTG_USERNAME", "")
DEFAULT_PASSWORD = os.getenv("OTG_PASSWORD", "")
DEFAULT_TOTP_SEED = os.getenv("OTG_TOTP_SEED", "")
DEFAULT_REMARK = "tg测试"
CREDENTIAL_IMAGE_PATH = BASE_DIR / "merchant_credential.png"
SCREENSHOT_DIR = DATA_DIR / "screenshots"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(BOT_NAME)
logging.getLogger("telegram").setLevel(logging.CRITICAL)
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("httpcore").setLevel(logging.CRITICAL)
_shutdown_requested = False
_lock_acquired = False
_shared_driver = None
_shared_driver_headless: bool | None = None
_operation_lock: asyncio.Lock | None = None
_transaction_counter = 0
_active_transaction_id: str | None = None
_queued_transaction_ids: list[str] = []


def next_transaction_id() -> str:
    global _transaction_counter
    _transaction_counter += 1
    return f"TX{datetime.now().strftime('%Y%m%d%H%M%S')}-{_transaction_counter:04d}"


def get_operation_lock() -> asyncio.Lock:
    global _operation_lock
    if _operation_lock is None:
        _operation_lock = asyncio.Lock()
    return _operation_lock


def queue_status_text() -> str:
    active = _active_transaction_id or "-"
    waiting = ", ".join(_queued_transaction_ids) or "-"
    return f"当前事务:\n执行中: {active}\n等待中: {waiting}"


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def display_time(value: str | None = None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value.replace("T", " ")


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cleanup_runtime_artifacts() -> None:
    ensure_data_dirs()
    close_shared_driver()
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


def format_decimal(value: str | Decimal | None, places: str = "0.01") -> str | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text == "-":
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return format(number.quantize(Decimal(places)), "f")


def format_list_line(item: dict[str, Any]) -> str:
    type_label = "充值" if item["type"] == "recharge" else "提现"
    party_value = item.get("user_code") or item["merchant_name"]
    usdt_amount = item.get("usdt_amount") or item["amount"]
    local_amount = item.get("local_amount") or "-"
    exchange_rate = item.get("exchange_rate") or "-"
    return (
        f"{item['id']} | {type_label} | {party_value} | "
        f"{usdt_amount} USDT | 汇率 {exchange_rate} | "
        f"{local_amount} {item['currency']} | {item['status']} | "
        f"{display_time(item.get('updated_at'))}"
    )


def resolve_operation_address(record_type: str, country: str) -> str:
    if record_type == "withdraw":
        return "123"
    return SUPPORTED_COUNTRIES[country]["recharge_address"]


def format_record(record: dict[str, Any]) -> str:
    type_label = "充值" if record["type"] == "recharge" else "提现"
    party_label = "用户编码" if record["type"] == "recharge" else "商户"
    party_value = record.get("user_code") or record["merchant_name"]
    decision_label = "通过" if record["status"] == "审核通过" else "拒绝"
    screenshot = record.get("screenshot_path")
    voucher = record.get("voucher_path")
    id_label = "后台编号" if record.get("backend_record_id") else "本地编号"
    local_currency = record["currency"]
    local_amount_label = f"{type_label}{local_currency}"
    return (
        f"{id_label}: {record['id']}\n"
        f"业务: {type_label}\n"
        f"国家: {record['country']} ({record['currency']})\n"
        f"{party_label}: {party_value}\n"
        f"{type_label}USDT: {record.get('usdt_amount') or record['amount']} USDT\n"
        f"汇率: {record.get('exchange_rate') or '-'}\n"
        f"{local_amount_label}: {record.get('local_amount') or '-'} {local_currency}\n"
        f"审核: {decision_label}\n"
        f"状态: {record['status']}\n"
        f"提交: {record.get('submit_result') or '已提交'}\n"
        f"审核提交: {record.get('audit_result') or '已提交'}\n"
        f"凭证: {'已上传' if voucher else '未上传'}\n"
        f"截图: {Path(screenshot).name if screenshot else '-'}\n"
        f"时间: {display_time(record.get('updated_at'))}"
    )


def format_operation_summary(record: dict[str, Any]) -> str:
    type_label = "充值" if record["type"] == "recharge" else "提现"
    return f"{type_label}处理完成\n"


@dataclass
class UserContext:
    country: str = DEFAULT_COUNTRY
    language: str = DEFAULT_LANGUAGE
    headless: bool = DEFAULT_HEADLESS
    last_record_id: str | None = None
    updated_at: str = field(default_factory=now_iso)


@dataclass
class State:
    users: dict[str, UserContext] = field(default_factory=dict)
    operations: list[dict[str, Any]] = field(default_factory=list)


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
                key: UserContext(
                    country=value.get("country", DEFAULT_COUNTRY),
                    language=value.get("language", DEFAULT_LANGUAGE),
                    headless=bool(value.get("headless", DEFAULT_HEADLESS)),
                    last_record_id=value.get("last_record_id"),
                    updated_at=value.get("updated_at", now_iso()),
                )
                for key, value in data.get("users", {}).items()
            }
            operations = data.get("operations", [])
            return State(users=users, operations=operations)
        except Exception:
            logger.exception("状态文件读取失败，将使用空状态。")
            return State()

    def save(self) -> None:
        payload = {
            "users": {key: asdict(value) for key, value in self.state.users.items()},
            "operations": self.state.operations,
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

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
        headless: bool | None = None,
        last_record_id: str | None = None,
    ) -> UserContext:
        ctx = self.get_user_context(user_id)
        if country is not None:
            ctx.country = country
        if language is not None:
            ctx.language = language
        if headless is not None:
            ctx.headless = headless
        if last_record_id is not None:
            ctx.last_record_id = last_record_id
        ctx.updated_at = now_iso()
        self.save()
        return ctx

    def add_operation(self, record: dict[str, Any]) -> dict[str, Any]:
        self.state.operations.append(record)
        self.save()
        return record

    def get_operation(self, record_id: str) -> dict[str, Any] | None:
        for operation in reversed(self.state.operations):
            if operation["id"] == record_id:
                return operation
        return None

    def list_operations(
        self,
        *,
        country: str | None = None,
        record_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        results = list(reversed(self.state.operations))
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


def resolve_voucher_source() -> Path | None:
    if CREDENTIAL_IMAGE_PATH.exists():
        return CREDENTIAL_IMAGE_PATH
    return None


def build_operation(
    *,
    user_id: int,
    record_type: str,
    country: str,
    language: str,
    merchant_name: str,
    amount: str,
    decision: str,
    address: str,
    remark: str,
) -> dict[str, Any]:
    record_id = f"{record_type[:1].upper()}{uuid4().hex[:8]}"
    created_at = now_iso()
    status = "审核通过" if decision == "approve" else "审核拒绝"
    voucher = resolve_voucher_source()
    record = {
        "id": record_id,
        "type": record_type,
        "country": country,
        "country_code": SUPPORTED_COUNTRIES[country]["code"],
        "currency": SUPPORTED_COUNTRIES[country]["currency"],
        "language": language,
        "merchant_name": merchant_name,
        "amount": amount,
        "usdt_amount": amount,
        "exchange_rate": None,
        "local_amount": None,
        "address": address,
        "remark": remark,
        "status": status,
        "audit_reason": "机器人自动审核" if decision == "approve" else "机器人自动拒绝",
        "voucher_path": str(voucher) if voucher else None,
        "mock_login_url": LOGIN_URL,
        "mock_operator": DEFAULT_USERNAME,
        "mock_totp_seed": DEFAULT_TOTP_SEED,
        "created_by": str(user_id),
        "created_at": created_at,
        "updated_at": created_at,
    }
    return record


def create_driver(*, headless: bool = DEFAULT_HEADLESS):
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        },
    )
    return driver


def is_driver_alive(driver) -> bool:
    try:
        _ = driver.current_url
        return True
    except Exception:
        return False


def ensure_driver(driver=None, *, headless: bool = DEFAULT_HEADLESS):
    if driver and is_driver_alive(driver):
        return driver
    if driver:
        try:
            driver.quit()
        except Exception:
            pass
    new_driver = create_driver(headless=headless)
    login(new_driver, headless=headless)
    switch_to_chinese(new_driver)
    return new_driver


def close_shared_driver() -> None:
    global _shared_driver, _shared_driver_headless
    if _shared_driver is not None:
        try:
            _shared_driver.quit()
        except Exception:
            pass
    _shared_driver = None
    _shared_driver_headless = None


def ensure_shared_driver(*, headless: bool = DEFAULT_HEADLESS):
    global _shared_driver, _shared_driver_headless
    if _shared_driver is not None and _shared_driver_headless == headless and is_driver_alive(_shared_driver):
        try:
            if "login" not in (_shared_driver.current_url or "").lower():
                return _shared_driver
        except Exception:
            pass
    close_shared_driver()
    _shared_driver = ensure_driver(headless=headless)
    _shared_driver_headless = headless
    return _shared_driver


def login(driver=None, *, headless: bool = DEFAULT_HEADLESS):
    validate_backend_credentials()
    if driver is None:
        driver = create_driver(headless=headless)
    for attempt in range(5):
        try:
            driver.get(LOGIN_URL)
            wait = WebDriverWait(driver, 15)
            wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[placeholder='Username']"))
            )
            username_input = driver.find_element(By.CSS_SELECTOR, "input[placeholder='Username']")
            username_input.clear()
            username_input.send_keys(DEFAULT_USERNAME)

            password_input = driver.find_element(By.CSS_SELECTOR, "input[placeholder='Password']")
            password_input.clear()
            password_input.send_keys(DEFAULT_PASSWORD)

            totp_code = pyotp.TOTP(DEFAULT_TOTP_SEED).now()
            code_input = driver.find_element(By.CSS_SELECTOR, "input[placeholder='Google Authenticator']")
            code_input.clear()
            code_input.send_keys(totp_code)

            for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
                if "Login" in btn.text:
                    btn.click()
                    break

            wait.until(lambda d: "login" not in d.current_url.lower())
            logger.info("后台登录成功")
            return driver
        except Exception as exc:
            logger.warning("后台登录重试 %s: %s", attempt + 1, exc)
            try:
                driver.quit()
            except Exception:
                pass
            driver = create_driver(headless=headless)
    raise RuntimeError("后台登录失败")


def switch_to_chinese(driver) -> None:
    try:
        icon = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".international-icon"))
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", icon)
        try:
            icon.click()
        except Exception:
            driver.execute_script(
                "arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}))",
                icon,
            )
        time.sleep(0.4)

        target_item = None
        for item in driver.find_elements(By.CSS_SELECTOR, ".el-dropdown-menu__item"):
            if "中文" in item.text:
                target_item = item
                break

        if target_item is None:
            raise RuntimeError("未找到中文选项")

        if "is-disabled" in target_item.get_attribute("class") or target_item.get_attribute("aria-disabled") == "true":
            logger.info("当前已是中文，无需切换")
            return

        try:
            target_item.click()
        except Exception:
            driver.execute_script(
                "arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}))",
                target_item,
            )
        time.sleep(1)
        logger.info("已切换到中文")
        return
    except Exception as exc:
        logger.warning("切换中文失败: %s", exc)
        raise


def switch_to_country(driver, country: str) -> None:
    try:
        target_label = SUPPORTED_COUNTRIES[country]["currency_label"]
        currency_input = driver.find_element(By.CSS_SELECTOR, ".currency-select input, .currency-select .el-input__inner")
        driver.execute_script("arguments[0].click()", currency_input)
        time.sleep(0.4)
        aliases = [target_label, SUPPORTED_COUNTRIES[country]["currency"], country]
        if country == "泰国":
            aliases.append("Thailand")
        if country == "印尼":
            aliases.extend(["Indonesia", "印尼盾"])
        for item in driver.find_elements(By.CSS_SELECTOR, ".el-select-dropdown__item"):
            item_text = item.text or item.get_attribute("innerText") or ""
            if any(alias in item_text for alias in aliases):
                driver.execute_script("arguments[0].click()", item)
                time.sleep(2)
                logger.info("已切换地区: %s", country)
                return
        raise RuntimeError(f"未找到货币选项: {target_label}")
    except Exception as exc:
        logger.warning("切换国家失败: %s", exc)
        raise


def get_totp_code() -> str:
    if not DEFAULT_TOTP_SEED:
        raise RuntimeError("缺少环境变量 OTG_TOTP_SEED")
    return pyotp.TOTP(DEFAULT_TOTP_SEED).now()


def validate_backend_credentials() -> None:
    missing = []
    if not DEFAULT_USERNAME:
        missing.append("OTG_USERNAME")
    if not DEFAULT_PASSWORD:
        missing.append("OTG_PASSWORD")
    if not DEFAULT_TOTP_SEED:
        missing.append("OTG_TOTP_SEED")
    if missing:
        raise RuntimeError(f"缺少后台环境变量: {', '.join(missing)}")


def get_form_item_by_label(driver, label_text: str):
    for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
        if dialog.is_displayed():
            for item in dialog.find_elements(By.CSS_SELECTOR, ".el-form-item"):
                try:
                    label = item.find_element(By.CSS_SELECTOR, ".el-form-item__label").text.strip()
                    if label_text in label or label.endswith(label_text):
                        return item
                except Exception:
                    pass
    return None


def select_el_dropdown(driver, form_item, option_text: str | None = None, index: int = 0):
    try:
        el_select = form_item.find_element(By.CSS_SELECTOR, ".el-select")

        def is_disabled(opt) -> bool:
            aria_disabled = (opt.get_attribute("aria-disabled") or "").strip().lower()
            classes = (opt.get_attribute("class") or "").lower()
            return aria_disabled == "true" or "is-disabled" in classes

        def is_visible(opt) -> bool:
            try:
                return bool(
                    driver.execute_script(
                        "return arguments[0].offsetParent !== null && window.getComputedStyle(arguments[0]).visibility !== 'hidden' && window.getComputedStyle(arguments[0]).display !== 'none';",
                        opt,
                    )
                )
            except Exception:
                return False

        def option_label(opt) -> str:
            return (opt.get_attribute("innerText") or opt.text or opt.get_attribute("textContent") or "").strip()

        def click_option(opt) -> None:
            try:
                span = opt.find_element(By.TAG_NAME, "span")
                driver.execute_script(
                    "arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}))",
                    span,
                )
            except Exception:
                driver.execute_script(
                    "arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}))",
                    opt,
                )

        def visible_option_list():
            options = driver.find_elements(By.CSS_SELECTOR, ".el-select-dropdown__item")
            return [opt for opt in options if is_visible(opt) and not is_disabled(opt)]

        def find_option(options, text: str):
            normalized = text.strip().lower()
            exact_candidates = []
            contains_candidates = []
            for opt in options:
                label = option_label(opt)
                label_normalized = label.lower()
                if label_normalized == normalized:
                    exact_candidates.append(opt)
                elif normalized in label_normalized:
                    contains_candidates.append(opt)
            return (exact_candidates or contains_candidates or [None])[0]

        driver.execute_script("arguments[0].click()", el_select)
        time.sleep(0.6)
        if option_text:
            try:
                input_el = form_item.find_element(By.CSS_SELECTOR, ".el-select input")
                set_input_value(driver, input_el, option_text)
                time.sleep(1.0)
            except Exception:
                pass

            deadline = time.time() + 5
            while time.time() < deadline:
                selected = find_option(visible_option_list(), option_text)
                if selected is not None:
                    click_option(selected)
                    time.sleep(0.3)
                    return True
                time.sleep(0.3)
            driver.execute_script("document.body.click()")
            time.sleep(0.2)
            return False

        visible_options = visible_option_list()
        if 0 <= index < len(visible_options):
            click_option(visible_options[index])
            time.sleep(0.3)
            return True
        driver.execute_script("document.body.click()")
        time.sleep(0.2)
        return False
    except Exception:
        try:
            driver.execute_script("document.body.click()")
            time.sleep(0.2)
        except Exception:
            pass
        return False


def set_input_value(driver, element, value: str) -> None:
    try:
        element.clear()
        element.send_keys(value)
    except Exception:
        driver.execute_script(
            """
            var el = arguments[0];
            var value = arguments[1];
            el.removeAttribute('readonly');
            el.removeAttribute('disabled');
            el.value = value;
            var events = ['input', 'change', 'blur'];
            events.forEach(function(eventType) {
                var event = new Event(eventType, { bubbles: true });
                el.dispatchEvent(event);
            });
            var vueEvent = new Event('input', { bubbles: true });
            el.dispatchEvent(vueEvent);
        """,
            element,
            value,
        )


def close_dialog(driver) -> bool:
    try:
        for btn in driver.find_elements(By.CSS_SELECTOR, ".el-dialog__headerbtn"):
            if btn.is_displayed():
                btn.click()
                time.sleep(0.3)
                return True
        for btn in driver.find_elements(By.CSS_SELECTOR, ".el-dialog button"):
            if "取消" in btn.text and btn.is_displayed():
                btn.click()
                time.sleep(0.3)
                return True
    except Exception:
        pass
    return False


def wait_and_click_button(driver, selector: str, text: str, exact: bool = False, timeout: int = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for btn in driver.find_elements(By.CSS_SELECTOR, selector):
                btn_text = btn.text.strip()
                if (btn_text == text if exact else text in btn_text) and btn.is_displayed() and btn.is_enabled():
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    driver.execute_script("arguments[0].click()", btn)
                    time.sleep(0.5)
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def visible_button_texts(driver) -> list[str]:
    texts: list[str] = []
    seen: set[str] = set()
    for btn in driver.find_elements(By.CSS_SELECTOR, "button, .el-button"):
        try:
            if not btn.is_displayed() or not btn.is_enabled():
                continue
            text = " ".join((btn.text or btn.get_attribute("innerText") or "").split())
            if text and text not in seen:
                seen.add(text)
                texts.append(text)
        except Exception:
            pass
    return texts


def wait_and_click_any_button(driver, texts: list[str], *, timeout: int = 15) -> str | None:
    deadline = time.time() + timeout
    normalized_targets = [text.strip().lower() for text in texts if text.strip()]
    while time.time() < deadline:
        try:
            for btn in driver.find_elements(By.CSS_SELECTOR, "button, .el-button"):
                btn_text = " ".join((btn.text or btn.get_attribute("innerText") or "").split())
                if not btn_text or not btn.is_displayed() or not btn.is_enabled():
                    continue
                normalized = btn_text.lower()
                if any(target in normalized for target in normalized_targets):
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    driver.execute_script("arguments[0].click()", btn)
                    time.sleep(0.5)
                    return btn_text
        except StaleElementReferenceException:
            time.sleep(0.2)
        except Exception:
            pass
        time.sleep(0.3)
    return None


def click_withdraw_apply_button(driver) -> str:
    labels = [
        "申请下发",
        "提现申请",
        "申请提现",
        "申请提款",
        "新增提现",
        "新增提款",
        "下发",
        "Apply",
        "Apply Withdraw",
        "Withdraw Apply",
        "Create",
        "Add",
    ]
    clicked = wait_and_click_any_button(driver, labels, timeout=18)
    if clicked:
        return clicked
    buttons = ", ".join(visible_button_texts(driver)) or "未读取到可见按钮"
    debug_path = save_debug_snapshot(driver, "提现申请入口")
    raise RuntimeError(f"未找到提现申请按钮；可见按钮: {buttons}；调试截图: {Path(debug_path).name}")


def page_has_text(driver, text: str) -> bool:
    try:
        return text in (driver.find_element(By.TAG_NAME, "body").text or "")
    except Exception:
        return False


def navigate_to_page(driver, route: str) -> None:
    driver.get(f"{BASE_URL}{route}")
    deadline = time.time() + 6
    while time.time() < deadline:
        try:
            if route.split("/")[-1] in driver.current_url:
                break
        except Exception:
            pass
        time.sleep(0.2)
    time.sleep(0.2)


def wait_page_ready(driver, keywords: list[str], timeout: int = 8) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text or ""
            if any(keyword in body_text for keyword in keywords):
                return
            if driver.find_elements(By.CSS_SELECTOR, ".el-table, .el-form, button"):
                return
        except Exception:
            pass
        time.sleep(0.3)


def verify_form_fields(driver, labels: list[str]) -> tuple[list[str], list[str]]:
    found: list[str] = []
    missing: list[str] = []
    for label in labels:
        if get_form_item_by_label(driver, label):
            found.append(label)
        else:
            missing.append(label)
    return found, missing


def save_test_screenshot(driver, record_id: str) -> str:
    ensure_data_dirs()
    path = SCREENSHOT_DIR / f"{record_id}.png"
    driver.save_screenshot(str(path))
    return str(path)


def visible_dialogs(driver):
    return [dialog for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog") if dialog.is_displayed()]


def visible_action_scopes(driver):
    selectors = [
        ".el-dialog",
        ".el-drawer",
        ".el-message-box",
        ".el-popover",
        ".v-modal + *",
        "body",
    ]
    scopes = []
    seen: set[str] = set()
    for selector in selectors:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if not element.is_displayed():
                    continue
                key = element.id
                if key in seen:
                    continue
                seen.add(key)
                scopes.append(element)
            except Exception:
                pass
    return scopes


def button_label(button) -> str:
    parts = [
        button.text,
        button.get_attribute("innerText"),
        button.get_attribute("textContent"),
        button.get_attribute("aria-label"),
        button.get_attribute("title"),
        button.get_attribute("value"),
    ]
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()


def visible_button_debug(driver) -> str:
    labels = []
    for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
        try:
            if not btn.is_displayed() or not btn.is_enabled():
                continue
            label = button_label(btn) or "(空文本)"
            classes = btn.get_attribute("class") or "-"
            labels.append(f"{label}[{classes}]")
        except Exception:
            pass
    return "; ".join(labels[:30]) or "未发现可见可用按钮"


def visible_button_infos(driver) -> list[dict[str, str]]:
    infos: list[dict[str, str]] = []
    for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
        try:
            if not btn.is_displayed() or not btn.is_enabled():
                continue
            infos.append(
                {
                    "label": button_label(btn),
                    "class": btn.get_attribute("class") or "",
                    "html": btn.get_attribute("outerHTML") or "",
                }
            )
        except StaleElementReferenceException:
            continue
        except Exception:
            pass
    return infos


def click_visible_button_by_info(driver, info: dict[str, str]) -> bool:
    target_label = (info.get("label") or "").strip()
    target_class = info.get("class") or ""
    target_html = info.get("html") or ""
    for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
        try:
            if not btn.is_displayed() or not btn.is_enabled():
                continue
            label = button_label(btn)
            classes = btn.get_attribute("class") or ""
            html = btn.get_attribute("outerHTML") or ""
            if target_label and label != target_label:
                continue
            if target_class and classes != target_class:
                continue
            if not target_label and target_html and html != target_html:
                continue
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            driver.execute_script("arguments[0].click()", btn)
            return True
        except StaleElementReferenceException:
            continue
        except Exception:
            continue
    return False


def save_debug_snapshot(driver, prefix: str) -> str:
    ensure_data_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "_", prefix).strip("_") or "debug"
    screenshot_path = SCREENSHOT_DIR / f"{safe_prefix}_{stamp}.png"
    html_path = SCREENSHOT_DIR / f"{safe_prefix}_{stamp}.html"
    try:
        driver.save_screenshot(str(screenshot_path))
    except Exception:
        pass
    try:
        html_path.write_text(driver.page_source, encoding="utf-8")
    except Exception:
        pass
    return str(screenshot_path)


def collect_messages(driver) -> tuple[list[str], list[str]]:
    success_keywords = [
        "success",
        "successful",
        "audit pass",
        "audit no pass",
        "no pass",
        "recharge audit",
        "withdraw audit",
        "提交成功",
        "审核成功",
        "成功",
    ]
    error_keywords = ["error", "fail", "failed", "失败", "错误"]
    success: list[str] = []
    errors: list[str] = []
    for item in driver.find_elements(By.CSS_SELECTOR, ".el-message, .el-message--success, .el-message--error"):
        try:
            text = item.text.strip()
            if not text:
                continue
            classes = item.get_attribute("class") or ""
            normalized = text.lower()
            if "el-message--success" in classes or any(keyword in normalized for keyword in success_keywords):
                success.append(text)
            elif "el-message--error" in classes or any(keyword in normalized for keyword in error_keywords):
                errors.append(text)
        except StaleElementReferenceException:
            continue
        except Exception:
            pass
    return success, errors


def collect_form_errors(driver) -> list[str]:
    errors: list[str] = []
    for item in driver.find_elements(By.CSS_SELECTOR, ".el-form-item"):
        try:
            error_texts = [
                err.text.strip()
                for err in item.find_elements(By.CSS_SELECTOR, ".el-form-item__error")
                if err.text.strip()
            ]
            if not error_texts:
                continue
            label = ""
            try:
                label = item.find_element(By.CSS_SELECTOR, ".el-form-item__label").text.strip()
            except Exception:
                pass
            prefix = f"{label}: " if label else ""
            errors.extend(f"{prefix}{text}" for text in error_texts)
        except Exception:
            pass
    return errors


def click_confirm_message_box(driver) -> bool:
    try:
        alert = driver.switch_to.alert
        alert.accept()
        time.sleep(0.5)
        return True
    except Exception:
        pass
    for box in driver.find_elements(By.CSS_SELECTOR, ".el-message-box__wrapper, .el-message-box"):
        try:
            if not box.is_displayed():
                continue
            for btn in box.find_elements(By.CSS_SELECTOR, "button"):
                text = button_label(btn)
                if any(keyword in text for keyword in ["确定", "确认", "Sure", "Confirm", "OK"]):
                    driver.execute_script("arguments[0].click()", btn)
                    time.sleep(0.8)
                    return True
        except Exception:
            pass
    for scope in driver.find_elements(By.CSS_SELECTOR, ".el-popover, .el-popconfirm, .el-dialog__wrapper"):
        try:
            if not scope.is_displayed():
                continue
            scope_text = (scope.text or "").strip()
            if not any(keyword in scope_text for keyword in ["确认", "确定", "是否", "Sure", "Confirm", "reject", "approve"]):
                continue
            for btn in scope.find_elements(By.CSS_SELECTOR, "button"):
                text = button_label(btn)
                classes = btn.get_attribute("class") or ""
                if (
                    btn.is_displayed()
                    and btn.is_enabled()
                    and (
                        any(keyword in text for keyword in ["确定", "确认", "Sure", "Confirm", "OK"])
                        or "el-button--primary" in classes
                    )
                    and "取消" not in text
                    and "Cancel" not in text
                ):
                    driver.execute_script("arguments[0].click()", btn)
                    time.sleep(0.8)
                    return True
        except Exception:
            pass
    return False


def fill_text_field_in_item(driver, form_item, value: str) -> bool:
    textareas = form_item.find_elements(By.CSS_SELECTOR, "textarea")
    for textarea in textareas:
        if textarea.is_displayed():
            set_input_value(driver, textarea, value)
            return True
    inputs = form_item.find_elements(By.CSS_SELECTOR, "input")
    for inp in inputs:
        input_type = (inp.get_attribute("type") or "").lower()
        if inp.is_displayed() and input_type != "file":
            set_input_value(driver, inp, value)
            return True
    return False


def submit_visible_dialog(driver, button_texts: list[str], action_name: str) -> str:
    button_infos = visible_button_infos(driver)
    matching_buttons = [
        info
        for info in button_infos
        if any(text.lower() in (info.get("label") or "").lower() for text in button_texts)
    ]
    fallback_buttons = [
        info
        for info in button_infos
        if "el-button--primary" in (info.get("class") or "")
        and "取消" not in (info.get("label") or "")
        and "Cancel" not in (info.get("label") or "")
        and "Search" not in (info.get("label") or "")
        and "Reset" not in (info.get("label") or "")
    ]
    for info in matching_buttons or fallback_buttons:
        for _ in range(3):
            try:
                if not click_visible_button_by_info(driver, info):
                    time.sleep(0.2)
                    continue
                deadline = time.time() + 8
                last_form_errors: list[str] = []
                success_text = ""
                while time.time() < deadline:
                    if click_confirm_message_box(driver):
                        deadline = time.time() + 8
                    success, errors = collect_messages(driver)
                    if errors:
                        raise RuntimeError(f"{action_name}失败: {'; '.join(errors)}")
                    if success:
                        success_text = "; ".join(success)
                        return success_text
                    form_errors = collect_form_errors(driver)
                    if form_errors:
                        last_form_errors = form_errors
                    if not visible_dialogs(driver):
                        return success_text or f"{action_name}已提交，弹窗已关闭"
                    time.sleep(0.4)
                if success_text:
                    close_dialog(driver)
                    return success_text
                if last_form_errors:
                    raise RuntimeError(f"{action_name}校验失败: {'; '.join(last_form_errors)}")
                raise RuntimeError(f"{action_name}超时: 弹窗仍打开，未收到成功或错误提示")
            except RuntimeError:
                raise
            except StaleElementReferenceException:
                time.sleep(0.2)
                continue
            except Exception:
                continue
    debug_path = save_debug_snapshot(driver, action_name)
    raise RuntimeError(
        f"未找到{action_name}按钮。期望: {', '.join(button_texts)}；"
        f"可见按钮: {visible_button_debug(driver)}；调试截图: {Path(debug_path).name}"
    )


def wait_submit_result(driver, action_name: str, timeout: int = 12) -> str:
    deadline = time.time() + timeout
    success_text = ""
    last_form_errors: list[str] = []
    while time.time() < deadline:
        if click_confirm_message_box(driver):
            deadline = time.time() + timeout
        success, errors = collect_messages(driver)
        if errors:
            raise RuntimeError(f"{action_name}失败: {'; '.join(errors)}")
        if success:
            success_text = "; ".join(success)
            return success_text
        form_errors = collect_form_errors(driver)
        if form_errors:
            last_form_errors = form_errors
        if not visible_dialogs(driver):
            return success_text or f"{action_name}已提交，弹窗已关闭"
        time.sleep(0.4)
    if last_form_errors:
        raise RuntimeError(f"{action_name}校验失败: {'; '.join(last_form_errors)}")
    debug_path = save_debug_snapshot(driver, action_name)
    raise RuntimeError(
        f"{action_name}超时: 弹窗仍打开，未收到成功或错误提示；"
        f"可见按钮: {visible_button_debug(driver)}；调试截图: {Path(debug_path).name}"
    )


def submit_audit_action(driver, decision: str) -> str:
    action_texts = (
        ["通过", "同意", "Approve", "Approved", "Pass"]
        if decision == "approve"
        else ["审核不通过", "不通过", "拒绝", "No Pass", "NoPass", "Reject", "Rejected", "Fail", "Failed"]
    )
    for info in visible_button_infos(driver):
        label = info.get("label") or ""
        if any(text.lower() in label.lower() for text in action_texts):
            for _ in range(3):
                if click_visible_button_by_info(driver, info):
                    return wait_submit_result(driver, "提交审核")
                time.sleep(0.2)
    if decision == "reject":
        for info in visible_button_infos(driver):
            label = info.get("label") or ""
            if "不" in label and any(key in label for key in ["审核", "通过", "Pass", "pass"]):
                for _ in range(3):
                    if click_visible_button_by_info(driver, info):
                        return wait_submit_result(driver, "提交审核")
                    time.sleep(0.2)
    return submit_visible_dialog(driver, ["确定", "提交", "保存", "Sure", "Confirm", "Submit", "Save"], "提交审核")


def refresh_current_page(driver) -> None:
    try:
        driver.refresh()
        time.sleep(0.6)
    except Exception:
        pass


def normalize_table_text(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip()).lower()


def clean_number_text(value: str | None) -> str | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    return match.group(0).replace(",", "")


def table_header_cells(driver) -> list[str]:
    headers: list[str] = []
    selectors = [
        ".el-table__header-wrapper th .cell",
        ".el-table__fixed-header-wrapper th .cell",
    ]
    for selector in selectors:
        for cell in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                text = " ".join((cell.text or "").split())
                if text:
                    headers.append(text)
            except StaleElementReferenceException:
                return headers
            except Exception:
                pass
    return headers


def first_visible_table_cells(driver) -> list[str]:
    rows = driver.find_elements(By.CSS_SELECTOR, ".el-table__body tbody tr")
    for row in rows:
        try:
            if not row.is_displayed():
                continue
            cells = [
                " ".join((cell.text or "").split())
                for cell in row.find_elements(By.CSS_SELECTOR, "td .cell")
            ]
            cells = [cell for cell in cells if cell]
            if cells:
                return cells
        except StaleElementReferenceException:
            return []
        except Exception:
            pass
    return []


def map_table_fields(headers: list[str], cells: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    if not headers or not cells:
        return fields
    if len(headers) >= len(cells):
        headers = headers[-len(cells):]
    elif len(cells) > len(headers):
        cells = cells[-len(headers):]
    for header, cell in zip(headers, cells):
        key = " ".join(header.split())
        value = " ".join(cell.split())
        if key and value and key not in fields:
            fields[key] = value
    return fields


def field_by_keywords(fields: dict[str, str], keywords: list[str]) -> str | None:
    normalized_keywords = [normalize_table_text(keyword) for keyword in keywords]
    for label, value in fields.items():
        normalized_label = normalize_table_text(label)
        if all(keyword in normalized_label for keyword in normalized_keywords):
            return value
    return None


def first_field_by_any_keywords(fields: dict[str, str], keyword_groups: list[list[str]]) -> str | None:
    for keywords in keyword_groups:
        value = field_by_keywords(fields, keywords)
        if value:
            return value
    return None


def calculate_local_amount(usdt_amount: str | None, exchange_rate: str | None) -> str | None:
    if not usdt_amount or not exchange_rate:
        return None
    try:
        return format((Decimal(usdt_amount) * Decimal(exchange_rate)).quantize(Decimal("0.01")), "f")
    except InvalidOperation:
        return None


def amounts_close(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return abs(Decimal(left) - Decimal(right)) <= Decimal("0.01")
    except InvalidOperation:
        return False


def extract_first_row_details(driver, record_type: str, currency: str, command_amount: str) -> dict[str, Any]:
    deadline = time.time() + 5
    currency_keywords = {
        "IDR": ["idr", "印尼盾", "印尼"],
        "THB": ["thb", "泰铢", "泰"],
    }.get(currency, [currency.lower()])
    record_patterns = [re.compile(r"\b\d{16,}\b"), re.compile(r"\b[RWDT]\d{8,}\b")]
    while time.time() < deadline:
        headers = table_header_cells(driver)
        cells = first_visible_table_cells(driver)
        fields = map_table_fields(headers, cells)
        row_text = " ".join(cells)
        record_no = None
        record_field = first_field_by_any_keywords(
            fields,
            [
                ["单号"],
                ["订单"],
                ["交易号"],
                ["流水"],
                ["编号"],
                ["no"],
            ],
        )
        for source in [record_field, row_text]:
            if not source:
                continue
            for pattern in record_patterns:
                match = pattern.search(source)
                if match:
                    record_no = match.group(0)
                    break
            if record_no:
                break

        usdt_source = first_field_by_any_keywords(
            fields,
            [
                ["usdt"],
                ["美元"],
                ["金额", "u"],
                ["申请金额"],
                ["充值金额"] if record_type == "recharge" else ["提现金额"],
            ],
        )
        usdt_amount = format_decimal(clean_number_text(usdt_source)) or command_amount

        exchange_source = first_field_by_any_keywords(
            fields,
            [
                ["汇率"],
                ["exchange", "rate"],
                ["rate"],
            ],
        )
        exchange_rate = clean_number_text(exchange_source)

        local_source = None
        for keyword in currency_keywords:
            local_source = first_field_by_any_keywords(
                fields,
                [
                    [keyword, "金额"],
                    [keyword, "amount"],
                    [keyword],
                ],
            )
            if local_source:
                break
        if not local_source:
            local_source = first_field_by_any_keywords(
                fields,
                [
                    ["本地", "金额"],
                    ["法币", "金额"],
                    ["当地", "金额"],
                    ["实际", "金额"],
                    ["到账", "金额"],
                ],
            )
        local_amount = format_decimal(clean_number_text(local_source))
        if not local_amount:
            local_amount = calculate_local_amount(usdt_amount, exchange_rate)
        if record_type == "recharge" and len(cells) >= 11:
            # 充值列表也有横向列，表头可能错位；命令金额固定是 USDT。
            # 当前后台顺序: 本地金额、USDT 金额、汇率分别位于凭证/状态后的第 9-11 列。
            row_local_amount = format_decimal(clean_number_text(cells[8]))
            row_usdt_amount = format_decimal(clean_number_text(cells[9]))
            row_exchange_rate = clean_number_text(cells[10])
            exchange_rate = row_exchange_rate or exchange_rate
            usdt_amount = command_amount
            if row_usdt_amount and amounts_close(row_usdt_amount, command_amount):
                usdt_amount = row_usdt_amount
            calculated_local = calculate_local_amount(usdt_amount, exchange_rate)
            local_amount = calculated_local or row_local_amount or local_amount
        if record_type == "withdraw" and len(cells) >= 11:
            # 提现列表存在横向滚动列，部分表头不在 DOM 可见区域时会和单元格错位。
            # 当前后台顺序: 本地金额、USDT 金额、汇率分别位于收款地址/状态后的第 9-11 列。
            local_amount = local_amount or format_decimal(clean_number_text(cells[8]))
            usdt_amount = format_decimal(clean_number_text(cells[9])) or usdt_amount
            exchange_rate = exchange_rate or clean_number_text(cells[10])
            if not local_amount:
                local_amount = calculate_local_amount(usdt_amount, exchange_rate)

        if record_no or exchange_rate or local_amount or fields:
            return {
                "record_no": record_no,
                "usdt_amount": usdt_amount,
                "exchange_rate": exchange_rate,
                "local_amount": local_amount,
                "fields": fields,
                "row_cells": cells,
            }
        time.sleep(0.4)
    return {
        "record_no": None,
        "usdt_amount": command_amount,
        "exchange_rate": None,
        "local_amount": None,
        "fields": {},
        "row_cells": [],
    }


def extract_first_row_record_no(driver) -> str | None:
    deadline = time.time() + 5
    patterns = [re.compile(r"\b\d{16,}\b"), re.compile(r"\b[RWDT]\d{8,}\b")]
    while time.time() < deadline:
        rows = driver.find_elements(By.CSS_SELECTOR, ".el-table__body tbody tr")
        for row in rows:
            try:
                if not row.is_displayed():
                    continue
                cells = [
                    cell.text.strip()
                    for cell in row.find_elements(By.CSS_SELECTOR, "td .cell")
                    if cell.text.strip()
                ]
                for cell in cells:
                    for pattern in patterns:
                        match = pattern.search(cell)
                        if match:
                            return match.group(0)
                text = " ".join((row.text or "").split())
                for pattern in patterns:
                    match = pattern.search(text)
                    if match:
                        return match.group(0)
            except StaleElementReferenceException:
                break
            except Exception:
                pass
        time.sleep(0.4)
    return None


def open_first_audit_dialog(driver) -> None:
    deadline = time.time() + 12
    while time.time() < deadline:
        for btn in driver.find_elements(By.CSS_SELECTOR, ".el-table button, .el-table__fixed-right button"):
            btn_text = btn.text.strip()
            if any(text in btn_text for text in ["审核", "Audit"]) and btn.is_displayed() and btn.is_enabled():
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                driver.execute_script("arguments[0].click()", btn)
                time.sleep(0.8)
                if visible_dialogs(driver):
                    WebDriverWait(driver, 8).until(
                        lambda d: d.find_elements(By.CSS_SELECTOR, ".el-dialog button, .el-dialog .el-form-item")
                    )
                    return
        time.sleep(0.5)
    raise RuntimeError("未找到可审核的新记录")


def get_form_item_by_labels(driver, labels: list[str]):
    for label in labels:
        item = get_form_item_by_label(driver, label)
        if item is not None:
            return item
    return None


def visible_dialog_form_labels(driver) -> list[str]:
    labels: list[str] = []
    for dialog in visible_dialogs(driver):
        for item in dialog.find_elements(By.CSS_SELECTOR, ".el-form-item"):
            try:
                label = item.find_element(By.CSS_SELECTOR, ".el-form-item__label").text.strip()
                if label:
                    labels.append(label)
            except Exception:
                pass
    return labels


def click_audit_radio(driver, decision: str) -> bool:
    target_texts = (
        ["通过", "同意", "Approve", "Approved", "Pass"]
        if decision == "approve"
        else ["审核不通过", "不通过", "拒绝", "No Pass", "NoPass", "Reject", "Rejected", "Fail", "Failed"]
    )
    for dialog in visible_dialogs(driver):
        for radio in dialog.find_elements(By.CSS_SELECTOR, ".el-radio"):
            radio_text = radio.text.strip()
            if any(text.lower() in radio_text.lower() for text in target_texts):
                driver.execute_script("arguments[0].click()", radio)
                time.sleep(0.2)
                return True
    return False


def select_audit_dropdown(driver, item, decision: str) -> bool:
    target_texts = (
        ["通过", "Approved", "Approve", "Pass"]
        if decision == "approve"
        else ["审核不通过", "不通过", "拒绝", "No Pass", "NoPass", "Rejected", "Reject", "Fail", "Failed"]
    )
    for text in target_texts:
        if select_el_dropdown(driver, item, option_text=text):
            return True
    return False


def click_audit_action_button(driver, decision: str) -> bool:
    target_texts = (
        ["通过", "同意", "Approve", "Approved", "Pass"]
        if decision == "approve"
        else ["审核不通过", "不通过", "拒绝", "No Pass", "NoPass", "Reject", "Rejected", "Fail", "Failed"]
    )
    for dialog in visible_dialogs(driver):
        for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
            btn_text = btn.text.strip()
            if any(text.lower() in btn_text.lower() for text in target_texts) and btn.is_displayed() and btn.is_enabled():
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                driver.execute_script("arguments[0].click()", btn)
                time.sleep(0.5)
                return True
    return False


def select_audit_status(driver, decision: str) -> bool:
    item = get_form_item_by_labels(driver, ["审核状态", "状态", "Status", "Audit Status", "Review Status"])
    if item is not None:
        if item.find_elements(By.CSS_SELECTOR, ".el-radio") and click_audit_radio(driver, decision):
            return True
        if item.find_elements(By.CSS_SELECTOR, ".el-select") and select_audit_dropdown(driver, item, decision):
            return True
    if click_audit_radio(driver, decision):
        return True
    for dialog in visible_dialogs(driver):
        for candidate in dialog.find_elements(By.CSS_SELECTOR, ".el-form-item"):
            if candidate.find_elements(By.CSS_SELECTOR, ".el-select") and select_audit_dropdown(driver, candidate, decision):
                return True
    return False


def fill_audit_dialog(driver, decision: str, reason: str) -> bool:
    status_selected = select_audit_status(driver, decision)

    code_item = get_form_item_by_labels(
        driver,
        ["谷歌验证码", "Google Authenticator", "Google Code", "Google Verification Code", "Google"],
    )
    if code_item is None:
        labels = ", ".join(visible_dialog_form_labels(driver)) or "未读取到字段标签"
        raise RuntimeError(f"审核弹窗缺少谷歌验证码字段，字段: {labels}")
    set_input_value(driver, code_item.find_element(By.CSS_SELECTOR, "input"), get_totp_code())

    reason_item = get_form_item_by_labels(
        driver,
        ["审核原因", "备注", "Reason", "Failure Reason", "Remark", "Audit Information", "Audit Remark"],
    )
    if reason_item:
        fill_text_field_in_item(driver, reason_item, reason)
    return status_selected


def audit_latest_record(driver, decision: str, reason: str) -> str:
    refresh_current_page(driver)
    open_first_audit_dialog(driver)
    status_selected = fill_audit_dialog(driver, decision, reason)
    if status_selected:
        return submit_visible_dialog(driver, ["确定", "提交", "保存", "Sure", "Confirm", "Submit", "Save"], "提交审核")
    return submit_audit_action(driver, decision)


def execute_backend_operation(
    user_id: int,
    record_type: str,
    country: str,
    merchant_name: str,
    amount: str,
    decision: str,
    language: str,
    headless: bool = DEFAULT_HEADLESS,
) -> dict[str, Any]:
    browser_mode = "无头浏览器" if headless else "可视化浏览器"
    if record_type == "recharge" and resolve_voucher_source() is None:
        raise RuntimeError(f"固定凭证不存在: {CREDENTIAL_IMAGE_PATH}")
    driver = ensure_shared_driver(headless=headless)
    try:
        switch_to_chinese(driver)
        switch_to_country(driver, country)

        route = RECHARGE_URL if record_type == "recharge" else WITHDRAW_URL
        navigate_to_page(driver, route)
        wait_page_ready(
            driver,
            ["充值记录", "充值"] if record_type == "recharge" else ["提现记录", "申请下发", "提现", "下发"],
        )

        if record_type == "recharge":
            if not wait_and_click_button(driver, "button", "充值", exact=True):
                buttons = ", ".join(visible_button_texts(driver)) or "未读取到可见按钮"
                debug_path = save_debug_snapshot(driver, "充值入口")
                raise RuntimeError(f"未找到充值按钮；可见按钮: {buttons}；调试截图: {Path(debug_path).name}")
            expected_labels = ["用户编码", "充值金额", "收款地址", "谷歌验证码"]
        else:
            click_withdraw_apply_button(driver)
            expected_labels = ["商户名称", "提现金额", "收款地址", "谷歌验证码"]

        record_id = f"{record_type[:1].upper()}{uuid4().hex[:8]}"
        remark = DEFAULT_REMARK
        found_labels, missing_labels = verify_form_fields(driver, expected_labels)
        if missing_labels:
            raise RuntimeError(f"表单字段缺失: {', '.join(missing_labels)}")
        if record_type == "recharge":
            user_code = merchant_name
            resolved_merchant = prepare_recharge_form(driver, user_code, amount, remark)
            submit_result = submit_visible_dialog(driver, ["保存", "Save"], "提交充值")
        else:
            resolved_merchant = prepare_withdraw_form(driver, merchant_name, amount, remark)
            submit_result = submit_visible_dialog(
                driver,
                ["Sure", "Confirm", "确定", "提交", "保存", "Submit", "Save"],
                "提交提现",
            )

        refresh_current_page(driver)
        local_currency = SUPPORTED_COUNTRIES[country]["currency"]
        list_details = extract_first_row_details(driver, record_type, local_currency, amount)
        backend_record_id = list_details.get("record_no") or extract_first_row_record_no(driver)
        if backend_record_id:
            record_id = backend_record_id

        audit_reason = DEFAULT_REMARK
        audit_result = audit_latest_record(driver, decision, audit_reason)
        screenshot_path = save_test_screenshot(driver, record_id)
        voucher = resolve_voucher_source()

        operation = build_operation(
            user_id=user_id,
            record_type=record_type,
            country=country,
            language=language,
            merchant_name=resolved_merchant,
            amount=amount,
            decision=decision,
            address=resolve_operation_address(record_type, country),
            remark=remark,
        )
        operation["id"] = record_id
        operation["backend_record_id"] = backend_record_id
        operation["usdt_amount"] = list_details.get("usdt_amount") or amount
        operation["exchange_rate"] = list_details.get("exchange_rate")
        operation["local_amount"] = list_details.get("local_amount")
        operation["list_fields"] = list_details.get("fields") or {}
        operation["list_row_cells"] = list_details.get("row_cells") or []
        if record_type == "recharge":
            operation["user_code"] = user_code
        operation["updated_at"] = now_iso()
        operation["status"] = "审核通过" if decision == "approve" else "审核拒绝"
        operation["browser_mode"] = browser_mode
        operation["submit_result"] = submit_result
        operation["audit_result"] = audit_result
        operation["audit_reason"] = (
            f"{browser_mode}真实提交完成；已进入{'充值' if record_type == 'recharge' else '提现'}表单；"
            f"已填写{'用户编码' if record_type == 'recharge' else '商户'}/金额/验证码/备注；提交结果: {submit_result}；"
            f"审核结果: {audit_result}；审核原因: {audit_reason}；"
            f"字段找到: {','.join(found_labels) or '-'}；"
            f"字段缺失: {','.join(missing_labels) or '-'}；"
            f"列表汇率: {operation.get('exchange_rate') or '-'}；"
            f"列表{local_currency}: {operation.get('local_amount') or '-'}；"
            f"固定凭证: {'存在' if voucher else '缺失'}；"
            f"最终审核: {'通过' if decision == 'approve' else '拒绝'}"
        )
        operation["voucher_path"] = str(voucher) if voucher else None
        operation["screenshot_path"] = screenshot_path
        store.add_operation(operation)
        return operation
    except Exception:
        close_shared_driver()
        raise


def fill_recharge_form(driver, user_code: str, amount: str, remark: str) -> str:
    resolved_merchant = user_code
    user_code_item = get_form_item_by_label(driver, "用户编码")
    if user_code_item:
        selected = select_el_dropdown(driver, user_code_item, option_text=user_code)
        if not selected:
            selected = select_el_dropdown(driver, user_code_item, index=0)
        if not selected:
            raise RuntimeError(f"未找到充值用户编码: {user_code}")
        time.sleep(0.8)

    merchant_item = get_form_item_by_label(driver, "ID-商户名称")
    if merchant_item:
        selected = False
        deadline = time.time() + 5
        while time.time() < deadline and not selected:
            selected = select_el_dropdown(driver, merchant_item, index=0)
            if not selected:
                time.sleep(0.6)
        if not selected:
            logger.info("充值商户字段无可选项，继续使用用户编码已选结果: %s", user_code)
        else:
            time.sleep(0.6)

    amount_item = get_form_item_by_label(driver, "充值金额")
    if amount_item:
        inputs = amount_item.find_elements(By.CSS_SELECTOR, "input")
        for inp in inputs:
            if inp.is_displayed():
                set_input_value(driver, inp, amount)
                break

    address_item = get_form_item_by_label(driver, "收款地址")
    if address_item:
        select_el_dropdown(driver, address_item, index=0)

    code_item = get_form_item_by_label(driver, "谷歌验证码")
    if code_item:
        inp = code_item.find_element(By.CSS_SELECTOR, "input")
        set_input_value(driver, inp, get_totp_code())

    voucher_item = get_form_item_by_label(driver, "商户凭证")
    if voucher_item:
        file_input = None
        for inp in voucher_item.find_elements(By.CSS_SELECTOR, "input[type='file']"):
            file_input = inp
            break
        if file_input is None:
            for btn in voucher_item.find_elements(By.CSS_SELECTOR, "button"):
                if btn.is_displayed():
                    btn.click()
                    time.sleep(0.4)
                    for inp in driver.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                        file_input = inp
                        break
                    break
        if file_input:
            voucher = resolve_voucher_source()
            if voucher is None:
                raise RuntimeError(f"固定凭证不存在: {CREDENTIAL_IMAGE_PATH}")
            file_input.send_keys(str(voucher))
            time.sleep(0.4)

    remark_item = get_form_item_by_label(driver, "备注")
    if remark_item:
        fill_text_field_in_item(driver, remark_item, remark)

    return resolved_merchant


def prepare_recharge_form(driver, user_code: str, amount: str, remark: str) -> str:
    return fill_recharge_form(driver, user_code, amount, remark)


def fill_withdraw_form(driver, merchant_name: str, amount: str, remark: str) -> str:
    merchant_item = get_form_item_by_label(driver, "ID-商户名称")
    if merchant_item is None:
        merchant_item = get_form_item_by_label(driver, "商户名称")
    resolved_merchant = merchant_name.strip()
    if not resolved_merchant:
        raise RuntimeError("提现商户不能为空")
    if merchant_item:
        logger.info("按指令选择提现商户: %s", resolved_merchant)
        selected = select_el_dropdown(driver, merchant_item, option_text=resolved_merchant)
        if not selected:
            raise RuntimeError(f"未找到提现商户: {resolved_merchant}")
        time.sleep(0.6)
    else:
        labels = ", ".join(visible_dialog_form_labels(driver)) or "未读取到字段标签"
        raise RuntimeError(f"提现弹窗缺少商户字段，字段: {labels}")

    amount_item = get_form_item_by_label(driver, "提现金额")
    if amount_item:
        inputs = amount_item.find_elements(By.CSS_SELECTOR, "input")
        for inp in inputs:
            if inp.is_displayed():
                set_input_value(driver, inp, amount)
                break

    address_item = get_form_item_by_label(driver, "收款地址")
    if address_item:
        inp = address_item.find_element(By.CSS_SELECTOR, "input")
        set_input_value(driver, inp, f"{resolved_merchant}-{uuid4().hex[:10]}")

    code_item = get_form_item_by_label(driver, "谷歌验证码")
    if code_item:
        inp = code_item.find_element(By.CSS_SELECTOR, "input")
        set_input_value(driver, inp, get_totp_code())

    remark_item = get_form_item_by_label(driver, "备注")
    if remark_item:
        fill_text_field_in_item(driver, remark_item, remark)

    return resolved_merchant


def prepare_withdraw_form(driver, merchant_name: str, amount: str, remark: str) -> str:
    resolved_merchant = fill_withdraw_form(driver, merchant_name, amount, remark)
    return resolved_merchant


def help_text() -> str:
    return (
        "ladan3 财务后台机器人\n\n"
        "当前环境: real\n"
        "TG 指令默认启动浏览器真实新增链路，并返回提交及审核结果。\n"
        "默认模式: 无头浏览器；需要观察页面时发送 /set_headless off。\n\n"
        "指令:\n"
        "/start - 启动说明\n"
        "/help - 查看帮助\n"
        "/status - 查看当前上下文\n"
        "/set_country 印尼|泰国 - 切换后台国家/币种\n"
        "/set_language 中文|English - 切换机器人语言\n"
        "/set_headless on|off - 切换无头/可视化浏览器\n"
        "/recharge 国家 用户编码 金额 通过|拒绝 - 新增充值并审核\n"
        "/withdraw 国家 商户 金额 通过|拒绝 - 新增提现并审核\n"
        "/queue - 查看当前事务序列\n"
        "/list [recharge|withdraw] - 查看最近操作\n"
        "/detail 记录ID - 查看操作详情\n\n"
        "示例:\n"
        "/recharge 印尼 299 100 通过\n"
        "/withdraw 泰国 merchantB 200 拒绝\n"
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


def normalize_headless_mode(raw: str) -> bool:
    value = raw.strip().lower()
    aliases = {
        "on": True,
        "true": True,
        "1": True,
        "yes": True,
        "y": True,
        "开启": True,
        "打开": True,
        "无头": True,
        "off": False,
        "false": False,
        "0": False,
        "no": False,
        "n": False,
        "关闭": False,
        "可视化": False,
        "显示": False,
    }
    if value not in aliases:
        raise ValueError("用法: /set_headless on|off")
    return aliases[value]


def normalize_command_token(raw: str) -> str:
    command = raw.strip().lower()
    if "@" in command:
        command = command.split("@", 1)[0]
    return command


def friendly_error(exc: Exception) -> str:
    raw = str(exc).splitlines()[0].strip()
    if isinstance(exc, StaleElementReferenceException) or "stale element reference" in str(exc):
        return "页面刚刷新导致按钮引用失效，已关闭浏览器会话，请重试一次。"
    if not raw:
        return exc.__class__.__name__
    return raw[:500]


def execute_local_command(user_id: int, raw_text: str) -> str:
    text = raw_text.strip()
    if not text:
        return "请输入命令，输入 /help 查看说明。"
    if not text.startswith("/"):
        return "命令需以 / 开头，输入 /help 查看说明。"

    parts = text.split()
    command = normalize_command_token(parts[0])
    args = parts[1:]
    user_ctx = store.get_user_context(user_id)

    if command in {"/start", "/help"}:
        return help_text()
    if command == "/status":
        voucher = resolve_voucher_source()
        return (
            "当前后台机器人上下文:\n"
            f"环境: real\n"
            f"国家: {user_ctx.country}\n"
            f"币种: {SUPPORTED_COUNTRIES[user_ctx.country]['currency']}\n"
            f"语言: {user_ctx.language}\n"
            f"浏览器: {'无头浏览器' if user_ctx.headless else '可视化浏览器'}\n"
            f"最后记录ID: {user_ctx.last_record_id or '-'}\n"
            f"固定凭证源: {voucher or '未找到'}\n"
            f"后台地址: {LOGIN_URL}"
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
    if command == "/set_headless":
        if not args:
            return "用法: /set_headless on|off"
        try:
            headless = normalize_headless_mode(args[0])
        except ValueError as exc:
            return str(exc)
        store.update_user_context(user_id, headless=headless)
        close_shared_driver()
        return f"浏览器模式已切换为 {'无头浏览器' if headless else '可视化浏览器'}，下一条任务会使用新模式。"
    if command == "/queue":
        return queue_status_text()
    if command in {"/recharge", "/withdraw"}:
        if len(args) != 4:
            target_label = "用户编码" if command == "/recharge" else "商户"
            return f"用法: {command} 国家 {target_label} 金额 通过|拒绝"
        country = normalize_country(args[0])
        if country not in SUPPORTED_COUNTRIES:
            return "国家仅支持: 印尼、泰国"
        merchant_name = args[1]
        try:
            amount = parse_amount(args[2])
        except ValueError as exc:
            return str(exc)
        try:
            decision = normalize_audit_decision(args[3])
        except ValueError as exc:
            return str(exc)
        try:
            record = execute_backend_operation(
                user_id=user_id,
                record_type=command.removeprefix("/"),
                country=country,
                merchant_name=merchant_name,
                amount=amount,
                decision=decision,
                language=user_ctx.language,
                headless=user_ctx.headless,
            )
        except Exception as exc:
            return f"执行失败: {friendly_error(exc)}"
        store.update_user_context(user_id, country=country, last_record_id=record["id"])
        return (
            format_operation_summary(record)
            + format_record(record)
        )
    if command == "/list":
        record_type = None
        if args:
            candidate = args[0].strip().lower()
            if candidate not in {"recharge", "withdraw"}:
                return "可选参数仅支持: recharge 或 withdraw"
            record_type = candidate
        records = store.list_operations(country=user_ctx.country, record_type=record_type, limit=10)
        if not records:
            return "当前筛选条件下暂无记录。"
        lines = [
            format_list_line(item)
            for item in records
        ]
        return f"最近操作 ({user_ctx.country}):\n" + "\n".join(lines)
    if command == "/detail":
        if not args:
            return "用法: /detail 记录ID"
        record = store.get_operation(args[0])
        if record is None:
            return f"未找到记录: {args[0]}"
        return format_record(record)
    if command in {"/exit", "/quit"}:
        raise EOFError
    return "未知命令，输入 /help 查看说明。"


def run_local_repl() -> None:
    ensure_data_dirs()
    print("ladan3 离线交互模式已启动。")
    print("指令会启动浏览器真实新增及审核链路；默认无头，输入 /set_headless off 可切到可视化。")
    print("输入 /exit 退出。")
    print()
    local_user_id = 1
    while True:
        try:
            raw = input("ladan3> ")
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
        f"当前环境: real\n"
        "TG 指令会启动浏览器真实新增及审核链路，并返回最终结果。\n\n"
        f"当前国家: {user_ctx.country}\n"
        f"当前语言: {user_ctx.language}\n"
        f"浏览器: {'无头浏览器' if user_ctx.headless else '可视化浏览器'}\n"
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
        "当前后台机器人上下文:\n"
        f"环境: real\n"
        f"用户: {user_label(update)}\n"
        f"国家: {user_ctx.country}\n"
        f"币种: {SUPPORTED_COUNTRIES[user_ctx.country]['currency']}\n"
        f"语言: {user_ctx.language}\n"
        f"浏览器: {'无头浏览器' if user_ctx.headless else '可视化浏览器'}\n"
        f"最后记录ID: {user_ctx.last_record_id or '-'}\n"
        f"固定凭证源: {voucher or '未找到'}\n"
        f"后台地址: {LOGIN_URL}"
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


async def cmd_set_headless(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    if not ctx.args:
        await update.message.reply_text("用法: /set_headless on|off")
        return
    try:
        headless = normalize_headless_mode(ctx.args[0])
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    store.update_user_context(update.effective_user.id, headless=headless)
    close_shared_driver()
    await update.message.reply_text(
        f"浏览器模式已切换为 {'无头浏览器' if headless else '可视化浏览器'}，下一条任务会使用新模式。"
    )


async def create_finance_record(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    record_type: str,
) -> None:
    global _active_transaction_id
    if update.message is None or update.effective_user is None:
        return
    if len(ctx.args) != 4:
        target_label = "用户编码" if record_type == "recharge" else "商户"
        await update.message.reply_text(f"用法: /{record_type} 国家 {target_label} 金额 通过|拒绝")
        return

    country = normalize_country(ctx.args[0])
    if country not in SUPPORTED_COUNTRIES:
        await update.message.reply_text("国家仅支持: 印尼、泰国")
        return
    merchant_name = ctx.args[1]
    try:
        amount = parse_amount(ctx.args[2])
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    try:
        decision = normalize_audit_decision(ctx.args[3])
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    user_ctx = store.get_user_context(update.effective_user.id)
    type_label = "充值" if record_type == "recharge" else "提现"
    decision_label = "通过" if decision == "approve" else "拒绝"
    transaction_id = next_transaction_id()
    lock = get_operation_lock()
    queue_position = len(_queued_transaction_ids) + (1 if lock.locked() else 0)
    _queued_transaction_ids.append(transaction_id)
    progress_message = await update.message.reply_text(
        f"{type_label}任务已入队\n"
        f"事务: {transaction_id}\n"
        f"排队: 前方 {queue_position} 单\n"
        f"国家: {country}\n"
        f"{'用户编码' if record_type == 'recharge' else '商户'}: {merchant_name}\n"
        f"金额: {amount} USDT\n"
        f"审核: {decision_label}\n"
        f"浏览器: {'无头' if user_ctx.headless else '可视化'}"
    )
    try:
        async with lock:
            _active_transaction_id = transaction_id
            if transaction_id in _queued_transaction_ids:
                _queued_transaction_ids.remove(transaction_id)
            await progress_message.edit_text(
                f"{type_label}任务执行中\n"
                f"事务: {transaction_id}\n"
                f"国家: {country}\n"
                f"{'用户编码' if record_type == 'recharge' else '商户'}: {merchant_name}\n"
                f"金额: {amount} USDT\n"
                f"审核: {decision_label}\n"
                f"浏览器: {'无头' if user_ctx.headless else '可视化'}"
            )
            record = await asyncio.to_thread(
                execute_backend_operation,
                user_id=update.effective_user.id,
                record_type=record_type,
                country=country,
                merchant_name=merchant_name,
                amount=amount,
                decision=decision,
                language=user_ctx.language,
                headless=user_ctx.headless,
            )
    except Exception as exc:
        if transaction_id in _queued_transaction_ids:
            _queued_transaction_ids.remove(transaction_id)
        if _active_transaction_id == transaction_id:
            _active_transaction_id = None
        await progress_message.edit_text(
            f"{type_label}执行失败\n"
            f"事务: {transaction_id}\n"
            f"原因: {friendly_error(exc)}"
        )
        return
    finally:
        if _active_transaction_id == transaction_id:
            _active_transaction_id = None
    store.update_user_context(update.effective_user.id, country=country, last_record_id=record["id"])
    record["transaction_id"] = transaction_id
    store.save()
    await progress_message.edit_text(
        f"事务: {transaction_id}\n"
        + format_operation_summary(record)
        + format_record(record)
    )


async def cmd_recharge(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await create_finance_record(update, ctx, record_type="recharge")


async def cmd_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await create_finance_record(update, ctx, record_type="withdraw")


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(queue_status_text())


async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text("当前已改为真实后台模式，请直接使用 /recharge 或 /withdraw。")


async def cmd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text("当前已改为真实后台模式，请直接使用 /recharge 或 /withdraw。")


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
    records = store.list_operations(country=user_ctx.country, record_type=record_type, limit=10)
    if not records:
        await update.message.reply_text("当前筛选条件下暂无记录。")
        return
    lines = [
        format_list_line(item)
        for item in records
    ]
    await update.message.reply_text(f"最近操作 ({user_ctx.country}):\n" + "\n".join(lines))


async def cmd_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not ctx.args:
        await update.message.reply_text("用法: /detail 记录ID")
        return
    record = store.get_operation(ctx.args[0])
    if record is None:
        await update.message.reply_text(f"未找到记录: {ctx.args[0]}")
        return
    await update.message.reply_text(format_record(record))


async def handle_text_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    text = update.message.text or ""
    if not text.startswith("/"):
        await update.message.reply_text("请输入 /help 查看可用指令。")
        return
    try:
        result = execute_local_command(update.effective_user.id, text)
    except EOFError:
        result = "Telegram 模式下不支持 /exit，请在服务器终端停止进程。"
    except Exception as exc:
        logger.exception("Telegram 文本指令处理失败")
        result = f"执行失败: {exc}"
    await update.message.reply_text(result)


async def handle_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, Conflict):
        logger.error("Telegram 轮询冲突：同一个 bot token 已被另一个进程或服务器使用。")
        return
    logger.exception("Telegram handler failed", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message is not None:
        await update.effective_message.reply_text(f"执行失败: {ctx.error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ladan3 real backend finance bot")
    parser.add_argument(
        "--local-demo",
        action="store_true",
        help="不连接 Telegram，输出本地说明和固定凭证信息",
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
        print("ladan3 本地演示模式已启动。")
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

    if not TG_BOT_TOKEN:
        print("机器人启动失败：缺少环境变量 TG_BOT_TOKEN")
        raise SystemExit(1)

    app = Application.builder().token(TG_BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("set_country", cmd_set_country))
    app.add_handler(CommandHandler("set_language", cmd_set_language))
    app.add_handler(CommandHandler("set_headless", cmd_set_headless))
    app.add_handler(CommandHandler("recharge", cmd_recharge))
    app.add_handler(CommandHandler("withdraw", cmd_withdraw))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("detail", cmd_detail))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_command))
    app.add_handler(MessageHandler(filters.COMMAND, handle_text_command))
    app.add_error_handler(handle_error)

    logger.info("ladan3 real backend bot started")
    try:
        app.run_polling(drop_pending_updates=True)
        request_shutdown("轮询结束")
    except NetworkError as exc:
        print(
            "机器人启动失败：当前环境无法连接 Telegram 服务器。\n"
            "已自动切换到离线交互模式。\n"
            "如果你只想看说明，也可以运行：python3 ladan3.py --local-demo\n"
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
