#!/usr/bin/env python3
# coding: utf-8

import argparse
import json
import logging
import os
import time

import pyotp
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

LOGIN_URL = "https://operator1.otgpaytest.com/#/login"
DEBUG_LOG_PATH = "/Users/apple/Documents/ceshi/.cursor/debug-b84c5e.log"
RUNTIME_LOG_PATH = "/Users/apple/Documents/ceshi/.cursor/zdhcs-runtime.log"
DEBUG_SESSION_ID = "b84c5e"
RUN_ID = f"run_{int(time.time() * 1000)}"
CLICK_STEP = 0
TEXT_STEP = 0
LOGGER = logging.getLogger("zdhcs")

# 优先读取环境变量，未设置时使用当前测试账号。
DEFAULT_USERNAME = "ceshi"
DEFAULT_PASSWORD = "qw123456"
DEFAULT_TOTP_SEED = "6gnkpcdu22be5pahlbvfvffmnxcqzjzv"
# 代收查询
CURRENCY_SELECTOR = (
    "#OtgPay > div > div.main-container.hasTagsView > div > div.navbar > div.right-menu > "
    "div.el-select.right-menu-item.currency-select.el-select--medium > div > input"
)
INDONESIAN_RUPIAH_OPTION_XPATH = (
    "//div[contains(@class,'el-select-dropdown') and not(contains(@style,'display: none'))]"
    "//span[normalize-space()='印尼盾']"
)
MENU_SELECTOR = (
    "#OtgPay > div > div.sidebar-container.has-logo > div.el-scrollbar > "
    "div.scrollbar-wrapper.el-scrollbar__wrap > div > ul > div:nth-child(4) > li > div"
)
SUBMENU_SELECTOR = (
    "#OtgPay > div > div.sidebar-container.has-logo > div.el-scrollbar > "
    "div.scrollbar-wrapper.el-scrollbar__wrap > div > ul > div:nth-child(4) > li > ul > "
    "div:nth-child(5)"
)
SUBMENU_FOURTH_SELECTOR = (
    "#OtgPay > div > div.sidebar-container.has-logo > div.el-scrollbar > "
    "div.scrollbar-wrapper.el-scrollbar__wrap > div > ul > div:nth-child(4) > li > ul > "
    "div:nth-child(4) > a > li > span"
)
DATE_RANGE_SELECTOR = (
    "#OtgPay > div > div.main-container.hasTagsView > section > div > div:nth-child(1) > "
    "div > form > div:nth-child(4) > div:nth-child(2) > div > div.el-form-item__content > div"
)
DATE_SHORTCUT_SELECTOR = (
    "body > div.el-picker-panel.el-date-range-picker.el-popper.has-sidebar.has-time > "
    "div.el-picker-panel__body-wrapper > div.el-picker-panel__sidebar > button:nth-child(2)"
)
DATE_CONFIRM_SELECTOR = (
    "body > div.el-picker-panel.el-date-range-picker.el-popper.has-sidebar.has-time > "
    "div.el-picker-panel__footer > button.el-button.el-picker-panel__link-btn."
    "el-button--default.el-button--mini.is-plain > span"
)
SEARCH_BUTTON_SELECTOR = (
    "#OtgPay > div > div.main-container.hasTagsView > section > div > div:nth-child(1) > "
    "div > form > div:nth-child(6) > button.el-button.el-button--primary.el-button--medium > span"
)
CARD_SEARCH_BUTTON_SELECTOR = (
    "#OtgPay > div > div.main-container.hasTagsView > section > div > "
    "div.el-card.is-always-shadow > div > form > div:nth-child(6) > "
    "button.el-button.el-button--primary.el-button--medium > span"
)
CARD_DATE_RANGE_SELECTOR = (
    "#OtgPay > div > div.main-container.hasTagsView > section > div > "
    "div.el-card.is-always-shadow > div > form > div:nth-child(4) > div:nth-child(2) > "
    "div > div.el-form-item__content > div > span"
)
CARD_DATE_SHORTCUT_SELECTOR = (
    "body > div:nth-child(8) > div.el-picker-panel__body-wrapper > "
    "div.el-picker-panel__sidebar > button:nth-child(2)"
)
COPY_SOURCE_SELECTOR = (
    "#OtgPay > div > div.main-container.hasTagsView > section > div > div:nth-child(3) > "
    "div.el-table.el-table--fit.el-table--border.el-table--scrollable-x.el-table--medium > "
    "div.el-table__body-wrapper.is-scrolling-left > table > tbody > tr:nth-child(1) > "
    "td.el-table_2_column_48 > div"
)
CARD_INPUT_TARGET_SELECTOR = (
    "#OtgPay > div > div.main-container.hasTagsView > section > div > "
    "div.el-card.is-always-shadow > div > form > div:nth-child(1) > div:nth-child(3) > "
    "div > div.el-form-item__content > div > input"
)


def configure_logging() -> None:
    if LOGGER.handlers:
        return

    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)

    try:
        os.makedirs(os.path.dirname(RUNTIME_LOG_PATH), exist_ok=True)
        file_handler = logging.FileHandler(RUNTIME_LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)
    except Exception:
        pass


def serialize_log_data(data: dict | None = None) -> str:
    if not data:
        return ""
    try:
        return " | " + json.dumps(data, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        return f" | {data}"


def emit_log(level: str, message: str, data: dict | None = None) -> None:
    log_method = getattr(LOGGER, level, LOGGER.info)
    log_method(message + serialize_log_data(data))


def mask_text(value: str, *, prefix: int = 2, suffix: int = 2) -> str:
    if not value:
        return ""
    if len(value) <= prefix + suffix:
        return "*" * len(value)
    return f"{value[:prefix]}{'*' * (len(value) - prefix - suffix)}{value[-suffix:]}"


def preview_text(value: str, *, limit: int = 8) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def browser_state(driver: webdriver.Chrome | None) -> dict:
    if driver is None:
        return {}

    state: dict[str, str | int] = {}
    try:
        state["windowHandleCount"] = len(driver.window_handles)
    except Exception as exc:
        state["windowHandleCountError"] = type(exc).__name__
    try:
        state["currentUrl"] = driver.current_url
    except Exception as exc:
        state["currentUrlError"] = type(exc).__name__
    try:
        state["title"] = driver.title
    except Exception as exc:
        state["titleError"] = type(exc).__name__
    return state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="登录 OTG 后台并执行指定页面点击动作")
    parser.add_argument("--headless", action="store_true", help="无头模式运行")
    return parser.parse_args()


def build_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_experimental_option("detach", True)
    return webdriver.Chrome(options=options)


def load_login() -> tuple[str, str, str]:
    username = os.getenv("OTG_USERNAME", DEFAULT_USERNAME)
    password = os.getenv("OTG_PASSWORD", DEFAULT_PASSWORD)
    totp_seed = os.getenv("OTG_TOTP_SEED", DEFAULT_TOTP_SEED)
    emit_log(
        "info",
        "已加载登录配置",
        {
            "username": mask_text(username),
            "passwordFromEnv": "OTG_PASSWORD" in os.environ,
            "totpSeedFromEnv": "OTG_TOTP_SEED" in os.environ,
        },
    )
    return username, password, totp_seed


def debug_log(location: str, message: str, data: dict, hypothesis_id: str) -> None:
    payload = {
        "sessionId": DEBUG_SESSION_ID,
        "runId": RUN_ID,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        os.makedirs(os.path.dirname(DEBUG_LOG_PATH), exist_ok=True)
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_click_result(
    step_no: int,
    phase: str,
    locator_type: str,
    locator_value: str,
    *,
    sleep_seconds: float = 0.0,
    error_type: str | None = None,
) -> None:
    timestamp = time.strftime("%H:%M:%S")
    locator_name = "CSS" if locator_type == "css" else "XPath"
    if phase == "start":
        summary = f"[{timestamp}] 第{step_no}步开始点击，定位方式：{locator_name}，定位值：{locator_value}"
    elif phase == "success":
        summary = f"[{timestamp}] 第{step_no}步执行成功，定位方式：{locator_name}"
    else:
        summary = f"[{timestamp}] 第{step_no}步执行失败（{error_type}），定位方式：{locator_name}，定位值：{locator_value}"

    if sleep_seconds > 0:
        summary += f"，等待 {sleep_seconds} 秒"

    emit_log(
        "error" if phase == "fail" else "info",
        summary,
        {
            "stepNo": step_no,
            "phase": phase,
            "locatorType": locator_type,
            "locatorValue": locator_value,
            "sleepSeconds": sleep_seconds,
            "errorType": error_type,
        },
    )
    debug_log(
        "zdhcs.py:log_click_result",
        summary,
        {
            "stepNo": step_no,
            "phase": phase,
            "locatorType": locator_type,
            "locatorValue": locator_value,
            "sleepSeconds": sleep_seconds,
            "errorType": error_type,
        },
        "CLICK",
    )


def click_css(wait: WebDriverWait, selector: str, *, sleep_seconds: float = 0.0) -> None:
    global CLICK_STEP
    CLICK_STEP += 1
    step_no = CLICK_STEP
    log_click_result(step_no, "start", "css", selector, sleep_seconds=sleep_seconds)
    try:
        element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
        element.click()
        log_click_result(step_no, "success", "css", selector, sleep_seconds=sleep_seconds)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    except Exception as exc:
        log_click_result(
            step_no,
            "fail",
            "css",
            selector,
            sleep_seconds=sleep_seconds,
            error_type=type(exc).__name__,
        )
        raise


def click_xpath(wait: WebDriverWait, xpath: str, *, sleep_seconds: float = 0.0) -> None:
    global CLICK_STEP
    CLICK_STEP += 1
    step_no = CLICK_STEP
    log_click_result(step_no, "start", "xpath", xpath, sleep_seconds=sleep_seconds)
    try:
        element = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
        element.click()
        log_click_result(step_no, "success", "xpath", xpath, sleep_seconds=sleep_seconds)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    except Exception as exc:
        log_click_result(
            step_no,
            "fail",
            "xpath",
            xpath,
            sleep_seconds=sleep_seconds,
            error_type=type(exc).__name__,
        )
        raise


def log_text_result(
    step_no: int,
    phase: str,
    action_name: str,
    selector: str,
    *,
    value: str = "",
    error_type: str | None = None,
) -> None:
    timestamp = time.strftime("%H:%M:%S")
    if phase == "start":
        summary = f"[{timestamp}] 文本步骤{step_no}开始{action_name}，定位值：{selector}"
    elif phase == "success":
        summary = f"[{timestamp}] 文本步骤{step_no}{action_name}成功，内容：{value}"
    else:
        summary = f"[{timestamp}] 文本步骤{step_no}{action_name}失败（{error_type}），定位值：{selector}"
    emit_log(
        "error" if phase == "fail" else "info",
        summary,
        {
            "stepNo": step_no,
            "phase": phase,
            "actionName": action_name,
            "selector": selector,
            "value": value,
            "errorType": error_type,
        },
    )
    debug_log(
        "zdhcs.py:log_text_result",
        summary,
        {
            "stepNo": step_no,
            "phase": phase,
            "actionName": action_name,
            "selector": selector,
            "value": value,
            "errorType": error_type,
        },
        "TEXT",
    )


def copy_text_from_css(wait: WebDriverWait, selector: str) -> str:
    global CLICK_STEP, TEXT_STEP
    CLICK_STEP += 1
    click_step_no = CLICK_STEP
    log_click_result(click_step_no, "start", "css", selector)
    try:
        element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
        copied_text = element.text.strip()
        element.click()
        log_click_result(click_step_no, "success", "css", selector)
    except Exception as exc:
        log_click_result(click_step_no, "fail", "css", selector, error_type=type(exc).__name__)
        raise

    TEXT_STEP += 1
    text_step_no = TEXT_STEP
    log_text_result(text_step_no, "start", "复制内容", selector)
    log_text_result(text_step_no, "success", "复制内容", selector, value=copied_text)
    return copied_text


def input_text_css(wait: WebDriverWait, selector: str, value: str) -> None:
    global TEXT_STEP
    TEXT_STEP += 1
    text_step_no = TEXT_STEP
    log_text_result(text_step_no, "start", "输入内容", selector)
    try:
        element = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, selector)))
        element.clear()
        element.send_keys(value)
        log_text_result(text_step_no, "success", "输入内容", selector, value=value)
    except Exception as exc:
        log_text_result(
            text_step_no,
            "fail",
            "输入内容",
            selector,
            value=value,
            error_type=type(exc).__name__,
        )
        raise


def login_and_run(headless: bool) -> None:
    try:
        emit_log("info", "开始执行自动化流程", {"headless": headless, "runId": RUN_ID})
        username, password, totp_seed = load_login()
        driver = build_driver(headless=headless)
        wait = WebDriverWait(driver, 40)
        emit_log("info", "浏览器驱动已创建", browser_state(driver))

        emit_log("info", "正在打开登录页", {"url": LOGIN_URL})
        driver.get(LOGIN_URL)
        emit_log("info", "登录页已打开", browser_state(driver))

        emit_log("info", "尝试最大化浏览器窗口")
        driver.maximize_window()
        emit_log("info", "浏览器窗口已最大化", browser_state(driver))

        emit_log("info", "开始填写登录表单")
        wait.until(EC.visibility_of_element_located((By.NAME, "username"))).send_keys(username)
        emit_log("info", "用户名已输入")
        wait.until(EC.visibility_of_element_located((By.NAME, "password"))).send_keys(password)
        emit_log("info", "密码已输入")
        mfa_code = pyotp.TOTP(totp_seed).now()
        wait.until(EC.visibility_of_element_located((By.NAME, "mfaCode"))).send_keys(mfa_code)
        emit_log("info", "MFA 动态码已输入", {"codeLength": len(mfa_code)})

        click_xpath(wait, "//*[text()='Login']")
        emit_log("info", "登录按钮已点击，等待后台首页加载")
        wait.until(EC.presence_of_element_located((By.ID, "OtgPay")))

        # region agent log
        debug_log(
            "zdhcs.py:login_and_run:card_flow_start",
            "entered post-login flow",
            {"current_url": driver.current_url, "title": driver.title},
            "H5",
        )
        # endregion
        emit_log("info", "登录成功，准备执行页面动作", browser_state(driver))

        click_css(wait, CURRENCY_SELECTOR)
        click_xpath(wait, INDONESIAN_RUPIAH_OPTION_XPATH, sleep_seconds=2.0)
        click_css(wait, MENU_SELECTOR, sleep_seconds=0.5)
        click_css(wait, SUBMENU_SELECTOR, sleep_seconds=0.5)
        click_css(wait, SEARCH_BUTTON_SELECTOR, sleep_seconds=2.0)
        click_css(wait, DATE_RANGE_SELECTOR)
        click_css(wait, DATE_SHORTCUT_SELECTOR)
        click_css(wait, DATE_CONFIRM_SELECTOR)
        click_css(wait, SEARCH_BUTTON_SELECTOR, sleep_seconds=2.0)

        emit_log("info", "基础查询流程执行完成，准备进入卡片流程", browser_state(driver))

        # region agent log
        debug_log(
            "zdhcs.py:login_and_run:card_flow_start",
            "reached appended card flow",
            {"current_url": driver.current_url},
            "H1",
        )
        # endregion
        click_css(wait, SUBMENU_FOURTH_SELECTOR)
        card_buttons = driver.find_elements(By.CSS_SELECTOR, CARD_SEARCH_BUTTON_SELECTOR)
        card_state = {"count": len(card_buttons), "displayed": None, "enabled": None}
        if card_buttons:
            card_state["displayed"] = card_buttons[0].is_displayed()
            card_state["enabled"] = card_buttons[0].is_enabled()
        emit_log("info", "卡片页搜索按钮状态已采集", card_state)
        # region agent log
        debug_log(
            "zdhcs.py:login_and_run:before_card_search_click",
            "about to click el-card search button",
            card_state,
            "H2",
        )
        # endregion
        click_css(wait, CARD_SEARCH_BUTTON_SELECTOR, sleep_seconds=2.0)
        # region agent log
        debug_log(
            "zdhcs.py:login_and_run:after_card_search_click",
            "clicked el-card search button",
            {"current_url": driver.current_url},
            "H3",
        )
        # endregion
        click_css(wait, CARD_DATE_RANGE_SELECTOR)
        click_css(wait, CARD_DATE_SHORTCUT_SELECTOR)
        # region agent log
        debug_log(
            "zdhcs.py:login_and_run:after_card_date_shortcut",
            "selected card date shortcut",
            {"current_url": driver.current_url},
            "H4",
        )
        # endregion
        click_css(wait, SEARCH_BUTTON_SELECTOR, sleep_seconds=2.0)
        # region agent log
        debug_log(
            "zdhcs.py:login_and_run:after_original_search_post_card",
            "finished search after el-card flow",
            {"current_url": driver.current_url},
            "H4",
        )
        # endregion
        copied_text = copy_text_from_css(wait, COPY_SOURCE_SELECTOR)
        emit_log(
            "info",
            "已复制首条记录文本",
            {"length": len(copied_text), "preview": preview_text(copied_text)},
        )
        input_text_css(wait, CARD_INPUT_TARGET_SELECTOR, copied_text)
        click_css(wait, SEARCH_BUTTON_SELECTOR, sleep_seconds=2.0)
        emit_log("info", "指定点击动作执行完成", browser_state(driver))
    except Exception as exc:
        driver = locals().get("driver")
        emit_log(
            "error",
            "自动化流程执行失败",
            {
                "errorType": type(exc).__name__,
                "message": str(exc),
                **browser_state(driver),
            },
        )
        if driver is not None:
            # region agent log
            debug_log(
                "zdhcs.py:login_and_run:card_flow_exception",
                "exception during appended card flow",
                {"type": type(exc).__name__, "message": str(exc)},
                "H1",
            )
            # endregion
        raise


def main() -> None:
    configure_logging()
    args = parse_args()
    emit_log("info", "命令行参数解析完成", vars(args))
    try:
        login_and_run(headless=args.headless)
    except Exception:
        LOGGER.exception("脚本执行失败")
        raise


if __name__ == "__main__":
    main()
