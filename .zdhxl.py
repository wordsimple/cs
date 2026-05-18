#!/usr/bin/env python3
# coding: utf-8
# 自动化测试脚本，基于 Selenium，自动登录 OTG 系统，
# 发现侧边栏模块，并生成对应的测试用例和执行结果。
import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pyotp
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

LOGIN_URL = "https://operator1.otgpaytest.com/#/login"
MAIN_READY_SELECTOR = "#OtgPay"
SIDEBAR_ROOT_SELECTOR = (
    "#OtgPay > div > div.sidebar-container.has-logo > div.el-scrollbar > "
    "div.scrollbar-wrapper.el-scrollbar__wrap > div > ul"
)
CURRENCY_INPUT_SELECTOR = (
    "#OtgPay > div > div.main-container.hasTagsView > div > div.navbar > div.right-menu > "
    "div.el-select.right-menu-item.currency-select.el-select--medium > div > input"
)
CURRENCY_OPTION_XPATH = (
    "//div[contains(@class,'el-select-dropdown') and not(contains(@style,'display: none'))]"
    "//span[normalize-space()='印尼盾']"
)

DEFAULT_USERNAME = "ceshi"
DEFAULT_PASSWORD = "qw123456"
DEFAULT_TOTP_SEED = "6gnkpcdu22be5pahlbvfvffmnxcqzjzv"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = SCRIPT_DIR / "id_test_payin.local.env"
DEFAULT_OUTPUT = SCRIPT_DIR / ".zdhxl_cases.json"
DEFAULT_SCREENSHOT_DIR = SCRIPT_DIR / ".zdhxl_artifacts"


@dataclass
class ModuleItem:
    group_index: int
    item_index: int
    group_title: str
    item_title: str
    href: str


@dataclass
class ModuleCaseResult:
    case_id: str
    module_name: str
    route_hint: str
    status: str
    current_url: str
    page_title: str
    error: str
    search_tested: bool = False
    search_status: str = ""
    search_msg: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按模块自动生成并执行 OTG UI 自动化用例")
    parser.add_argument("--headless", action="store_true", help="无头模式运行")
    parser.add_argument("--discover-only", action="store_true", help="只发现模块并生成用例，不执行点击")
    parser.add_argument("--close", action="store_true", help="执行完成后关闭浏览器")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="测试用例和执行结果输出文件路径",
    )
    return parser.parse_args()


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}")


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        result[key] = value.strip().strip('"').strip("'")
    return result


def load_env_from_file() -> None:
    for key, value in parse_env_file(Path(os.environ.get("OTG_ENV_FILE", str(DEFAULT_ENV_FILE)))).items():
        os.environ.setdefault(key, value)


def load_login() -> tuple[str, str, str]:
    return (
        os.getenv("OTG_USERNAME", DEFAULT_USERNAME),
        os.getenv("OTG_PASSWORD", DEFAULT_PASSWORD),
        os.getenv("OTG_TOTP_SEED", DEFAULT_TOTP_SEED),
    )


def build_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_experimental_option("detach", True)
    return webdriver.Chrome(options=options)


def click_css(driver: webdriver.Chrome, wait: WebDriverWait, selector: str, desc: str) -> None:
    log(f"{desc}：开始")
    try:
        element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        try:
            element.click()
        except Exception:
            driver.execute_script("arguments[0].click();", element)
        log(f"{desc}：成功")
    except Exception as exc:
        log(f"{desc}：失败（{type(exc).__name__}）")
        raise


def click_xpath(driver: webdriver.Chrome, wait: WebDriverWait, xpath: str, desc: str) -> None:
    log(f"{desc}：开始")
    try:
        element = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        try:
            element.click()
        except Exception:
            driver.execute_script("arguments[0].click();", element)
        log(f"{desc}：成功")
    except Exception as exc:
        log(f"{desc}：失败（{type(exc).__name__}）")
        raise


def login(driver: webdriver.Chrome, wait: WebDriverWait, username: str, password: str, totp_seed: str) -> None:
    log("打开登录页")
    driver.get(LOGIN_URL)
    driver.maximize_window()

    wait.until(EC.visibility_of_element_located((By.NAME, "username"))).send_keys(username)
    wait.until(EC.visibility_of_element_located((By.NAME, "password"))).send_keys(password)
    wait.until(EC.visibility_of_element_located((By.NAME, "mfaCode"))).send_keys(pyotp.TOTP(totp_seed).now())
    click_xpath(driver, wait, "//*[text()='Login']", "点击登录按钮")
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, MAIN_READY_SELECTOR)))
    log("登录成功")


def choose_currency_if_visible(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    try:
        click_css(driver, wait, CURRENCY_INPUT_SELECTOR, "点击币种下拉框")
        click_xpath(driver, wait, CURRENCY_OPTION_XPATH, "选择印尼盾")
    except Exception:
        log("币种切换跳过")


def discover_modules(driver: webdriver.Chrome, wait: WebDriverWait) -> list[ModuleItem]:
    sidebar = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SIDEBAR_ROOT_SELECTOR)))
    raw_groups = driver.execute_script(
        """
        const root = arguments[0];
        return Array.from(root.children).map((group, groupIndex) => {
          const header = group.querySelector("li > div span") || group.querySelector("li > div");
          const groupTitle = ((header && header.innerText) || "").trim().replace(/\\s+/g, " ");
          const items = Array.from(group.querySelectorAll("li > ul > div")).map((item, itemIndex) => {
            const titleNode = item.querySelector("span") || item.querySelector("li") || item;
            const itemTitle = ((titleNode && titleNode.innerText) || item.innerText || "")
              .trim()
              .replace(/\\s+/g, " ");
            const anchor = item.querySelector("a");
            return {
              itemIndex,
              itemTitle,
              href: anchor ? (anchor.getAttribute("href") || "") : ""
            };
          }).filter(item => item.itemTitle);
          return {
            groupIndex,
            groupTitle: groupTitle || `模块组${groupIndex + 1}`,
            items
          };
        }).filter(group => group.items.length);
        """,
        sidebar,
    )

    modules: list[ModuleItem] = []
    for group in raw_groups:
        for item in group["items"]:
            modules.append(
                ModuleItem(
                    group_index=group["groupIndex"],
                    item_index=item["itemIndex"],
                    group_title=group["groupTitle"],
                    item_title=item["itemTitle"],
                    href=item["href"],
                )
            )
    log(f"自动发现模块数量：{len(modules)}")
    return modules


def module_header_selector(group_index: int) -> str:
    return f"{SIDEBAR_ROOT_SELECTOR} > div:nth-child({group_index + 1}) > li > div"


def module_item_selector(group_index: int, item_index: int) -> str:
    return (
        f"{SIDEBAR_ROOT_SELECTOR} > div:nth-child({group_index + 1}) > li > ul > "
        f"div:nth-child({item_index + 1})"
    )


def get_active_page_title(driver: webdriver.Chrome) -> str:
    title = driver.execute_script(
        """
        const activeTag = document.querySelector(".tags-view-item.active span");
        if (activeTag && activeTag.innerText.trim()) return activeTag.innerText.trim();
        const titleNode = document.querySelector(".app-main .el-card__header, .app-main h2, .app-main h3");
        if (titleNode && titleNode.innerText.trim()) return titleNode.innerText.trim();
        return document.title || "";
        """
    )
    return str(title).strip()


def wait_module_ready(driver: webdriver.Chrome, wait: WebDriverWait, old_url: str) -> None:
    def _ready(current_driver: webdriver.Chrome) -> bool:
        if current_driver.current_url != old_url:
            return True
        active = current_driver.execute_script(
            """
            return !!document.querySelector(
              ".sidebar-container .submenu-title-noDropdown.is-active, "
              + ".sidebar-container .is-active"
            );
            """
        )
        return bool(active)

    wait.until(_ready)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, MAIN_READY_SELECTOR)))


def screenshot_on_failure(driver: webdriver.Chrome, case_id: str) -> str:
    DEFAULT_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = DEFAULT_SCREENSHOT_DIR / f"{case_id}.png"
    driver.save_screenshot(str(path))
    return str(path)


def try_module_search(driver: webdriver.Chrome) -> tuple[bool, str]:
    script = """
    const buttons = Array.from(document.querySelectorAll('.app-main .el-button--primary, .main-container .el-button--primary, .filter-container .el-button--primary'));
    let searchBtn = buttons.find(b => b.innerText.includes('查询') || b.innerText.includes('Search') || b.innerText.includes('搜索'));
    if (!searchBtn && buttons.length > 0) {
        // 如果没找到明确带“查询”字样的，且有主按钮在搜索区附近，就尝试第一个
        const formBtn = document.querySelector('.app-main form .el-button--primary, .main-container form .el-button--primary, .filter-container .el-button--primary');
        searchBtn = formBtn;
    }
    if (searchBtn) {
        searchBtn.click();
        return {success: true, msg: '找到并点击了查询按钮'};
    }
    return {success: false, msg: '当前页面未发现查询按钮'};
    """
    try:
        res = driver.execute_script(script)
        return res["success"], res["msg"]
    except Exception as e:
        return False, f"执行查询脚本失败: {e}"


def run_module_case(driver: webdriver.Chrome, wait: WebDriverWait, module: ModuleItem) -> ModuleCaseResult:
    case_id = f"G{module.group_index + 1:02d}_I{module.item_index + 1:02d}"
    module_name = f"{module.group_title} / {module.item_title}"
    log(f"执行模块用例：{module_name}")
    old_url = driver.current_url

    try:
        click_css(driver, wait, module_header_selector(module.group_index), f"展开模块组 {module.group_title}")
        click_css(driver, wait, module_item_selector(module.group_index, module.item_index), f"进入模块 {module.item_title}")
        wait_module_ready(driver, wait, old_url)
        
        # 尝试查询测试
        search_success, search_msg = try_module_search(driver)
        search_status = "passed" if search_success else "skipped"
        if search_success:
            time.sleep(1.5)  # 等待可能出现的 loading 或请求发出
            log(f"  - {search_msg}")
        else:
            log(f"  - 查询测试跳过：{search_msg}")

        result = ModuleCaseResult(
            case_id=case_id,
            module_name=module_name,
            route_hint=module.href,
            status="passed",
            current_url=driver.current_url,
            page_title=get_active_page_title(driver),
            error="",
            search_tested=True,
            search_status=search_status,
            search_msg=search_msg,
        )
        log(f"模块用例通过：{module_name}")
        return result
    except Exception as exc:
        screenshot = screenshot_on_failure(driver, case_id)
        result = ModuleCaseResult(
            case_id=case_id,
            module_name=module_name,
            route_hint=module.href,
            status="failed",
            current_url=driver.current_url,
            page_title=get_active_page_title(driver),
            error=f"{type(exc).__name__}: {exc}; screenshot={screenshot}",
            search_tested=False,
            search_status="failed",
            search_msg="页面进入或加载失败",
        )
        log(f"模块用例失败：{module_name}")
        return result


def build_case_definition(module: ModuleItem) -> dict:
    return {
        "case_id": f"G{module.group_index + 1:02d}_I{module.item_index + 1:02d}",
        "module_name": f"{module.group_title} / {module.item_title}",
        "route_hint": module.href,
        "precondition": "已成功登录 OTG 系统",
        "steps": [
            f"展开菜单组《{module.group_title}》",
            f"点击模块《{module.item_title}》",
            "等待页面主区域渲染完成",
        ],
        "expected_result": "成功进入对应模块页面，且页面主区域正常显示",
    }


def save_output(path: Path, modules: list[ModuleItem], results: list[ModuleCaseResult]) -> None:
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "login_url": LOGIN_URL,
        "module_count": len(modules),
        "case_definitions": [build_case_definition(module) for module in modules],
        "execution_results": [asdict(result) for result in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"测试案例已输出到：{path}")


def main() -> None:
    args = parse_args()
    load_env_from_file()
    username, password, totp_seed = load_login()

    driver = build_driver(headless=args.headless)
    wait = WebDriverWait(driver, 20)

    try:
        login(driver, wait, username, password, totp_seed)
        choose_currency_if_visible(driver, wait)
        modules = discover_modules(driver, wait)

        results: list[ModuleCaseResult] = []
        if not args.discover_only:
            for module in modules:
                results.append(run_module_case(driver, wait, module))

        save_output(Path(args.output), modules, results)
    finally:
        if args.close:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
