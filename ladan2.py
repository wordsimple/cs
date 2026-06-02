#!/usr/bin/env python3
# coding: utf-8
"""
ladan2: Telegram finance bot with real backend automation.

该机器人保留原有 /recharge /withdraw /set_country /set_language /list /detail
等指令风格，但实际执行走 OTG 后台浏览器自动化，支持固定上传商户凭证，
并对新增记录执行审核通过/拒绝流程。
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

import pyotp
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from telegram import Update
from telegram.error import Conflict, NetworkError, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


BOT_NAME = "ladan2"
APP_ENV = os.getenv("LADAN_ENV", "real").strip().lower()
TG_BOT_TOKEN = os.getenv(
    "TG_BOT_TOKEN",
    "8864081579:AAGvx_5rWYDkGjB5LqJPMO5vWbCytEsidCc",
)
DEFAULT_LANGUAGE = "中文"
DEFAULT_COUNTRY = "印尼"
DEFAULT_HEADLESS = False
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
DATA_DIR = BASE_DIR / "ladan_data"
STATE_FILE = DATA_DIR / "state.json"
LOGIN_URL = "https://operator1.otgpaytest.com/#/login"
BASE_URL = "https://operator1.otgpaytest.com/#"
RECHARGE_URL = "/operate/recharge/list"
WITHDRAW_URL = "/operate/withdraw/list"
DEFAULT_USERNAME = "ceshi"
DEFAULT_PASSWORD = "qw123456"
DEFAULT_TOTP_SEED = "6gnkpcdu22be5pahlbvfvffmnxcqzjzv"
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


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


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


def resolve_operation_address(record_type: str, country: str) -> str:
    if record_type == "withdraw":
        return "123"
    return SUPPORTED_COUNTRIES[country]["recharge_address"]


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


def format_operation_summary(record: dict[str, Any]) -> str:
    browser_mode = record.get("browser_mode", "无头浏览器")
    return f"{'充值' if record['type'] == 'recharge' else '提现'}{browser_mode}测试结果:\n"


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


def login(driver=None, *, headless: bool = DEFAULT_HEADLESS):
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
        driver.find_element(By.CSS_SELECTOR, ".currency-select .el-input__inner").click()
        time.sleep(0.4)
        for item in driver.find_elements(By.CSS_SELECTOR, ".el-select-dropdown__item"):
            if target_label in item.text:
                item.click()
                time.sleep(1)
                logger.info("已切换地区: %s", country)
                return
        raise RuntimeError(f"未找到货币选项: {target_label}")
    except Exception as exc:
        logger.warning("切换国家失败: %s", exc)
        raise


def get_totp_code() -> str:
    return pyotp.TOTP(DEFAULT_TOTP_SEED).now()


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


def page_has_text(driver, text: str) -> bool:
    try:
        return text in (driver.find_element(By.TAG_NAME, "body").text or "")
    except Exception:
        return False


def navigate_to_page(driver, route: str) -> None:
    driver.get(f"{BASE_URL}{route}")
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if route.split("/")[-1] in driver.current_url:
                break
        except Exception:
            pass
        time.sleep(0.2)
    time.sleep(0.5)


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
    driver = ensure_driver(headless=headless)
    try:
        switch_to_chinese(driver)
        switch_to_country(driver, country)

        route = RECHARGE_URL if record_type == "recharge" else WITHDRAW_URL
        navigate_to_page(driver, route)

        if record_type == "recharge":
            if not wait_and_click_button(driver, "button", "充值", exact=True):
                raise RuntimeError("未找到充值按钮")
            expected_labels = ["用户编码", "充值金额", "收款地址", "谷歌验证码"]
        else:
            if not wait_and_click_button(driver, "button", "申请下发", exact=False):
                raise RuntimeError("未找到提现申请按钮")
            expected_labels = ["商户名称", "提现金额", "收款地址", "谷歌验证码"]

        record_id = f"{record_type[:1].upper()}{uuid4().hex[:8]}"
        remark = f"{browser_mode}测试{('充值' if record_type == 'recharge' else '提现')} {now_iso()}"
        found_labels, missing_labels = verify_form_fields(driver, expected_labels)
        if record_type == "recharge":
            resolved_merchant = prepare_recharge_form(driver, merchant_name, amount, remark)
        else:
            resolved_merchant = prepare_withdraw_form(driver, merchant_name, amount, remark)
        screenshot_path = save_test_screenshot(driver, record_id)
        voucher = resolve_voucher_source()
        test_passed = not missing_labels and voucher is not None

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
        operation["updated_at"] = now_iso()
        operation["status"] = "测试通过" if test_passed else "测试失败"
        operation["browser_mode"] = browser_mode
        operation["audit_reason"] = (
            f"{browser_mode}测试完成；已进入{'充值' if record_type == 'recharge' else '提现'}表单；"
            f"已填写商户/金额/验证码/备注；"
            f"字段找到: {','.join(found_labels) or '-'}；"
            f"字段缺失: {','.join(missing_labels) or '-'}；"
            f"固定凭证: {'存在' if voucher else '缺失'}；"
            f"预期审核结果: {'通过' if decision == 'approve' else '拒绝'}；"
            f"停止点: 保存/提交前，未创建真实单据"
        )
        operation["voucher_path"] = str(voucher) if voucher else None
        operation["screenshot_path"] = screenshot_path
        store.add_operation(operation)
        return operation
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def fill_recharge_form(driver, merchant_name: str, amount: str, remark: str) -> str:
    resolved_merchant = merchant_name
    user_code_item = get_form_item_by_label(driver, "用户编码")
    if user_code_item:
        selected = select_el_dropdown(driver, user_code_item, option_text=merchant_name)
        if not selected:
            selected = select_el_dropdown(driver, user_code_item, index=0)
        if not selected:
            raise RuntimeError(f"未找到充值用户编码/商户: {merchant_name}")
        time.sleep(0.8)

    merchant_item = get_form_item_by_label(driver, "ID-商户名称")
    if merchant_item:
        selected = select_el_dropdown(driver, merchant_item, option_text=merchant_name)
        if not selected:
            logger.info("充值商户字段未匹配 %s，继续使用用户编码已选结果", merchant_name)
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
            file_input.send_keys(str(resolve_voucher_source()))
            time.sleep(0.4)

    remark_item = get_form_item_by_label(driver, "备注")
    if remark_item:
        textarea = remark_item.find_element(By.CSS_SELECTOR, "textarea")
        set_input_value(driver, textarea, remark)

    return resolved_merchant


def prepare_recharge_form(driver, merchant_name: str, amount: str, remark: str) -> str:
    return fill_recharge_form(driver, merchant_name, amount, remark)


def fill_withdraw_form(driver, merchant_name: str, amount: str, remark: str) -> str:
    merchant_item = get_form_item_by_label(driver, "ID-商户名称")
    if merchant_item is None:
        merchant_item = get_form_item_by_label(driver, "商户名称")
    resolved_merchant = "1075"
    if merchant_item:
        logger.info("暂时固定使用提现商户: %s", resolved_merchant)
        selected = select_el_dropdown(driver, merchant_item, option_text=resolved_merchant)
        if not selected:
            raise RuntimeError(f"未找到提现商户: {resolved_merchant}")
        time.sleep(0.6)

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
        textarea = remark_item.find_elements(By.CSS_SELECTOR, "textarea")
        if textarea:
            set_input_value(driver, textarea[0], remark)

    return resolved_merchant


def prepare_withdraw_form(driver, merchant_name: str, amount: str, remark: str) -> str:
    resolved_merchant = fill_withdraw_form(driver, merchant_name, amount, remark)
    return resolved_merchant


def help_text() -> str:
    return (
        "ladan2 财务后台机器人\n\n"
        "当前环境: real\n"
        "TG 指令默认启动浏览器测试链路，并返回最终测试结果。\n"
        "默认模式: 可视化浏览器；需要后台静默运行时发送 /set_headless on。\n\n"
        "指令:\n"
        "/start - 启动说明\n"
        "/help - 查看帮助\n"
        "/status - 查看当前上下文\n"
        "/set_country 印尼|泰国 - 切换后台国家/币种\n"
        "/set_language 中文|English - 切换机器人语言\n"
        "/set_headless on|off - 切换无头/可视化浏览器\n"
        "/recharge 国家 商户 金额 通过|拒绝 - 浏览器测试充值链路\n"
        "/withdraw 国家 商户 金额 通过|拒绝 - 浏览器测试提现链路\n"
        "/list [recharge|withdraw] - 查看最近操作\n"
        "/detail 记录ID - 查看操作详情\n\n"
        "示例:\n"
        "/recharge 印尼 merchantA 100 通过\n"
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
        return f"浏览器模式已切换为 {'无头浏览器' if headless else '可视化浏览器'}。"
    if command in {"/recharge", "/withdraw"}:
        if len(args) != 4:
            return f"用法: {command} 国家 商户 金额 通过|拒绝"
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
            return f"执行失败: {exc}"
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
            f"{item['id']} | {item['type']} | {item['merchant_name']} | {item['amount']} {item['currency']} | {item['status']}"
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
    print("ladan2 离线交互模式已启动。")
    print("指令会启动浏览器测试链路；默认可视化，输入 /set_headless on 可切到无头。")
    print("输入 /exit 退出。")
    print()
    local_user_id = 1
    while True:
        try:
            raw = input("ladan2> ")
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
        "TG 指令会启动浏览器测试链路，并返回最终测试结果。\n\n"
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
    await update.message.reply_text(
        f"浏览器模式已切换为 {'无头浏览器' if headless else '可视化浏览器'}。"
    )


async def create_finance_record(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    record_type: str,
) -> None:
    if update.message is None or update.effective_user is None:
        return
    if len(ctx.args) != 4:
        await update.message.reply_text(f"用法: /{record_type} 国家 商户 金额 通过|拒绝")
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
    try:
        record = execute_backend_operation(
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
        await update.message.reply_text(f"执行失败: {exc}")
        return
    store.update_user_context(update.effective_user.id, country=country, last_record_id=record["id"])
    await update.message.reply_text(
        format_operation_summary(record)
        + format_record(record)
    )


async def cmd_recharge(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await create_finance_record(update, ctx, record_type="recharge")


async def cmd_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await create_finance_record(update, ctx, record_type="withdraw")


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
        f"{item['id']} | {item['type']} | {item['merchant_name']} | {item['amount']} {item['currency']} | {item['status']}"
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
    parser = argparse.ArgumentParser(description="ladan2 real backend finance bot")
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
        print("ladan2 本地演示模式已启动。")
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
    app.add_handler(CommandHandler("set_headless", cmd_set_headless))
    app.add_handler(CommandHandler("recharge", cmd_recharge))
    app.add_handler(CommandHandler("withdraw", cmd_withdraw))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("detail", cmd_detail))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_command))
    app.add_handler(MessageHandler(filters.COMMAND, handle_text_command))
    app.add_error_handler(handle_error)

    logger.info("ladan2 real backend bot started")
    try:
        app.run_polling(drop_pending_updates=True)
        request_shutdown("轮询结束")
    except NetworkError as exc:
        print(
            "机器人启动失败：当前环境无法连接 Telegram 服务器。\n"
            "已自动切换到离线交互模式。\n"
            "如果你只想看说明，也可以运行：python3 ladan2.py --local-demo\n"
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
