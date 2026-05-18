#!/usr/bin/env python3
# coding: utf-8
"""
OTG UI 自动化脚本：
1. 使用账号、密码和 TOTP 登录
2. 登录后按指定 XPath 依次点击菜单、子菜单、Search
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import struct
import time

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


LOGIN_URL = "https://operator1.otgpaytest.com/#/login"
DEFAULT_USERNAME = "ceshi"
DEFAULT_PASSWORD = "qw123456"
DEFAULT_TOTP_SEED = "6gnkpcdu22be5pahlbvfvffmnxcqzjzv"

# 绝对 XPath 容易随页面结构变化而失效，因此提供“多定位器兜底”（优先 CSS，其次旧 XPath）
MENU_LOCATORS: list[tuple[str, str]] = [
    (
        By.CSS_SELECTOR,
        "#OtgPay > div > div.sidebar-container.has-logo > div.el-scrollbar > "
        "div.scrollbar-wrapper.el-scrollbar__wrap > div > ul > div:nth-child(4) > li > div",
    ),
    (By.XPATH, '//*[@id="OtgPay"]/div/div[2]/div[2]/div[1]/div/ul/div[4]/li/div/span'),
]

SUBMENU_LOCATORS: list[tuple[str, str]] = [
    (
        By.CSS_SELECTOR,
        "#OtgPay > div > div.sidebar-container.has-logo > div.el-scrollbar > "
        "div.scrollbar-wrapper.el-scrollbar__wrap > div > ul > div:nth-child(4) > li > ul > "
        "div:nth-child(5)",
    ),
    (By.XPATH, '//*[@id="OtgPay"]/div/div[2]/div[2]/div[1]/div/ul/div[4]/li/ul/div[5]/a/li/span'),
]

SEARCH_LOCATORS: list[tuple[str, str]] = [
    (
        By.CSS_SELECTOR,
        "#OtgPay > div > div.main-container.hasTagsView > section > div > div:nth-child(1) > "
        "div > form > div:nth-child(6) > button.el-button.el-button--primary.el-button--medium > span",
    ),
    (By.XPATH, '//*[@id="OtgPay"]/div/div[2]/section/div/div[1]/div/form/div[6]/button[1]/span'),
]


def generate_totp(secret: str, *, interval: int = 30, digits: int = 6) -> str:
    normalized = secret.strip().replace(" ", "").upper()
    padded = normalized + "=" * (-len(normalized) % 8)
    key = base64.b32decode(padded, casefold=True)
    counter = int(time.time() // interval)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def load_credentials() -> tuple[str, str, str]:
    return (
        os.getenv("OTG_USERNAME", DEFAULT_USERNAME),
        os.getenv("OTG_PASSWORD", DEFAULT_PASSWORD),
        os.getenv("OTG_TOTP_SEED", DEFAULT_TOTP_SEED),
    )


def build_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,900")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=options)


def wait_visible(wait: WebDriverWait, locators: list[tuple[str, str]]):
    last_error: Exception | None = None
    for locator in locators:
        try:
            return wait.until(EC.visibility_of_element_located(locator))
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("未提供可用定位器")


def click_any(driver: webdriver.Chrome, locators: list[tuple[str, str]], *, timeout: int) -> None:
    wait = WebDriverWait(driver, timeout)
    last_error: Exception | None = None

    for by, value in locators:
        print(f"准备点击: ({by}, {value})", flush=True)
        try:
            element = wait.until(EC.presence_of_element_located((by, value)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
            time.sleep(0.2)
            try:
                element = wait.until(EC.element_to_be_clickable((by, value)))
                element.click()
            except Exception:
                driver.execute_script("arguments[0].click();", element)
            print(f"点击完成: ({by}, {value})", flush=True)
            return
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise RuntimeError("未提供可用定位器")


def perform_login(
    driver: webdriver.Chrome,
    *,
    username: str,
    password: str,
    totp_seed: str,
    timeout: int,
) -> None:
    wait = WebDriverWait(driver, timeout)
    driver.get(LOGIN_URL)
    driver.maximize_window()

    username_input = wait_visible(
        wait,
        [
            (By.NAME, "username"),
            (By.XPATH, "//input[@placeholder='Please enter username']"),
        ],
    )
    password_input = wait_visible(
        wait,
        [
            (By.NAME, "password"),
            (By.XPATH, "//input[@type='password']"),
        ],
    )
    mfa_input = wait_visible(
        wait,
        [
            (By.NAME, "mfaCode"),
            (By.XPATH, "//input[contains(@placeholder,'MFA')]"),
        ],
    )

    username_input.clear()
    username_input.send_keys(username)
    password_input.clear()
    password_input.send_keys(password)
    mfa_input.clear()
    mfa_input.send_keys(generate_totp(totp_seed))

    wait.until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                "//button[normalize-space()='Login']|//*[normalize-space()='Login' and (self::button or self::span)]",
            )
        )
    ).click()

    wait.until(lambda d: d.find_elements(By.ID, "OtgPay") or "#/login" not in d.current_url)
    print("登录成功，已进入系统", flush=True)


def run_post_login_flow(driver: webdriver.Chrome, *, timeout: int) -> None:
    # 登录完成后页面仍可能处于渲染/动画中，先等到主容器出现
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, "OtgPay")))

    click_any(driver, MENU_LOCATORS, timeout=timeout)
    click_any(driver, SUBMENU_LOCATORS, timeout=timeout)
    print("子菜单点击完成，停留 0.5 秒", flush=True)
    time.sleep(0.5)
    click_any(driver, SEARCH_LOCATORS, timeout=timeout)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OTG 登录并执行指定点击操作")
    parser.add_argument("--headless", action="store_true", help="以无头模式运行 Chrome")
    parser.add_argument("--timeout", type=int, default=40, help="单步等待超时秒数，默认 40")
    parser.add_argument("--close", action="store_true", help="执行完成后自动关闭浏览器")
    parser.add_argument("--pause", action="store_true", help="执行完成后等待回车")
    parser.add_argument("--screenshot", default="zdh_result.png", help="成功截图文件名")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    username, password, totp_seed = load_credentials()
    driver = build_driver(headless=args.headless)

    try:
        print("开始执行 OTG 登录与点击流程", flush=True)
        perform_login(
            driver,
            username=username,
            password=password,
            totp_seed=totp_seed,
            timeout=args.timeout,
        )
        run_post_login_flow(driver, timeout=args.timeout)
        driver.save_screenshot(args.screenshot)
        print(f"流程执行成功，截图已保存到: {args.screenshot}", flush=True)
        if args.pause:
            input("按回车继续...")
    except TimeoutException as exc:
        driver.save_screenshot("zdh_failed.png")
        raise SystemExit(f"页面超时，失败截图: zdh_failed.png\n{exc}") from exc
    except Exception as exc:
        driver.save_screenshot("zdh_failed.png")
        raise SystemExit(f"执行失败，失败截图: zdh_failed.png\n{exc}") from exc
    finally:
        if args.close:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
