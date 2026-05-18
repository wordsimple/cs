#!/usr/bin/env python3
# coding: utf-8

import argparse
import base64
import hashlib
import hmac
import logging
import os
import struct
import time
import zipfile
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import NoSuchWindowException, TimeoutException, WebDriverException
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


LOGIN_URL = "https://operator1.otgpaytest.com/#/login"
DEFAULT_USERNAME = "ceshi"
DEFAULT_PASSWORD = "qw123456"
DEFAULT_TOTP_SEED = "6gnkpcdu22be5pahlbvfvffmnxcqzjzv"
DEFAULT_TIMEOUT = 30
TARGET_CURRENCY_KEYWORDS = ("Thailand", "泰铢")
TARGET_MERCHANT_KEYWORD = "10000034"
TARGET_CHANNEL_PREFIX = "pr"
TARGET_LIST_KEYWORD = "teb"

BASE_DIR = Path(__file__).resolve().parent
ARTIFACT_DIR = BASE_DIR / ".cursor"
LOG_PATH = ARTIFACT_DIR / "zdhcsGX-runtime.log"
SCREENSHOT_PATH = ARTIFACT_DIR / f"zdhcsGX-failure-{int(time.time() * 1000)}.png"
REPORT_DIR = BASE_DIR / "test_reports"

LOGGER = logging.getLogger("zdhcsGX")


class TestReport:
    def __init__(self) -> None:
        self.started_at = datetime.now()
        self.ended_at: datetime | None = None
        self.rows: list[dict[str, str | int]] = []
        self.summary = {"success": 0, "skipped": 0, "failed": 0, "total": 0}
        self.report_path: Path | None = None

    def add_result(
        self,
        module: str,
        url: str,
        status: str,
        *,
        tab: str = "",
        text_count: int = 0,
        date_count: int = 0,
        select_count: int = 0,
        message: str = "",
    ) -> None:
        self.rows.append(
            {
                "module": module,
                "tab": tab,
                "url": url,
                "status": status,
                "textCount": text_count,
                "dateCount": date_count,
                "selectCount": select_count,
                "message": message,
            }
        )

    def finish(self, success: int, skipped: int, failed: int, total: int) -> None:
        self.ended_at = datetime.now()
        self.summary = {"success": success, "skipped": skipped, "failed": failed, "total": total}


CURRENT_REPORT: TestReport | None = None


def configure_logging() -> None:
    if LOGGER.handlers:
        return

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)


def docx_paragraph(text: str, *, style: str | None = None) -> str:
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    return (
        "<w:p>"
        f"<w:pPr>{style_xml}</w:pPr>"
        f"<w:r><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r>"
        "</w:p>"
    )


def docx_table(headers: list[str], rows: list[list[str]]) -> str:
    def cell(text: str, bold: bool = False) -> str:
        bold_xml = "<w:b/>" if bold else ""
        return (
            "<w:tc><w:tcPr><w:tcW w:w=\"2400\" w:type=\"dxa\"/></w:tcPr>"
            "<w:p><w:r>"
            f"<w:rPr>{bold_xml}</w:rPr>"
            f"<w:t xml:space=\"preserve\">{escape(text)}</w:t>"
            "</w:r></w:p></w:tc>"
        )

    table = [
        "<w:tbl>",
        "<w:tblPr><w:tblW w:w=\"0\" w:type=\"auto\"/><w:tblBorders>"
        "<w:top w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"auto\"/>"
        "<w:left w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"auto\"/>"
        "<w:bottom w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"auto\"/>"
        "<w:right w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"auto\"/>"
        "<w:insideH w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"auto\"/>"
        "<w:insideV w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"auto\"/>"
        "</w:tblBorders></w:tblPr>",
        "<w:tr>" + "".join(cell(header, bold=True) for header in headers) + "</w:tr>",
    ]
    for row in rows:
        table.append("<w:tr>" + "".join(cell(value) for value in row) + "</w:tr>")
    table.append("</w:tbl>")
    return "".join(table)


def write_docx_report(report: TestReport) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ended_at = report.ended_at or datetime.now()
    filename = f"{ended_at.strftime('%Y-%m-%d_%H-%M-%S')}.docx"
    output_path = REPORT_DIR / filename

    duration = int((ended_at - report.started_at).total_seconds())
    summary_rows = [
        ["开始时间", report.started_at.strftime("%Y-%m-%d %H:%M:%S")],
        ["结束时间", ended_at.strftime("%Y-%m-%d %H:%M:%S")],
        ["耗时秒数", str(duration)],
        ["总模块数", str(report.summary["total"])],
        ["成功", str(report.summary["success"])],
        ["跳过", str(report.summary["skipped"])],
        ["失败", str(report.summary["failed"])],
    ]
    detail_rows = [
        [
            str(index),
            str(row["module"]),
            str(row["tab"]),
            str(row["status"]),
            str(row["textCount"]),
            str(row["dateCount"]),
            str(row["selectCount"]),
            str(row["message"]),
        ]
        for index, row in enumerate(report.rows, 1)
    ]

    body = [
        docx_paragraph("OTG UI 自动化测试报告", style="Title"),
        docx_paragraph("测试概览", style="Heading1"),
        docx_table(["项目", "结果"], summary_rows),
        docx_paragraph("测试明细", style="Heading1"),
        docx_table(
            ["序号", "模块", "页签", "结果", "文本", "日期", "选择框", "备注"],
            detail_rows or [["", "", "", "无明细", "", "", "", ""]],
        ),
    ]

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(body)
        + '<w:sectPr><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/>'
        '<w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720" '
        'w:header="450" w:footer="450" w:gutter="0"/></w:sectPr>'
        "</w:body></w:document>"
    )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    document_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>'
        '<w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
        '<w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>'
        "</w:styles>"
    )

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", content_types_xml)
        docx.writestr("_rels/.rels", rels_xml)
        docx.writestr("word/_rels/document.xml.rels", document_rels_xml)
        docx.writestr("word/document.xml", document_xml)
        docx.writestr("word/styles.xml", styles_xml)

    report.report_path = output_path
    return output_path


def mask_text(value: str, prefix: int = 2, suffix: int = 2) -> str:
    if not value:
        return ""
    if len(value) <= prefix + suffix:
        return "*" * len(value)
    return f"{value[:prefix]}{'*' * (len(value) - prefix - suffix)}{value[-suffix:]}"


def generate_totp(secret: str, interval: int = 30, digits: int = 6) -> str:
    normalized = secret.strip().replace(" ", "").upper()
    padded = normalized + "=" * (-len(normalized) % 8)
    key = base64.b32decode(padded, casefold=True)
    counter = int(time.time() // interval)
    message = struct.pack(">Q", counter)
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OTG 后台登录 UI 自动化")
    parser.add_argument("--headless", action="store_true", help="使用无头模式运行 Chrome")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="元素等待超时时间，默认 30 秒")
    parser.add_argument("--keep-open", action="store_true", help="脚本结束后保留浏览器窗口")
    parser.add_argument("--module-limit", type=int, default=0, help="限制遍历模块数量，0 表示全部模块")
    parser.add_argument("--module-delay", type=float, default=1.2, help="进入模块后的等待秒数，默认 1.2 秒")
    return parser.parse_args()


def read_login_config() -> tuple[str, str, str]:
    username = os.getenv("OTG_USERNAME", DEFAULT_USERNAME)
    password = os.getenv("OTG_PASSWORD", DEFAULT_PASSWORD)
    totp_seed = os.getenv("OTG_TOTP_SEED", DEFAULT_TOTP_SEED)

    LOGGER.info(
        "登录配置已加载 username=%s passwordFromEnv=%s totpSeedFromEnv=%s",
        mask_text(username),
        "OTG_PASSWORD" in os.environ,
        "OTG_TOTP_SEED" in os.environ,
    )
    return username, password, totp_seed


def build_driver(headless: bool, keep_open: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-notifications")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if keep_open:
        options.add_experimental_option("detach", True)
    return webdriver.Chrome(options=options)


def browser_state(driver: webdriver.Chrome | None) -> dict[str, str | int]:
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


def wait_visible(wait: WebDriverWait, name: str, locator: tuple[str, str]):
    LOGGER.info("等待元素可见：%s", name)
    return wait.until(EC.visibility_of_element_located(locator))


def safe_input(wait: WebDriverWait, name: str, locator: tuple[str, str], value: str) -> None:
    element = wait_visible(wait, name, locator)
    element.clear()
    element.send_keys(value)
    LOGGER.info("%s 已输入", name)


def safe_click(wait: WebDriverWait, driver: webdriver.Chrome, name: str, locator: tuple[str, str]) -> None:
    LOGGER.info("准备点击：%s", name)
    element = wait.until(EC.element_to_be_clickable(locator))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
    try:
        element.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", element)
    LOGGER.info("%s 已点击", name)


def driver_alive(driver: webdriver.Chrome | None) -> bool:
    if driver is None:
        return False
    try:
        return bool(driver.window_handles)
    except Exception:
        return False


def dismiss_transient_overlays(driver: webdriver.Chrome | None) -> None:
    """关闭 Element UI 的下拉、日期面板、tooltip 等临时选择框。"""
    if not driver_alive(driver):
        return

    try:
        driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        time.sleep(0.1)
    except Exception:
        pass

    try:
        driver.execute_script(
            """
            if (document.activeElement && document.activeElement.blur) {
                document.activeElement.blur();
            }
            document.querySelectorAll(
                '.el-select-dropdown, .el-picker-panel, .el-autocomplete-suggestion, ' +
                '.el-popover, .el-tooltip__popper, .el-dropdown-menu'
            ).forEach(el => {
                el.style.display = 'none';
                el.setAttribute('aria-hidden', 'true');
            });
            """
        )
    except Exception:
        pass


def close_dialogs_and_selectors(driver: webdriver.Chrome | None) -> None:
    if not driver_alive(driver):
        return

    dismiss_transient_overlays(driver)
    close_button_xpath = (
        "//div[contains(@class, 'el-dialog') or contains(@class, 'el-drawer')]"
        "//button[contains(@class, 'el-dialog__headerbtn') "
        "or contains(@class, 'el-drawer__close-btn') "
        "or contains(., '取消') or contains(., '取 消') or contains(., 'Cancel')]"
    )
    try:
        for button in driver.find_elements(By.XPATH, close_button_xpath):
            if button.is_displayed():
                try:
                    driver.execute_script("arguments[0].click();", button)
                    time.sleep(0.15)
                except Exception:
                    pass
    except Exception:
        pass
    dismiss_transient_overlays(driver)


def switch_language_to_chinese(wait: WebDriverWait, driver: webdriver.Chrome) -> None:
    LOGGER.info("准备切换语言为中文")
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".sidebar-container a[href^='#/']")))
        language_buttons = driver.find_elements(
            By.CSS_SELECTOR,
            ".international .el-dropdown-selfdefine, .international.right-menu-item, .international-icon",
        )
        for language_button in language_buttons:
            if not language_button.is_displayed():
                continue
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", language_button)
            try:
                ActionChains(driver).move_to_element(language_button).pause(0.2).click(language_button).perform()
            except Exception:
                driver.execute_script(
                    """
                    const el = arguments[0];
                    ['mouseenter', 'mouseover', 'mousedown', 'mouseup', 'click'].forEach(type => {
                        el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    });
                    """,
                    language_button,
                )
            time.sleep(0.5)

            visible_items = visible_elements(
                driver.find_elements(
                    By.XPATH,
                    "//li[contains(@class, 'el-dropdown-menu__item') "
                    "and contains(normalize-space(.), '中文') "
                    "and not(contains(@class, 'is-disabled'))]",
                )
            )
            if visible_items:
                driver.execute_script("arguments[0].click();", visible_items[0])
                time.sleep(2.0)
                LOGGER.info("已切换为中文")
                return

        chinese_items = driver.find_elements(
            By.XPATH,
            "//li[contains(@class, 'el-dropdown-menu__item') "
            "and contains(normalize-space(.), '中文') "
            "and not(contains(@class, 'is-disabled'))]",
        )
        if chinese_items:
            driver.execute_script(
                """
                const item = arguments[0];
                let parent = item.parentElement;
                while (parent && parent !== document.body) {
                    if (parent.classList && parent.classList.contains('el-dropdown-menu')) {
                        parent.style.display = 'block';
                        parent.setAttribute('aria-hidden', 'false');
                        break;
                    }
                    parent = parent.parentElement;
                }
                item.click();
                """,
                chinese_items[0],
            )
            time.sleep(2.0)
            LOGGER.info("已切换为中文")
            return

        wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//li[contains(@class, 'el-dropdown-menu__item') and contains(normalize-space(.), '中文')]",
                )
            )
        ).click()
        time.sleep(2.0)
        LOGGER.info("已切换为中文")
    except Exception as exc:
        LOGGER.warning("切换中文失败或当前已是中文：%s", exc)
    finally:
        dismiss_transient_overlays(driver)


def switch_currency_to_thailand(wait: WebDriverWait, driver: webdriver.Chrome) -> None:
    LOGGER.info("准备切换币种/国家为泰国")
    try:
        currency_input = wait.until(
            EC.element_to_be_clickable(
                (
                    By.CSS_SELECTOR,
                    ".right-menu .currency-select input, "
                    ".navbar .currency-select input, "
                    ".currency-select .el-input__inner",
                )
            )
        )
        driver.execute_script("arguments[0].click();", currency_input)
        option_xpath = (
            "//li[contains(@class, 'el-select-dropdown__item') "
            "and not(contains(@class, 'is-disabled')) "
            "and (contains(normalize-space(.), 'Thailand') or contains(normalize-space(.), '泰铢'))]"
        )
        thailand_option = wait.until(EC.element_to_be_clickable((By.XPATH, option_xpath)))
        driver.execute_script("arguments[0].click();", thailand_option)
        time.sleep(2.0)
        LOGGER.info("已切换为泰国")
    except Exception as exc:
        LOGGER.warning("切换泰国失败或当前已是泰国：%s", exc)
    finally:
        dismiss_transient_overlays(driver)


def get_all_modules(driver: webdriver.Chrome) -> list[dict[str, str]]:
    modules = driver.execute_script(
        """
        const seen = new Set();
        return Array.from(document.querySelectorAll('.sidebar-container a[href^="#/"]'))
            .map(a => ({
                url: a.href,
                name: (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ')
            }))
            .filter(item => item.url && item.name)
            .filter(item => {
                if (seen.has(item.url)) return false;
                seen.add(item.url);
                return true;
            });
        """
    )
    return modules or []


def visible_elements(elements) -> list:
    visible = []
    for element in elements:
        try:
            if element.is_displayed() and element.is_enabled():
                visible.append(element)
        except Exception:
            pass
    return visible


def field_description(driver: webdriver.Chrome, element) -> str:
    parts: list[str] = []
    try:
        form_item = driver.execute_script("return arguments[0].closest('.el-form-item');", element)
        if form_item:
            labels = form_item.find_elements(By.CSS_SELECTOR, ".el-form-item__label")
            parts.extend(label.text for label in labels if label.text)
    except Exception:
        pass

    for attr in ("placeholder", "name", "aria-label"):
        try:
            value = element.get_attribute(attr)
            if value:
                parts.append(value)
        except Exception:
            pass
    return " ".join(parts)


def resolve_text_value(description: str) -> str:
    lowered = description.lower()
    if any(key in lowered for key in ("url", "notify", "callback")) or any(
        key in description for key in ("回调", "链接", "地址")
    ):
        return "https://example.com/callback"
    if "ip" in lowered:
        return "127.0.0.1"
    if any(key in description for key in ("手机号", "电话", "手机")):
        return "13800138000"
    if any(key in description for key in ("金额", "数量", "限额", "排序")):
        return "1"
    return ""


def should_skip_input(description: str) -> bool:
    lowered = description.lower()
    skip_keywords = (
        "商户订单号",
        "平台订单号",
        "订单号",
        "密码",
        "分组名称",
        "group name",
        "password",
    )
    return any(keyword in lowered or keyword in description for keyword in skip_keywords)


def fill_text_inputs(driver: webdriver.Chrome, container) -> int:
    count = 0
    inputs = container.find_elements(By.XPATH, ".//input[not(@readonly) and not(@disabled)]")
    text_inputs = []
    for element in inputs:
        input_type = (element.get_attribute("type") or "text").lower()
        if input_type in ("text", "search", "number", "tel", "email"):
            text_inputs.append(element)

    for element in visible_elements(text_inputs):
        description = field_description(driver, element)
        if should_skip_input(description):
            continue
        try:
            element.clear()
            element.send_keys(resolve_text_value(description))
            count += 1
        except Exception:
            pass

    for textarea in visible_elements(container.find_elements(By.XPATH, ".//textarea[not(@readonly) and not(@disabled)]")):
        if should_skip_input(field_description(driver, textarea)):
            continue
        try:
            textarea.clear()
            textarea.send_keys(resolve_text_value(field_description(driver, textarea)))
            count += 1
        except Exception:
            pass
    return count


def option_text(element) -> str:
    try:
        return (element.text or "").strip()
    except Exception:
        return ""


def pick_option(options: list, description: str):
    lowered_description = description.lower()
    option_pairs = [(option, option_text(option)) for option in options]

    if "商户" in description or "merchant" in lowered_description or "mer" in lowered_description:
        for option, text in option_pairs:
            if TARGET_MERCHANT_KEYWORD in text:
                return option

    if "渠道" in description or "channel" in lowered_description:
        for option, text in option_pairs:
            if text.lower().strip().startswith(TARGET_CHANNEL_PREFIX):
                return option
        for option, text in option_pairs:
            if text.lower().strip().startswith(TARGET_LIST_KEYWORD):
                return option

    for option, text in option_pairs:
        if TARGET_LIST_KEYWORD in text.lower():
            return option

    for option, _ in option_pairs:
        classes = option.get_attribute("class") or ""
        if "selected" not in classes:
            return option

    return options[0] if options else None


def choose_select_options(driver: webdriver.Chrome, container) -> int:
    count = 0
    selects = container.find_elements(By.XPATH, ".//div[contains(@class, 'el-select')]//input[@readonly]")
    for select_input in visible_elements(selects):
        try:
            description = field_description(driver, select_input)
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", select_input)
            driver.execute_script("arguments[0].click();", select_input)
            time.sleep(0.35)

            options = visible_elements(
                driver.find_elements(
                    By.XPATH,
                    "//li[contains(@class, 'el-select-dropdown__item') "
                    "and not(contains(@class, 'is-disabled'))]",
                )
            )
            if not options:
                dismiss_transient_overlays(driver)
                continue

            target = pick_option(options, description)
            if target is None:
                dismiss_transient_overlays(driver)
                continue

            driver.execute_script("arguments[0].click();", target)
            count += 1
            time.sleep(0.2)
        except Exception as exc:
            LOGGER.debug("选择下拉内容失败：%s", exc)
        finally:
            dismiss_transient_overlays(driver)
    return count


def set_native_value(driver: webdriver.Chrome, element, value: str) -> None:
    driver.execute_script(
        """
        const input = arguments[0];
        const value = arguments[1];
        const descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
        input.removeAttribute('readonly');
        input.removeAttribute('disabled');
        if (descriptor && descriptor.set) {
            descriptor.set.call(input, value);
        } else {
            input.value = value;
        }
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
        """,
        element,
        value,
    )


def choose_date_ranges(driver: webdriver.Chrome, container) -> int:
    count = 0
    now = datetime.now()
    start_date = (now - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    end_date = now.strftime("%Y-%m-%d 23:59:59")
    editors = visible_elements(
        container.find_elements(
            By.XPATH,
            ".//div[contains(@class, 'el-date-editor') and "
            "(contains(@class, 'el-date-editor--daterange') "
            "or contains(@class, 'el-date-editor--datetimerange') "
            "or contains(@class, 'el-date-editor--date'))]",
        )
    )

    for editor in editors:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", editor)
            inputs = visible_elements(editor.find_elements(By.XPATH, ".//input"))
            if len(inputs) >= 2:
                set_native_value(driver, inputs[0], start_date)
                set_native_value(driver, inputs[1], end_date)
                count += 1
            elif len(inputs) == 1:
                set_native_value(driver, inputs[0], now.strftime("%Y-%m-%d"))
                count += 1
            else:
                continue
            try:
                inputs[-1].send_keys(Keys.ENTER)
            except Exception:
                pass
            time.sleep(0.2)
        except Exception as exc:
            LOGGER.debug("选择日期内容失败：%s", exc)
        finally:
            dismiss_transient_overlays(driver)
    return count


def find_search_button(driver: webdriver.Chrome):
    search_xpaths = [
        "//form//button[contains(@class, 'el-button') and "
        "(contains(normalize-space(.), '查询') "
        "or contains(normalize-space(.), '搜索') "
        "or contains(normalize-space(.), 'Search'))]",
        "//button[contains(@class, 'el-button--primary') and "
        "(contains(normalize-space(.), '查询') "
        "or contains(normalize-space(.), '搜索') "
        "or contains(normalize-space(.), 'Search'))]",
        "//form//button[contains(@class, 'el-button--primary')]",
    ]
    for xpath in search_xpaths:
        buttons = visible_elements(driver.find_elements(By.XPATH, xpath))
        if buttons:
            return buttons[0]
    return None


def get_inner_tabs(driver: webdriver.Chrome) -> list[dict[str, str]]:
    tabs = driver.execute_script(
        """
        const appMain = document.querySelector('.app-main, .main-container section, section');
        if (!appMain) return [];
        const routeView = Array.from(appMain.querySelectorAll(
            '.app-container, .dashboard-container, .el-card, form'
        )).find(el => {
            const box = el.getBoundingClientRect();
            return box.width > 0 && box.height > 0;
        }) || appMain;

        const containers = Array.from(routeView.querySelectorAll('.el-tabs')).filter(container => {
            const box = container.getBoundingClientRect();
            const hasVisiblePanel = Array.from(container.querySelectorAll('.el-tab-pane, [role="tabpanel"]'))
                .some(panel => {
                    const panelBox = panel.getBoundingClientRect();
                    return panelBox.width > 0 && panelBox.height > 0;
                });
            return box.width > 0 && box.height > 0 && hasVisiblePanel;
        });

        const tabs = [];
        const seen = new Set();
        containers.forEach(container => {
            Array.from(container.querySelectorAll(
                '.el-tabs__header .el-tabs__item, [role="tab"]'
            )).forEach(tab => {
                const text = (tab.innerText || tab.textContent || '').trim().replace(/\\s+/g, ' ');
                const id = tab.id || tab.getAttribute('aria-controls') || text;
                const box = tab.getBoundingClientRect();
                if (!text || box.width <= 0 || box.height <= 0 || seen.has(id)) return;
                seen.add(id);
                tabs.push({id, text});
            });
        });
        return tabs;
        """
    )
    return tabs or []


def click_inner_tab(driver: webdriver.Chrome, tab_text: str) -> None:
    script = """
        const targetText = arguments[0];
        const appMain = document.querySelector('.app-main, .main-container section, section');
        if (!appMain) return false;
        const routeView = Array.from(appMain.querySelectorAll(
            '.app-container, .dashboard-container, .el-card, form'
        )).find(el => {
            const box = el.getBoundingClientRect();
            return box.width > 0 && box.height > 0;
        }) || appMain;
        const containers = Array.from(routeView.querySelectorAll('.el-tabs'));
        for (const container of containers) {
            const box = container.getBoundingClientRect();
            if (box.width <= 0 || box.height <= 0) continue;
            const tabs = Array.from(container.querySelectorAll(
                '.el-tabs__header .el-tabs__item, [role="tab"]'
            ));
            const tab = tabs.find(item => {
                const text = (item.innerText || item.textContent || '').trim().replace(/\\s+/g, ' ');
                return text === targetText;
            });
            if (tab) {
                tab.scrollIntoView({block: 'center', inline: 'center'});
                tab.click();
                return true;
            }
        }
        return false;
    """
    clicked = driver.execute_script(script, tab_text)
    if not clicked:
        raise RuntimeError(f"未找到页面内页签：{tab_text}")
    time.sleep(0.8)
    dismiss_transient_overlays(driver)


def test_module_search(driver: webdriver.Chrome, module_name: str) -> tuple[bool, dict[str, int | str]]:
    search_button = find_search_button(driver)
    if search_button is None:
        LOGGER.info("模块 [%s] 未发现查询按钮，跳过", module_name)
        return False, {"textCount": 0, "dateCount": 0, "selectCount": 0, "message": "未发现查询按钮"}

    text_count = 0
    date_count = 0
    select_count = 0
    form = None
    try:
        form = driver.execute_script("return arguments[0].closest('form');", search_button)
    except Exception:
        pass

    if form is not None:
        text_count = fill_text_inputs(driver, form)
        date_count = choose_date_ranges(driver, form)
        select_count = choose_select_options(driver, form)
        LOGGER.info(
            "模块 [%s] 已自动填写文本 %s 个、日期 %s 个、选择框 %s 个",
            module_name,
            text_count,
            date_count,
            select_count,
        )

    dismiss_transient_overlays(driver)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", search_button)
    try:
        search_button.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", search_button)
    time.sleep(0.8)
    dismiss_transient_overlays(driver)
    LOGGER.info("模块 [%s] 查询完成", module_name)
    return True, {
        "textCount": text_count,
        "dateCount": date_count,
        "selectCount": select_count,
        "message": "查询完成",
    }


def test_module_with_inner_tabs(driver: webdriver.Chrome, module_name: str, module_url: str) -> bool:
    global CURRENT_REPORT
    tabs = get_inner_tabs(driver)
    if not tabs:
        success, detail = test_module_search(driver, module_name)
        if CURRENT_REPORT is not None:
            CURRENT_REPORT.add_result(
                module_name,
                module_url,
                "成功" if success else "跳过",
                text_count=int(detail["textCount"]),
                date_count=int(detail["dateCount"]),
                select_count=int(detail["selectCount"]),
                message=str(detail["message"]),
            )
        return success

    LOGGER.info("模块 [%s] 发现 %s 个页面内页签，准备逐个测试", module_name, len(tabs))
    success_count = 0
    skipped_count = 0
    tab_names = [tab.get("text", f"页签{index}") for index, tab in enumerate(tabs, 1)]

    for index, tab_text in enumerate(tab_names, 1):
        try:
            LOGGER.info("模块 [%s] 切换页签 (%s/%s)：%s", module_name, index, len(tabs), tab_text)
            click_inner_tab(driver, tab_text)
            success, detail = test_module_search(driver, f"{module_name}/{tab_text}")
            if CURRENT_REPORT is not None:
                CURRENT_REPORT.add_result(
                    module_name,
                    module_url,
                    "成功" if success else "跳过",
                    tab=tab_text,
                    text_count=int(detail["textCount"]),
                    date_count=int(detail["dateCount"]),
                    select_count=int(detail["selectCount"]),
                    message=str(detail["message"]),
                )
            if success:
                success_count += 1
            else:
                skipped_count += 1
        except Exception as exc:
            skipped_count += 1
            if CURRENT_REPORT is not None:
                CURRENT_REPORT.add_result(
                    module_name,
                    module_url,
                    "失败",
                    tab=tab_text,
                    message=str(exc),
                )
            LOGGER.warning("模块 [%s] 页签 [%s] 测试失败，继续下一个页签：%s", module_name, tab_text, exc)
        finally:
            close_dialogs_and_selectors(driver)

    LOGGER.info(
        "模块 [%s] 页签测试完成：成功=%s 跳过=%s 总页签=%s",
        module_name,
        success_count,
        skipped_count,
        len(tabs),
    )
    return success_count > 0


def run_all_module_queries(
    wait: WebDriverWait,
    driver: webdriver.Chrome,
    *,
    module_limit: int,
    module_delay: float,
) -> None:
    global CURRENT_REPORT
    switch_language_to_chinese(wait, driver)
    switch_currency_to_thailand(wait, driver)
    modules = get_all_modules(driver)
    if module_limit > 0:
        modules = modules[:module_limit]

    LOGGER.info("共发现 %s 个模块，准备逐个查询", len(modules))
    success_count = 0
    skipped_count = 0
    failed_count = 0

    for index, module in enumerate(modules, 1):
        if not driver_alive(driver):
            LOGGER.error("浏览器窗口不可用，中止模块遍历")
            break

        name = module.get("name", f"模块{index}")
        url = module.get("url")
        LOGGER.info("开始模块 (%s/%s)：%s -> %s", index, len(modules), name, url)
        try:
            driver.get(url)
            wait.until(EC.presence_of_element_located((By.ID, "OtgPay")))
            time.sleep(module_delay)
            if test_module_with_inner_tabs(driver, name, url):
                success_count += 1
            else:
                skipped_count += 1
        except Exception as exc:
            failed_count += 1
            if CURRENT_REPORT is not None:
                CURRENT_REPORT.add_result(name, url, "失败", message=str(exc))
            LOGGER.exception("模块 [%s] 查询失败：%s", name, exc)
        finally:
            close_dialogs_and_selectors(driver)

    LOGGER.info(
        "模块查询结束：成功=%s 跳过=%s 失败=%s 总数=%s",
        success_count,
        skipped_count,
        failed_count,
        len(modules),
    )
    if CURRENT_REPORT is not None:
        CURRENT_REPORT.finish(success_count, skipped_count, failed_count, len(modules))
        report_path = write_docx_report(CURRENT_REPORT)
        LOGGER.info("Word 测试报告已生成：%s", report_path)


def save_failure_screenshot(driver: webdriver.Chrome | None) -> None:
    if driver is None:
        return
    try:
        if not driver.window_handles:
            LOGGER.warning("浏览器窗口已关闭，无法截图")
            return
        driver.save_screenshot(str(SCREENSHOT_PATH))
        LOGGER.info("失败截图已保存：%s", SCREENSHOT_PATH)
    except Exception as exc:
        LOGGER.warning("保存失败截图失败：%s", exc)


def assert_login_success(wait: WebDriverWait, driver: webdriver.Chrome) -> None:
    LOGGER.info("等待登录完成")
    wait.until(lambda d: bool(d.find_elements(By.CSS_SELECTOR, ".sidebar-container a[href^='#/']")))
    wait.until(EC.presence_of_element_located((By.ID, "OtgPay")))
    LOGGER.info("登录成功 %s", browser_state(driver))


def login_otg(headless: bool, timeout: int, keep_open: bool, module_limit: int, module_delay: float) -> None:
    global CURRENT_REPORT
    driver: webdriver.Chrome | None = None
    try:
        CURRENT_REPORT = TestReport()
        username, password, totp_seed = read_login_config()
        driver = build_driver(headless=headless, keep_open=keep_open)
        wait = WebDriverWait(driver, timeout)

        LOGGER.info("浏览器已启动 %s", browser_state(driver))
        LOGGER.info("打开登录页：%s", LOGIN_URL)
        driver.get(LOGIN_URL)
        LOGGER.info("登录页已打开 %s", browser_state(driver))

        if not headless:
            driver.maximize_window()

        safe_input(wait, "用户名", (By.NAME, "username"), username)
        safe_input(wait, "密码", (By.NAME, "password"), password)
        safe_input(wait, "MFA 动态码", (By.NAME, "mfaCode"), generate_totp(totp_seed))
        safe_click(wait, driver, "Login 按钮", (By.XPATH, "//button[.//*[normalize-space()='Login'] or normalize-space()='Login']"))

        assert_login_success(wait, driver)
        run_all_module_queries(wait, driver, module_limit=module_limit, module_delay=module_delay)
    except (TimeoutException, NoSuchWindowException, WebDriverException, Exception) as exc:
        LOGGER.exception("登录自动化执行失败：%s", exc)
        LOGGER.error("失败时浏览器状态：%s", browser_state(driver))
        save_failure_screenshot(driver)
        raise
    finally:
        if driver is not None and not keep_open:
            try:
                driver.quit()
                LOGGER.info("浏览器已关闭")
            except Exception:
                pass


def main() -> None:
    configure_logging()
    args = parse_args()
    LOGGER.info(
        "启动参数 headless=%s timeout=%s keepOpen=%s moduleLimit=%s moduleDelay=%s",
        args.headless,
        args.timeout,
        args.keep_open,
        args.module_limit,
        args.module_delay,
    )
    login_otg(
        headless=args.headless,
        timeout=args.timeout,
        keep_open=args.keep_open,
        module_limit=args.module_limit,
        module_delay=args.module_delay,
    )


if __name__ == "__main__":
    main()
