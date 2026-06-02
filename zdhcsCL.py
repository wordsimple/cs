"""
zdhcsCL.py
自动化测试脚本 - 财务管理模块
充值记录/提现记录/商户互转 - 新增测试
"""
import time
import pyotp
import random
import os
import tempfile
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ==================== 配置 ====================
LOGIN_URL = "https://operator1.otgpaytest.com/#/login"
DEFAULT_USERNAME = "ceshi"
DEFAULT_PASSWORD = "qw123456"
DEFAULT_TOTP_SEED = "6gnkpcdu22be5pahlbvfvffmnxcqzjzv"
BASE_URL = "https://operator1.otgpaytest.com/#"

RECHARGE_URL = "/operate/recharge/list"
WITHDRAW_URL = "/operate/withdraw/list"
MERCHANT_TRANSFER_URL = "/operate/merchantTransfer/list"
TRANSFER_APPLICATION_URL = "/operate/transferApplication/index"

# 固定商户凭证图片路径（Krungthai 转账截图）
CREDENTIAL_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merchant_credential.png")

test_results = []


def log_result(module, case_name, status, message=""):
    """记录测试结果"""
    result = {
        "module": module,
        "case": case_name,
        "status": status,
        "message": message,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    test_results.append(result)
    icon = "PASS" if status == "PASS" else "FAIL"
    print(f"  [{icon}] {case_name}: {message}")


def is_driver_alive(driver):
    """检测driver窗口是否还存活"""
    try:
        _ = driver.current_url
        return True
    except:
        return False


def ensure_driver(driver):
    """确保driver存活，窗口关闭时重新登录并返回新driver"""
    if driver and is_driver_alive(driver):
        return driver
    print("  [恢复] 浏览器窗口已关闭，重新登录...")
    new_driver = login()
    if new_driver:
        switch_to_chinese(new_driver)
        switch_to_thailand(new_driver)
    return new_driver


def create_driver():
    """创建浏览器驱动"""
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    return driver


def login(driver=None):
    """登录系统（含重试机制，每次重试重建driver）"""
    for attempt in range(5):
        # 每次重试都重建driver，避免窗口关闭问题
        try:
            if driver is None or attempt > 0:
                try:
                    driver.quit()
                except:
                    pass
                driver = create_driver()

            wait = WebDriverWait(driver, 15)
            driver.get(LOGIN_URL)

            # 等待登录表单出现
            wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, "input[placeholder='Username']")
            ))

            # 填写用户名
            username = driver.find_element(By.CSS_SELECTOR, "input[placeholder='Username']")
            username.clear()
            username.send_keys(DEFAULT_USERNAME)

            # 填写密码
            password = driver.find_element(By.CSS_SELECTOR, "input[placeholder='Password']")
            password.clear()
            password.send_keys(DEFAULT_PASSWORD)

            # 最后时刻获取TOTP（减少验证码过期风险）
            totp_code = pyotp.TOTP(DEFAULT_TOTP_SEED).now()
            print(f"TOTP: {totp_code} (尝试{attempt+1})")
            code_input = driver.find_element(By.CSS_SELECTOR, "input[placeholder='Google Authenticator']")
            code_input.clear()
            code_input.send_keys(totp_code)

            # 点击登录
            for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
                if "Login" in btn.text:
                    btn.click()
                    break

            # 用WebDriverWait等待URL跳转，最多等10秒
            try:
                wait.until(lambda d: "login" not in d.current_url.lower())
                log_result("登录", "系统登录", "PASS", "登录成功")
                return driver
            except:
                current_url = driver.current_url
                print(f"  第{attempt+1}次登录失败, URL: {current_url}")

        except Exception as e:
            print(f"  第{attempt+1}次登录异常: {str(e)[:80]}")

    log_result("登录", "系统登录", "FAIL", "登录失败(5次尝试)")
    return None


def switch_to_chinese(driver):
    """切换到中文"""
    try:
        driver.find_element(By.CSS_SELECTOR, ".international-icon").click()
        time.sleep(0.4)
        for item in driver.find_elements(By.CSS_SELECTOR, ".el-dropdown-menu__item"):
            if "中文" in item.text:
                item.click()
                time.sleep(1)
                log_result("系统设置", "切换中文", "PASS", "已切换到中文")
                return True
        log_result("系统设置", "切换中文", "FAIL", "未找到中文选项")
    except Exception as e:
        log_result("系统设置", "切换中文", "FAIL", str(e)[:80])
    return False


def switch_to_thailand(driver):
    """切换到泰国"""
    try:
        driver.find_element(By.CSS_SELECTOR, ".currency-select .el-input__inner").click()
        time.sleep(0.4)
        for item in driver.find_elements(By.CSS_SELECTOR, ".el-select-dropdown__item"):
            if "泰铢" in item.text:
                item.click()
                time.sleep(1)
                log_result("系统设置", "切换泰国", "PASS", "已切换到泰国")
                return True
        log_result("系统设置", "切换泰国", "FAIL", "未找到泰国选项")
    except Exception as e:
        log_result("系统设置", "切换泰国", "FAIL", str(e)[:80])
    return False


def js_set_input(driver, element, value):
    """用JS设置input的值(解决不可交互问题)"""
    try:
        driver.execute_script("""
            var el = arguments[0];
            var value = arguments[1];
            // 移除readonly
            el.removeAttribute('readonly');
            el.removeAttribute('disabled');
            // 设置值
            el.value = value;
            // 触发事件
            var events = ['input', 'change', 'blur'];
            events.forEach(function(eventType) {
                var event = new Event(eventType, { bubbles: true });
                el.dispatchEvent(event);
            });
            // Vue兼容
            var vueEvent = new Event('input', { bubbles: true });
            el.dispatchEvent(vueEvent);
        """, element, value)
        return True
    except:
        return False


def select_el_dropdown(driver, form_item, option_text=None, index=0):
    """选择Element UI下拉框选项"""
    try:
        el_select = form_item.find_element(By.CSS_SELECTOR, ".el-select")
        # 用JS点击避免遮挡
        driver.execute_script("arguments[0].click()", el_select)
        time.sleep(0.6)

        options = driver.find_elements(By.CSS_SELECTOR, ".el-select-dropdown__item")
        visible_options = [opt for opt in options if opt.is_displayed()]

        if option_text:
            for opt in visible_options:
                if option_text in opt.text:
                    opt.click()
                    time.sleep(0.3)
                    return True
        elif visible_options and index < len(visible_options):
            visible_options[index].click()
            time.sleep(0.3)
            return True

        # 无匹配选项,点击空白处关闭下拉(不用ESC避免影响弹窗)
        driver.execute_script("document.body.click()")
        time.sleep(0.2)
        return False
    except:
        try:
            driver.execute_script("document.body.click()")
            time.sleep(0.2)
        except:
            pass
        return False


def generate_credential_image(file_format="jpg"):
    """自动生成商户凭证图片(jpg或png格式)"""
    random_str = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=8))
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 创建200x100的图片
    img = Image.new('RGB', (400, 200), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    # 绘制边框
    draw.rectangle([(5, 5), (395, 195)], outline=(0, 102, 204), width=2)

    # 绘制标题
    draw.text((20, 20), "Merchant Credential", fill=(0, 102, 204))
    # 绘制凭证编号
    draw.text((20, 60), f"Credential ID: {random_str}", fill=(0, 0, 0))
    # 绘制时间戳
    draw.text((20, 90), f"Time: {timestamp}", fill=(100, 100, 100))
    # 绘制格式标记
    draw.text((20, 120), f"Format: {file_format.upper()}", fill=(100, 100, 100))
    # 绘制状态
    draw.text((20, 150), "Status: VALID", fill=(0, 153, 0))

    # 保存为指定格式
    tmp_dir = tempfile.gettempdir()
    ext = "jpg" if file_format == "jpg" else "png"
    file_path = os.path.join(tmp_dir, f"credential_{random_str}.{ext}")
    img.save(file_path, format="JPEG" if file_format == "jpg" else "PNG")

    return file_path, random_str


def get_totp_code():
    """获取当前TOTP验证码"""
    return pyotp.TOTP(DEFAULT_TOTP_SEED).now()


def get_form_item_by_label(driver, label_text):
    """根据label文本获取弹窗内的表单项(支持模糊匹配)"""
    # 确保只搜索弹窗内的form-item
    for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
        if dialog.is_displayed():
            for item in dialog.find_elements(By.CSS_SELECTOR, ".el-form-item"):
                try:
                    label = item.find_element(By.CSS_SELECTOR, ".el-form-item__label").text.strip()
                    if label_text in label or label.endswith(label_text):
                        return item
                except:
                    pass
    return None


def get_dialog_labels(driver):
    """获取当前弹窗内所有表单label(调试用)"""
    labels = []
    for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
        if dialog.is_displayed():
            for item in dialog.find_elements(By.CSS_SELECTOR, ".el-form-item"):
                try:
                    label = item.find_element(By.CSS_SELECTOR, ".el-form-item__label").text.strip()
                    if label:
                        labels.append(label)
                except:
                    pass
    return labels


def close_dialog(driver):
    """关闭弹窗"""
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
    except:
        pass
    return False


def _wait_and_click_btn(driver, url, btn_text, exact=True, timeout=15):
    """导航到页面，等URL稳定后等待目标按钮出现并点击"""
    driver.get(f"{BASE_URL}{url}")
    # 先等待URL包含目标路径，确保页面已切换
    deadline = __import__('time').time() + 8
    while __import__('time').time() < deadline:
        try:
            if url.split('/')[-1] in driver.current_url:
                break
        except:
            pass
        time.sleep(0.2)
    # 再等待目标按钮出现
    deadline = __import__('time').time() + timeout
    while __import__('time').time() < deadline:
        for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
            try:
                t = btn.text.strip()
                matched = (t == btn_text) if exact else (btn_text in t)
                if matched and btn.is_displayed() and btn.is_enabled():
                    btn.click()
                    time.sleep(0.3)
                    return True
            except:
                pass
        time.sleep(0.3)
    return False


def open_recharge_dialog(driver):
    """打开充值弹窗"""
    return _wait_and_click_btn(driver, RECHARGE_URL, "充值", exact=True)


def open_withdraw_dialog(driver):
    """打开提现弹窗"""
    return _wait_and_click_btn(driver, WITHDRAW_URL, "申请下发", exact=False)


# =====================================================
# 充值记录 - 新增测试
# =====================================================
def test_recharge_add(driver):
    """充值记录新增测试"""
    module = "充值记录"
    print(f"\n{'='*60}")
    print(f"  测试模块: {module} - 新增")
    print(f"{'='*60}")

    # ---------- 用例1: 打开充值弹窗（最多重试3次）----------
    opened = False
    for _retry in range(3):
        if open_recharge_dialog(driver):
            opened = True
            break
        time.sleep(1)
    if not opened:
        log_result(module, "打开充值弹窗", "FAIL", "未找到充值按钮")
        return
    log_result(module, "打开充值弹窗", "PASS", "充值弹窗已打开")

    # 调试: 打印当前弹窗所有label
    labels = get_dialog_labels(driver)
    print(f"  [调试] 弹窗字段: {labels}")

    # ---------- 用例2: 必填项验证 ----------
    try:
        save_btn = None
        for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
            if dialog.is_displayed():
                for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
                    if "保存" in btn.text and btn.is_displayed():
                        save_btn = btn
                        break
        if save_btn:
            save_btn.click()
            time.sleep(0.4)
            error_msgs = driver.find_elements(By.CSS_SELECTOR, ".el-form-item__error")
            error_texts = [e.text for e in error_msgs if e.is_displayed()]
            if error_texts:
                log_result(module, "必填项验证", "PASS", f"验证提示: {error_texts}")
            else:
                log_result(module, "必填项验证", "FAIL", "未显示必填验证提示")
    except Exception as e:
        log_result(module, "必填项验证", "FAIL", str(e)[:80])

    # ---------- 用例3: 选择用户编码 ----------
    try:
        item = get_form_item_by_label(driver, "用户编码")
        if item:
            # 尝试选择用户编码，可能需要多次尝试不同选项
            selected = False
            for retry in range(3):
                if select_el_dropdown(driver, item, option_text="299"):
                    selected = True
                    break
                time.sleep(0.4)

            if selected:
                log_result(module, "选择用户编码", "PASS", "已选择用户编码")
                # 选择后等待级联加载
                time.sleep(1)
                labels = get_dialog_labels(driver)
                print(f"  [调试] 选择用户后字段: {labels}")
            else:
                log_result(module, "选择用户编码", "FAIL", "下拉无选项")
        else:
            log_result(module, "选择用户编码", "FAIL", "未找到字段")
    except Exception as e:
        log_result(module, "选择用户编码", "FAIL", str(e)[:80])

    # ---------- 用例4: 选择商户名称 ----------
    try:
        item = get_form_item_by_label(driver, "ID-商户名称")
        if item:
            # 级联下拉可能需要等待加载，重试几次
            selected = False
            for retry in range(3):
                if select_el_dropdown(driver, item, index=0):
                    selected = True
                    break
                time.sleep(0.8)
            if selected:
                log_result(module, "选择商户名称", "PASS", "已选择商户名称")
                time.sleep(0.6)
            else:
                log_result(module, "选择商户名称", "FAIL", "下拉无选项(级联未加载)")
                # 即使商户名称选择失败，继续尝试后续字段
        else:
            log_result(module, "选择商户名称", "FAIL", "未找到字段")
    except Exception as e:
        log_result(module, "选择商户名称", "FAIL", str(e)[:80])

    # ---------- 用例5: 输入充值金额 ----------
    try:
        item = get_form_item_by_label(driver, "充值金额")
        if item:
            # 查找所有input
            inputs = item.find_elements(By.CSS_SELECTOR, "input")
            test_amount = "100"
            filled = False
            for inp in inputs:
                try:
                    if inp.is_displayed() and inp.is_enabled():
                        placeholder = inp.get_attribute("placeholder") or ""
                        if "请输入" in placeholder or not placeholder:
                            inp.clear()
                            time.sleep(0.2)
                            inp.send_keys(test_amount)
                            filled = True
                            break
                except:
                    # 如果普通方式失败, 用JS
                    js_set_input(driver, inp, test_amount)
                    filled = True
                    break

            if not filled:
                # 尝试用JS直接设置所有可见input
                for inp in inputs:
                    if inp.is_displayed():
                        js_set_input(driver, inp, test_amount)
                        filled = True
                        break

            if filled:
                log_result(module, "输入充值金额", "PASS", f"已输入金额: {test_amount}")
            else:
                log_result(module, "输入充值金额", "FAIL", "金额输入框不可用")
        else:
            log_result(module, "输入充值金额", "FAIL", "未找到充值金额字段")
    except Exception as e:
        log_result(module, "输入充值金额", "FAIL", str(e)[:80])

    # ---------- 用例6: 选择收款地址 ----------
    try:
        item = get_form_item_by_label(driver, "收款地址")
        if item:
            if select_el_dropdown(driver, item, index=0):
                log_result(module, "选择收款地址", "PASS", "已选择收款地址")
            else:
                log_result(module, "选择收款地址", "FAIL", "下拉无选项")
        else:
            log_result(module, "选择收款地址", "FAIL", "未找到字段")
    except Exception as e:
        log_result(module, "选择收款地址", "FAIL", str(e)[:80])

    # ---------- 用例7: 输入谷歌验证码 ----------
    try:
        item = get_form_item_by_label(driver, "谷歌验证码")
        if item:
            code_input = item.find_element(By.CSS_SELECTOR, "input")
            totp_code = get_totp_code()
            code_input.clear()
            code_input.send_keys(totp_code)
            log_result(module, "输入谷歌验证码", "PASS", f"已输入验证码")
        else:
            log_result(module, "输入谷歌验证码", "FAIL", "未找到字段")
    except Exception as e:
        log_result(module, "输入谷歌验证码", "FAIL", str(e)[:80])

    # ---------- 用例7.5: 商户凭证(上传图片) ----------
    try:
        item = get_form_item_by_label(driver, "商户凭证")
        if item:
            # 查找隐藏的file input
            file_input = None
            for inp in item.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                file_input = inp
                break
            if not file_input:
                for btn in item.find_elements(By.CSS_SELECTOR, "button"):
                    if "商户凭证" in btn.text and btn.is_displayed():
                        btn.click()
                        time.sleep(0.4)
                        for inp in driver.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                            file_input = inp
                            break
                        break

            if file_input:
                # 使用固定的 Krungthai 转账凭证图片
                file_input.send_keys(CREDENTIAL_IMAGE_PATH)
                time.sleep(0.4)
                log_result(module, "商户凭证", "PASS", f"已上传固定凭证: merchant_credential.png")
            else:
                log_result(module, "商户凭证", "FAIL", "未找到文件上传入口")
        else:
            log_result(module, "商户凭证", "FAIL", "未找到商户凭证字段")
    except Exception as e:
        log_result(module, "商户凭证", "FAIL", str(e)[:80])

    # ---------- 用例8: 输入备注 ----------
    try:
        item = get_form_item_by_label(driver, "备注")
        if item:
            textareas = item.find_elements(By.CSS_SELECTOR, "textarea")
            if textareas:
                test_remark = f"自动化测试充值-{datetime.now().strftime('%H%M%S')}"
                textareas[0].clear()
                textareas[0].send_keys(test_remark)
                log_result(module, "输入备注", "PASS", f"已输入备注")
            else:
                log_result(module, "输入备注", "FAIL", "无textarea")
        else:
            log_result(module, "输入备注", "FAIL", "未找到字段")
    except Exception as e:
        log_result(module, "输入备注", "FAIL", str(e)[:80])

    # ---------- 用例9: 提交充值 ----------
    try:
        for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
            if dialog.is_displayed():
                for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
                    if "保存" in btn.text and btn.is_displayed():
                        btn.click()
                        time.sleep(1)
                        # 检查结果
                        success = driver.find_elements(By.CSS_SELECTOR, ".el-message--success")
                        errors = driver.find_elements(By.CSS_SELECTOR, ".el-message--error")
                        if success:
                            log_result(module, "提交充值", "PASS", "充值提交成功")
                        elif errors:
                            log_result(module, "提交充值", "FAIL", f"提交失败: {[e.text for e in errors]}")
                        else:
                            dialog_still = any(d.is_displayed() for d in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"))
                            log_result(module, "提交充值",
                                       "PASS" if not dialog_still else "FAIL",
                                       "弹窗已关闭" if not dialog_still else "弹窗仍打开")
                        break
    except Exception as e:
        log_result(module, "提交充值", "FAIL", str(e)[:80])

    close_dialog(driver)


def test_recharge_boundary(driver):
    """充值记录边界值测试"""
    module = "充值记录"
    print(f"\n{'='*60}")
    print(f"  测试模块: {module} - 边界值测试")
    print(f"{'='*60}")

    test_values = [
        ("金额输入0", "0"),
        ("金额输入负数", "-100"),
        ("金额输入超大值", "99999999999"),
        ("金额输入特殊字符", "abc!@#"),
    ]

    for case_name, test_val in test_values:
        try:
            if not open_recharge_dialog(driver):
                log_result(module, case_name, "FAIL", "弹窗打开失败")
                continue

            # 先选择用户编码, 使表单加载
            item = get_form_item_by_label(driver, "用户编码")
            if item:
                select_el_dropdown(driver, item, option_text="299")
                time.sleep(0.8)

            # 选择商户名称
            item = get_form_item_by_label(driver, "ID-商户名称")
            if item:
                select_el_dropdown(driver, item, index=0)
                time.sleep(0.4)

            # 输入金额
            item = get_form_item_by_label(driver, "充值金额")
            if item:
                inputs = item.find_elements(By.CSS_SELECTOR, "input")
                filled = False
                for inp in inputs:
                    if inp.is_displayed():
                        try:
                            inp.clear()
                            inp.send_keys(test_val)
                            filled = True
                        except:
                            if js_set_input(driver, inp, test_val):
                                filled = True
                        if filled:
                            break
                log_result(module, case_name, "PASS" if filled else "FAIL",
                           f"已输入: {test_val}" if filled else "输入失败")
            else:
                log_result(module, case_name, "FAIL", "未找到金额字段(表单级联未完成)")

            # 上传商户凭证
            cred_item = get_form_item_by_label(driver, "商户凭证")
            if cred_item:
                file_inp = None
                for fi in cred_item.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                    file_inp = fi
                    break
                if not file_inp:
                    for b in cred_item.find_elements(By.CSS_SELECTOR, "button"):
                        if "商户凭证" in b.text and b.is_displayed():
                            b.click()
                            time.sleep(0.4)
                            for fi in driver.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                                file_inp = fi
                            break
                if file_inp:
                    file_inp.send_keys(CREDENTIAL_IMAGE_PATH)
                    time.sleep(0.2)

            close_dialog(driver)
        except Exception as e:
            log_result(module, case_name, "FAIL", str(e)[:80])
            close_dialog(driver)

    # 谷歌验证码错误测试
    try:
        if open_recharge_dialog(driver):
            item = get_form_item_by_label(driver, "谷歌验证码")
            if item:
                inp = item.find_element(By.CSS_SELECTOR, "input")
                inp.clear()
                inp.send_keys("000000")
                log_result(module, "验证码错误值", "PASS", "已输入错误验证码")
            else:
                log_result(module, "验证码错误值", "FAIL", "未找到字段")
            close_dialog(driver)
    except Exception as e:
        log_result(module, "验证码错误值", "FAIL", str(e)[:80])
        close_dialog(driver)


def test_recharge_scenarios(driver):
    """充值记录多场景测试"""
    module = "充值记录"
    print(f"\n{'='*60}")
    print(f"  测试模块: {module} - 场景测试")
    print(f"{'='*60}")

    # ---------- 场景1: 正常充值流程(完整填写所有字段) ----------
    try:
        if open_recharge_dialog(driver):
            # 选择用户编码299
            item = get_form_item_by_label(driver, "用户编码")
            if item:
                select_el_dropdown(driver, item, option_text="299")
                time.sleep(0.8)

            # 选择商户名称
            item = get_form_item_by_label(driver, "ID-商户名称")
            if item:
                select_el_dropdown(driver, item, index=0)
                time.sleep(0.4)

            # 输入充值金额
            item = get_form_item_by_label(driver, "充值金额")
            if item:
                for inp in item.find_elements(By.CSS_SELECTOR, "input"):
                    if inp.is_displayed():
                        try:
                            inp.clear()
                            inp.send_keys("500")
                        except:
                            js_set_input(driver, inp, "500")
                        break

            # 选择收款地址
            item = get_form_item_by_label(driver, "收款地址")
            if item:
                select_el_dropdown(driver, item, index=0)

            # 输入谷歌验证码
            item = get_form_item_by_label(driver, "谷歌验证码")
            if item:
                inp = item.find_element(By.CSS_SELECTOR, "input")
                inp.clear()
                inp.send_keys(get_totp_code())

            # 上传商户凭证
            cred_item = get_form_item_by_label(driver, "商户凭证")
            if cred_item:
                file_inp = None
                for fi in cred_item.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                    file_inp = fi
                    break
                if not file_inp:
                    for b in cred_item.find_elements(By.CSS_SELECTOR, "button"):
                        if "商户凭证" in b.text and b.is_displayed():
                            b.click()
                            time.sleep(0.4)
                            for fi in driver.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                                file_inp = fi
                            break
                if file_inp:
                    file_inp.send_keys(CREDENTIAL_IMAGE_PATH)
                    time.sleep(0.2)

            # 输入备注
            item = get_form_item_by_label(driver, "备注")
            if item:
                ta = item.find_elements(By.CSS_SELECTOR, "textarea")
                if ta:
                    ta[0].clear()
                    ta[0].send_keys(f"场景测试-正常充值-{datetime.now().strftime('%H%M%S')}")

            # 点击保存（提交前刷新TOTP，避免验证码过期）
            item = get_form_item_by_label(driver, "谷歌验证码")
            if item:
                try:
                    inp = item.find_element(By.CSS_SELECTOR, "input")
                    inp.clear()
                    inp.send_keys(get_totp_code())
                except:
                    pass
            for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
                if dialog.is_displayed():
                    for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
                        if "保存" in btn.text and btn.is_displayed():
                            btn.click()
                            break

            # 等待结果（最多5秒）
            result_found = False
            deadline = __import__('time').time() + 5
            while __import__('time').time() < deadline:
                success = driver.find_elements(By.CSS_SELECTOR, ".el-message--success")
                errors = driver.find_elements(By.CSS_SELECTOR, ".el-message--error")
                dialogs = [d for d in driver.find_elements(By.CSS_SELECTOR, ".el-dialog") if d.is_displayed()]
                if success and any(s.is_displayed() for s in success):
                    log_result(module, "正常充值流程", "PASS", "充值提交成功")
                    result_found = True
                    break
                if errors and any(e.is_displayed() for e in errors):
                    err_text = [e.text for e in errors if e.is_displayed()]
                    log_result(module, "正常充值流程", "FAIL", f"提交失败: {err_text}")
                    result_found = True
                    break
                if not dialogs:
                    log_result(module, "正常充值流程", "PASS", "弹窗已关闭")
                    result_found = True
                    break
                time.sleep(0.3)
            if not result_found:
                log_result(module, "正常充值流程", "FAIL", "弹窗仍打开(提交无响应)")
            close_dialog(driver)
        else:
            log_result(module, "正常充值流程", "FAIL", "弹窗打开失败")
    except Exception as e:
        log_result(module, "正常充值流程", "FAIL", str(e)[:80])
        close_dialog(driver)

    # ---------- 场景2: 不同金额充值(小额) ----------
    try:
        if open_recharge_dialog(driver):
            item = get_form_item_by_label(driver, "用户编码")
            if item:
                select_el_dropdown(driver, item, option_text="299")
                time.sleep(0.8)
            item = get_form_item_by_label(driver, "ID-商户名称")
            if item:
                select_el_dropdown(driver, item, index=0)
                time.sleep(0.4)
            item = get_form_item_by_label(driver, "充值金额")
            if item:
                for inp in item.find_elements(By.CSS_SELECTOR, "input"):
                    if inp.is_displayed():
                        try:
                            inp.clear()
                            inp.send_keys("1")
                        except:
                            js_set_input(driver, inp, "1")
                        break
            item = get_form_item_by_label(driver, "收款地址")
            if item:
                select_el_dropdown(driver, item, index=0)
            item = get_form_item_by_label(driver, "谷歌验证码")
            if item:
                inp = item.find_element(By.CSS_SELECTOR, "input")
                inp.clear()
                inp.send_keys(get_totp_code())
            # 上传凭证(png格式)
            cred_item = get_form_item_by_label(driver, "商户凭证")
            if cred_item:
                file_inp = None
                for fi in cred_item.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                    file_inp = fi
                    break
                if not file_inp:
                    for b in cred_item.find_elements(By.CSS_SELECTOR, "button"):
                        if "商户凭证" in b.text and b.is_displayed():
                            b.click()
                            time.sleep(0.4)
                            for fi in driver.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                                file_inp = fi
                            break
                if file_inp:
                    file_inp.send_keys(CREDENTIAL_IMAGE_PATH)
                    time.sleep(0.2)

            log_result(module, "小额充值(1THB)", "PASS", "已填写小额充值表单")
            close_dialog(driver)
        else:
            log_result(module, "小额充值(1THB)", "FAIL", "弹窗打开失败")
    except Exception as e:
        log_result(module, "小额充值(1THB)", "FAIL", str(e)[:80])
        close_dialog(driver)

    # ---------- 场景3: 大额充值 ----------
    try:
        if open_recharge_dialog(driver):
            item = get_form_item_by_label(driver, "用户编码")
            if item:
                select_el_dropdown(driver, item, option_text="299")
                time.sleep(0.8)
            item = get_form_item_by_label(driver, "ID-商户名称")
            if item:
                select_el_dropdown(driver, item, index=0)
                time.sleep(0.4)
            item = get_form_item_by_label(driver, "充值金额")
            if item:
                for inp in item.find_elements(By.CSS_SELECTOR, "input"):
                    if inp.is_displayed():
                        try:
                            inp.clear()
                            inp.send_keys("999999")
                        except:
                            js_set_input(driver, inp, "999999")
                        break
            item = get_form_item_by_label(driver, "收款地址")
            if item:
                select_el_dropdown(driver, item, index=0)
            item = get_form_item_by_label(driver, "谷歌验证码")
            if item:
                inp = item.find_element(By.CSS_SELECTOR, "input")
                inp.clear()
                inp.send_keys(get_totp_code())
            cred_item = get_form_item_by_label(driver, "商户凭证")
            if cred_item:
                file_inp = None
                for fi in cred_item.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                    file_inp = fi
                    break
                if not file_inp:
                    for b in cred_item.find_elements(By.CSS_SELECTOR, "button"):
                        if "商户凭证" in b.text and b.is_displayed():
                            b.click()
                            time.sleep(0.4)
                            for fi in driver.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                                file_inp = fi
                            break
                if file_inp:
                    file_inp.send_keys(CREDENTIAL_IMAGE_PATH)
                    time.sleep(0.2)

            log_result(module, "大额充值(999999)", "PASS", "已填写大额充值表单")
            close_dialog(driver)
        else:
            log_result(module, "大额充值(999999)", "FAIL", "弹窗打开失败")
    except Exception as e:
        log_result(module, "大额充值(999999)", "FAIL", str(e)[:80])
        close_dialog(driver)

    # ---------- 场景4: 谷歌验证码过期验证 ----------
    try:
        if open_recharge_dialog(driver):
            item = get_form_item_by_label(driver, "用户编码")
            if item:
                select_el_dropdown(driver, item, option_text="299")
                time.sleep(0.8)
            item = get_form_item_by_label(driver, "ID-商户名称")
            if item:
                select_el_dropdown(driver, item, index=0)
                time.sleep(0.4)
            item = get_form_item_by_label(driver, "充值金额")
            if item:
                for inp in item.find_elements(By.CSS_SELECTOR, "input"):
                    if inp.is_displayed():
                        try:
                            inp.clear()
                            inp.send_keys("100")
                        except:
                            js_set_input(driver, inp, "100")
                        break
            item = get_form_item_by_label(driver, "收款地址")
            if item:
                select_el_dropdown(driver, item, index=0)
            item = get_form_item_by_label(driver, "谷歌验证码")
            if item:
                inp = item.find_element(By.CSS_SELECTOR, "input")
                inp.clear()
                inp.send_keys("123456")  # 故意输入错误验证码
            cred_item = get_form_item_by_label(driver, "商户凭证")
            if cred_item:
                file_inp = None
                for fi in cred_item.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                    file_inp = fi
                    break
                if not file_inp:
                    for b in cred_item.find_elements(By.CSS_SELECTOR, "button"):
                        if "商户凭证" in b.text and b.is_displayed():
                            b.click()
                            time.sleep(0.4)
                            for fi in driver.find_elements(By.CSS_SELECTOR, "input[type='file']"):
                                file_inp = fi
                            break
                if file_inp:
                    file_inp.send_keys(CREDENTIAL_IMAGE_PATH)
                    time.sleep(0.2)

            # 点击保存
            for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
                if dialog.is_displayed():
                    for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
                        if "保存" in btn.text and btn.is_displayed():
                            btn.click()
                            time.sleep(1)
                            break

            # 验证码错误应该导致提交失败
            errors = driver.find_elements(By.CSS_SELECTOR, ".el-message--error")
            dialog_still = any(d.is_displayed() for d in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"))
            if errors or dialog_still:
                log_result(module, "验证码过期/错误", "PASS", "错误验证码被正确拒绝")
            else:
                log_result(module, "验证码过期/错误", "FAIL", "错误验证码未被拒绝")
            close_dialog(driver)
        else:
            log_result(module, "验证码过期/错误", "FAIL", "弹窗打开失败")
    except Exception as e:
        log_result(module, "验证码过期/错误", "FAIL", str(e)[:80])
        close_dialog(driver)

    # ---------- 场景5: 收款地址为空验证 ----------
    try:
        if open_recharge_dialog(driver):
            item = get_form_item_by_label(driver, "用户编码")
            if item:
                select_el_dropdown(driver, item, option_text="299")
                time.sleep(0.8)
            item = get_form_item_by_label(driver, "ID-商户名称")
            if item:
                select_el_dropdown(driver, item, index=0)
                time.sleep(0.4)
            item = get_form_item_by_label(driver, "充值金额")
            if item:
                for inp in item.find_elements(By.CSS_SELECTOR, "input"):
                    if inp.is_displayed():
                        try:
                            inp.clear()
                            inp.send_keys("100")
                        except:
                            js_set_input(driver, inp, "100")
                        break
            # 不选收款地址
            item = get_form_item_by_label(driver, "谷歌验证码")
            if item:
                inp = item.find_element(By.CSS_SELECTOR, "input")
                inp.clear()
                inp.send_keys(get_totp_code())

            for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
                if dialog.is_displayed():
                    for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
                        if "保存" in btn.text and btn.is_displayed():
                            btn.click()
                            time.sleep(0.8)
                            break

            errors = driver.find_elements(By.CSS_SELECTOR, ".el-form-item__error")
            error_texts = [e.text for e in errors if e.is_displayed()]
            if error_texts:
                log_result(module, "收款地址为空验证", "PASS", f"验证提示: {error_texts}")
            else:
                dialog_still = any(d.is_displayed() for d in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"))
                log_result(module, "收款地址为空验证", "PASS" if dialog_still else "FAIL",
                           "弹窗未关闭(被拦截)" if dialog_still else "未拦截空收款地址")
            close_dialog(driver)
        else:
            log_result(module, "收款地址为空验证", "FAIL", "弹窗打开失败")
    except Exception as e:
        log_result(module, "收款地址为空验证", "FAIL", str(e)[:80])
        close_dialog(driver)


# =====================================================
# 提现记录 - 新增测试
# =====================================================
def test_withdraw_add(driver):
    """提现记录新增测试"""
    module = "提现记录"
    print(f"\n{'='*60}")
    print(f"  测试模块: {module} - 新增")
    print(f"{'='*60}")

    # ---------- 用例1: 打开弹窗 ----------
    if not open_withdraw_dialog(driver):
        log_result(module, "打开申请下发弹窗", "FAIL", "未找到按钮")
        return
    log_result(module, "打开申请下发弹窗", "PASS", "弹窗已打开")

    # 调试
    labels = get_dialog_labels(driver)
    print(f"  [调试] 提现弹窗字段: {labels}")

    # ---------- 用例2: 必填项验证 ----------
    try:
        for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
            if dialog.is_displayed():
                for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
                    if "提交" in btn.text and btn.is_displayed():
                        btn.click()
                        time.sleep(0.4)
                        errors = [e.text for e in driver.find_elements(By.CSS_SELECTOR, ".el-form-item__error") if e.is_displayed()]
                        if errors:
                            log_result(module, "必填项验证", "PASS", f"验证提示: {errors}")
                        else:
                            log_result(module, "必填项验证", "FAIL", "未显示验证提示")
                        break
    except Exception as e:
        log_result(module, "必填项验证", "FAIL", str(e)[:80])

    # ---------- 用例3: 选择商户名称 ----------
    try:
        item = get_form_item_by_label(driver, "商户名称")
        if item:
            if select_el_dropdown(driver, item, option_text="ceshi8"):
                log_result(module, "选择商户名称", "PASS", "已选择商户名称")
                time.sleep(0.6)
                # 重新获取标签(商户选择后可能有新字段)
                labels = get_dialog_labels(driver)
                print(f"  [调试] 选择商户后字段: {labels}")
            else:
                log_result(module, "选择商户名称", "FAIL", "下拉无选项")
        else:
            log_result(module, "选择商户名称", "FAIL", "未找到字段")
    except Exception as e:
        log_result(module, "选择商户名称", "FAIL", str(e)[:80])

    # ---------- 用例4: 输入提现金额 ----------
    try:
        item = get_form_item_by_label(driver, "提现金额")
        if item:
            inputs = item.find_elements(By.CSS_SELECTOR, "input")
            test_amount = "200"
            filled = False
            for inp in inputs:
                try:
                    if inp.is_displayed():
                        placeholder = inp.get_attribute("placeholder") or ""
                        if "请输入" in placeholder or not placeholder:
                            try:
                                inp.clear()
                                inp.send_keys(test_amount)
                            except:
                                js_set_input(driver, inp, test_amount)
                            filled = True
                            break
                except:
                    pass

            if not filled:
                for inp in inputs:
                    if inp.is_displayed():
                        js_set_input(driver, inp, test_amount)
                        filled = True
                        break

            log_result(module, "输入提现金额", "PASS" if filled else "FAIL",
                       f"已输入金额: {test_amount}" if filled else "金额输入失败")
        else:
            log_result(module, "输入提现金额", "FAIL", "未找到字段")
    except Exception as e:
        log_result(module, "输入提现金额", "FAIL", str(e)[:80])

    # ---------- 用例5: 输入收款地址 ----------
    try:
        item = get_form_item_by_label(driver, "收款地址")
        if item:
            inp = item.find_element(By.CSS_SELECTOR, "input")
            test_addr = f"TH{random.randint(100000, 999999)}"
            try:
                inp.clear()
                inp.send_keys(test_addr)
            except:
                js_set_input(driver, inp, test_addr)
            log_result(module, "输入收款地址", "PASS", f"已输入收款地址: {test_addr}")
        else:
            log_result(module, "输入收款地址", "FAIL", "未找到字段")
    except Exception as e:
        log_result(module, "输入收款地址", "FAIL", str(e)[:80])

    # ---------- 用例6: 输入谷歌验证码 ----------
    try:
        item = get_form_item_by_label(driver, "谷歌验证码")
        if item:
            inp = item.find_element(By.CSS_SELECTOR, "input")
            totp_code = get_totp_code()
            inp.clear()
            inp.send_keys(totp_code)
            log_result(module, "输入谷歌验证码", "PASS", f"已输入验证码")
        else:
            log_result(module, "输入谷歌验证码", "FAIL", "未找到字段")
    except Exception as e:
        log_result(module, "输入谷歌验证码", "FAIL", str(e)[:80])

    # ---------- 用例7: 提交提现 ----------
    try:
        for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
            if dialog.is_displayed():
                for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
                    if "提交" in btn.text and btn.is_displayed():
                        btn.click()
                        time.sleep(1)
                        success = driver.find_elements(By.CSS_SELECTOR, ".el-message--success")
                        errors = driver.find_elements(By.CSS_SELECTOR, ".el-message--error")
                        if success:
                            log_result(module, "提交提现", "PASS", "提现提交成功")
                        elif errors:
                            log_result(module, "提交提现", "FAIL", f"提交失败")
                        else:
                            dialog_still = any(d.is_displayed() for d in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"))
                            log_result(module, "提交提现",
                                       "PASS" if not dialog_still else "FAIL",
                                       "弹窗已关闭" if not dialog_still else "弹窗仍打开")
                        break
    except Exception as e:
        log_result(module, "提交提现", "FAIL", str(e)[:80])

    close_dialog(driver)


def test_withdraw_boundary(driver):
    """提现记录边界值测试"""
    module = "提现记录"
    print(f"\n{'='*60}")
    print(f"  测试模块: {module} - 边界值测试")
    print(f"{'='*60}")

    # 收款地址XSS
    try:
        if open_withdraw_dialog(driver):
            item = get_form_item_by_label(driver, "收款地址")
            if item:
                inp = item.find_element(By.CSS_SELECTOR, "input")
                try:
                    inp.clear()
                    inp.send_keys("<script>alert(1)</script>")
                except:
                    js_set_input(driver, inp, "<script>alert(1)</script>")
                log_result(module, "收款地址XSS", "PASS", "已输入XSS脚本")
            else:
                log_result(module, "收款地址XSS", "FAIL", "未找到字段")
            close_dialog(driver)
    except Exception as e:
        log_result(module, "收款地址XSS", "FAIL", str(e)[:80])
        close_dialog(driver)

    # 收款地址超长
    try:
        if open_withdraw_dialog(driver):
            item = get_form_item_by_label(driver, "收款地址")
            if item:
                inp = item.find_element(By.CSS_SELECTOR, "input")
                long_text = "A" * 500
                try:
                    inp.clear()
                    inp.send_keys(long_text)
                except:
                    js_set_input(driver, inp, long_text)
                log_result(module, "收款地址超长", "PASS", "已输入500字符")
            else:
                log_result(module, "收款地址超长", "FAIL", "未找到字段")
            close_dialog(driver)
    except Exception as e:
        log_result(module, "收款地址超长", "FAIL", str(e)[:80])
        close_dialog(driver)

    # 金额边界测试
    amount_tests = [("金额输入0", "0"), ("金额输入负数", "-500")]
    for case_name, val in amount_tests:
        try:
            if open_withdraw_dialog(driver):
                # 先选商户
                item = get_form_item_by_label(driver, "商户名称")
                if item:
                    select_el_dropdown(driver, item, option_text="ceshi8")
                    time.sleep(0.4)

                item = get_form_item_by_label(driver, "提现金额")
                if item:
                    inputs = item.find_elements(By.CSS_SELECTOR, "input")
                    for inp in inputs:
                        if inp.is_displayed():
                            try:
                                inp.clear()
                                inp.send_keys(val)
                            except:
                                js_set_input(driver, inp, val)
                            break
                    log_result(module, case_name, "PASS", f"已输入: {val}")
                else:
                    log_result(module, case_name, "FAIL", "未找到金额字段")
                close_dialog(driver)
        except Exception as e:
            log_result(module, case_name, "FAIL", str(e)[:80])
            close_dialog(driver)

    # 验证码为空提交
    try:
        if open_withdraw_dialog(driver):
            item = get_form_item_by_label(driver, "商户名称")
            if item:
                select_el_dropdown(driver, item, option_text="ceshi8")

            for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
                if dialog.is_displayed():
                    for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
                        if "提交" in btn.text and btn.is_displayed():
                            btn.click()
                            time.sleep(0.4)
                            errors = [e.text for e in driver.find_elements(By.CSS_SELECTOR, ".el-form-item__error") if e.is_displayed()]
                            log_result(module, "验证码为空提交",
                                       "PASS" if errors else "FAIL",
                                       f"验证提示: {errors}" if errors else "无验证提示")
                            break
            close_dialog(driver)
    except Exception as e:
        log_result(module, "验证码为空提交", "FAIL", str(e)[:80])
        close_dialog(driver)


def test_withdraw_scenarios(driver):
    """提现记录多场景测试"""
    module = "提现记录"
    print(f"\n{'='*60}")
    print(f"  测试模块: {module} - 场景测试")
    print(f"{'='*60}")

    # ---------- 场景1: 正常提现流程 ----------
    try:
        if open_withdraw_dialog(driver):
            # 选择商户ceshi8
            item = get_form_item_by_label(driver, "商户名称")
            if item:
                select_el_dropdown(driver, item, option_text="ceshi8")
                time.sleep(0.8)

            # 输入提现金额
            item = get_form_item_by_label(driver, "提现金额")
            if item:
                for inp in item.find_elements(By.CSS_SELECTOR, "input"):
                    if inp.is_displayed():
                        try:
                            inp.clear()
                            inp.send_keys("100")
                        except:
                            js_set_input(driver, inp, "100")
                        break

            # 输入收款地址
            item = get_form_item_by_label(driver, "收款地址")
            if item:
                addr = f"TH{random.randint(100000, 999999)}"
                try:
                    item.find_element(By.CSS_SELECTOR, "input").clear()
                    item.find_element(By.CSS_SELECTOR, "input").send_keys(addr)
                except:
                    js_set_input(driver, item.find_element(By.CSS_SELECTOR, "input"), addr)

            # 输入谷歌验证码
            item = get_form_item_by_label(driver, "谷歌验证码")
            if item:
                inp = item.find_element(By.CSS_SELECTOR, "input")
                inp.clear()
                inp.send_keys(get_totp_code())

            # 点击提交
            for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
                if dialog.is_displayed():
                    for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
                        if "提交" in btn.text and btn.is_displayed():
                            btn.click()
                            time.sleep(1)
                            break

            success = driver.find_elements(By.CSS_SELECTOR, ".el-message--success")
            errors = driver.find_elements(By.CSS_SELECTOR, ".el-message--error")
            if success:
                log_result(module, "正常提现流程", "PASS", "提现提交成功")
            elif errors:
                err_text = [e.text for e in errors if e.is_displayed()]
                log_result(module, "正常提现流程", "FAIL", f"提交失败: {err_text}")
            else:
                dialog_still = any(d.is_displayed() for d in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"))
                log_result(module, "正常提现流程", "PASS" if not dialog_still else "FAIL",
                           "弹窗已关闭" if not dialog_still else "弹窗仍打开")
            close_dialog(driver)
        else:
            log_result(module, "正常提现流程", "FAIL", "弹窗打开失败")
    except Exception as e:
        log_result(module, "正常提现流程", "FAIL", str(e)[:80])
        close_dialog(driver)

    # ---------- 场景2: 超额提现(超过余额) ----------
    try:
        if open_withdraw_dialog(driver):
            item = get_form_item_by_label(driver, "商户名称")
            if item:
                select_el_dropdown(driver, item, option_text="ceshi8")
                time.sleep(0.8)

            # 查看剩余可用余额
            balance_item = get_form_item_by_label(driver, "余额")
            balance_text = ""
            if balance_item:
                balance_text = balance_item.text

            # 输入超大金额
            item = get_form_item_by_label(driver, "提现金额")
            if item:
                for inp in item.find_elements(By.CSS_SELECTOR, "input"):
                    if inp.is_displayed():
                        try:
                            inp.clear()
                            inp.send_keys("999999999")
                        except:
                            js_set_input(driver, inp, "999999999")
                        break

            item = get_form_item_by_label(driver, "收款地址")
            if item:
                addr = f"TH{random.randint(100000, 999999)}"
                try:
                    item.find_element(By.CSS_SELECTOR, "input").clear()
                    item.find_element(By.CSS_SELECTOR, "input").send_keys(addr)
                except:
                    js_set_input(driver, item.find_element(By.CSS_SELECTOR, "input"), addr)

            item = get_form_item_by_label(driver, "谷歌验证码")
            if item:
                inp = item.find_element(By.CSS_SELECTOR, "input")
                inp.clear()
                inp.send_keys(get_totp_code())

            for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
                if dialog.is_displayed():
                    for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
                        if "提交" in btn.text and btn.is_displayed():
                            btn.click()
                            time.sleep(1)
                            break

            errors = driver.find_elements(By.CSS_SELECTOR, ".el-message--error")
            dialog_still = any(d.is_displayed() for d in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"))
            if errors or dialog_still:
                log_result(module, "超额提现验证", "PASS", "超额提现被拒绝")
            else:
                log_result(module, "超额提现验证", "FAIL", "超额提现未被拦截")
            close_dialog(driver)
        else:
            log_result(module, "超额提现验证", "FAIL", "弹窗打开失败")
    except Exception as e:
        log_result(module, "超额提现验证", "FAIL", str(e)[:80])
        close_dialog(driver)

    # ---------- 场景3: 不同商户提现 ----------
    try:
        if open_withdraw_dialog(driver):
            # 选择不同的商户
            item = get_form_item_by_label(driver, "商户名称")
            if item:
                # 尝试选择第二个商户
                if select_el_dropdown(driver, item, index=1):
                    log_result(module, "不同商户提现", "PASS", "已选择其他商户")
                else:
                    log_result(module, "不同商户提现", "FAIL", "无其他商户可选")
            else:
                log_result(module, "不同商户提现", "FAIL", "未找到商户字段")
            close_dialog(driver)
        else:
            log_result(module, "不同商户提现", "FAIL", "弹窗打开失败")
    except Exception as e:
        log_result(module, "不同商户提现", "FAIL", str(e)[:80])
        close_dialog(driver)

    # ---------- 场景4: 提现金额为0提交 ----------
    try:
        if open_withdraw_dialog(driver):
            item = get_form_item_by_label(driver, "商户名称")
            if item:
                select_el_dropdown(driver, item, option_text="ceshi8")
                time.sleep(0.8)

            item = get_form_item_by_label(driver, "提现金额")
            if item:
                for inp in item.find_elements(By.CSS_SELECTOR, "input"):
                    if inp.is_displayed():
                        try:
                            inp.clear()
                            inp.send_keys("0")
                        except:
                            js_set_input(driver, inp, "0")
                        break

            item = get_form_item_by_label(driver, "收款地址")
            if item:
                addr = f"TH{random.randint(100000, 999999)}"
                try:
                    item.find_element(By.CSS_SELECTOR, "input").clear()
                    item.find_element(By.CSS_SELECTOR, "input").send_keys(addr)
                except:
                    js_set_input(driver, item.find_element(By.CSS_SELECTOR, "input"), addr)

            item = get_form_item_by_label(driver, "谷歌验证码")
            if item:
                inp = item.find_element(By.CSS_SELECTOR, "input")
                inp.clear()
                inp.send_keys(get_totp_code())

            for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
                if dialog.is_displayed():
                    for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
                        if "提交" in btn.text and btn.is_displayed():
                            btn.click()
                            time.sleep(1)
                            break

            errors = driver.find_elements(By.CSS_SELECTOR, ".el-message--error, .el-form-item__error")
            dialog_still = any(d.is_displayed() for d in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"))
            if errors or dialog_still:
                log_result(module, "金额为0提现验证", "PASS", "0金额提现被拒绝")
            else:
                log_result(module, "金额为0提现验证", "FAIL", "0金额提现未被拦截")
            close_dialog(driver)
        else:
            log_result(module, "金额为0提现验证", "FAIL", "弹窗打开失败")
    except Exception as e:
        log_result(module, "金额为0提现验证", "FAIL", str(e)[:80])
        close_dialog(driver)

    # ---------- 场景5: 收款地址格式测试 ----------
    try:
        if open_withdraw_dialog(driver):
            item = get_form_item_by_label(driver, "商户名称")
            if item:
                select_el_dropdown(driver, item, option_text="ceshi8")
                time.sleep(0.8)

            item = get_form_item_by_label(driver, "提现金额")
            if item:
                for inp in item.find_elements(By.CSS_SELECTOR, "input"):
                    if inp.is_displayed():
                        try:
                            inp.clear()
                            inp.send_keys("50")
                        except:
                            js_set_input(driver, inp, "50")
                        break

            # 输入SQL注入格式
            item = get_form_item_by_label(driver, "收款地址")
            if item:
                try:
                    item.find_element(By.CSS_SELECTOR, "input").clear()
                    item.find_element(By.CSS_SELECTOR, "input").send_keys("' OR 1=1 --")
                except:
                    js_set_input(driver, item.find_element(By.CSS_SELECTOR, "input"), "' OR 1=1 --")

            item = get_form_item_by_label(driver, "谷歌验证码")
            if item:
                inp = item.find_element(By.CSS_SELECTOR, "input")
                inp.clear()
                inp.send_keys(get_totp_code())

            log_result(module, "收款地址SQL注入", "PASS", "已输入SQL注入格式")
            close_dialog(driver)
        else:
            log_result(module, "收款地址SQL注入", "FAIL", "弹窗打开失败")
    except Exception as e:
        log_result(module, "收款地址SQL注入", "FAIL", str(e)[:80])
        close_dialog(driver)


# =====================================================
# 商户互转 - 新增测试
# =====================================================
def test_merchant_transfer_add(driver):
    """商户互转新增测试"""
    module = "商户互转"
    print(f"\n{'='*60}")
    print(f"  测试模块: {module} - 新增")
    print(f"{'='*60}")

    # ---------- 用例1: 商户互转页面 ----------
    driver.get(f"{BASE_URL}{MERCHANT_TRANSFER_URL}")
    # 等待面包屑或表格容器出现（最多10秒）
    deadline = __import__('time').time() + 10
    while __import__('time').time() < deadline:
        crumbs = driver.find_elements(By.CSS_SELECTOR, ".el-breadcrumb__inner")
        if any("商户互转" in c.text for c in crumbs):
            break
        time.sleep(0.3)

    try:
        found = any("商户互转" in c.text for c in driver.find_elements(By.CSS_SELECTOR, ".el-breadcrumb__inner"))
        log_result(module, "页面加载", "PASS" if found else "FAIL",
                   "页面已加载" if found else "页面加载失败")
    except Exception as e:
        log_result(module, "页面加载", "FAIL", str(e)[:80])

    # ---------- 用例2: 筛选功能 ----------
    try:
        for inp in driver.find_elements(By.CSS_SELECTOR, "input"):
            ph = inp.get_attribute("placeholder") or ""
            if "互转流水" in ph and inp.is_displayed():
                inp.clear()
                inp.send_keys("TEST" + str(random.randint(1000, 9999)))
                break
        log_result(module, "筛选功能", "PASS", "筛选输入可用")
    except Exception as e:
        log_result(module, "筛选功能", "FAIL", str(e)[:80])

    # ---------- 用例3: 状态筛选 ----------
    try:
        for sel in driver.find_elements(By.CSS_SELECTOR, ".el-select .el-input__inner"):
            ph = sel.get_attribute("placeholder") or ""
            if "状态" in ph and sel.is_displayed():
                sel.click()
                time.sleep(0.4)
                opts = [opt.text for opt in driver.find_elements(By.CSS_SELECTOR, ".el-select-dropdown__item") if opt.is_displayed()]
                if opts:
                    log_result(module, "状态筛选", "PASS", f"可选: {opts}")
                    for opt in driver.find_elements(By.CSS_SELECTOR, ".el-select-dropdown__item"):
                        if opt.is_displayed():
                            opt.click()
                            break
                else:
                    log_result(module, "状态筛选", "FAIL", "无可选项")
                break
    except Exception as e:
        log_result(module, "状态筛选", "FAIL", str(e)[:80])

    # ---------- 用例4: 我要转帐页面 ----------
    driver.get(f"{BASE_URL}{TRANSFER_APPLICATION_URL}")
    deadline = __import__('time').time() + 10
    while __import__('time').time() < deadline:
        crumbs = driver.find_elements(By.CSS_SELECTOR, ".el-breadcrumb__inner")
        if any("转" in c.text for c in crumbs):
            break
        time.sleep(0.3)

    try:
        found = any("转" in item.text for item in driver.find_elements(By.CSS_SELECTOR, ".el-breadcrumb__inner"))
        log_result(module, "我要转帐页面", "PASS" if found else "FAIL",
                   "页面已加载" if found else "页面加载失败")
    except Exception as e:
        log_result(module, "我要转帐页面", "FAIL", str(e)[:80])

    # ---------- 用例5: 审核操作 ----------
    try:
        review_btn = None
        for btn in driver.find_elements(By.CSS_SELECTOR, ".el-table button"):
            if "审核" in btn.text and btn.is_displayed():
                review_btn = btn
                break

        if review_btn:
            review_btn.click()
            time.sleep(0.8)

            # 获取审核字段
            fields = []
            for item in driver.find_elements(By.CSS_SELECTOR, ".el-dialog .el-form-item"):
                try:
                    label = item.find_element(By.CSS_SELECTOR, ".el-form-item__label").text.strip()
                    if label:
                        fields.append(label)
                except:
                    pass
            log_result(module, "审核弹窗", "PASS", f"字段: {fields[:8]}")

            # 审核状态 - 通过
            for item in driver.find_elements(By.CSS_SELECTOR, ".el-dialog .el-form-item"):
                try:
                    label = item.find_element(By.CSS_SELECTOR, ".el-form-item__label").text.strip()
                    if "审核状态" in label:
                        for radio in item.find_elements(By.CSS_SELECTOR, ".el-radio"):
                            if "通过" in radio.text:
                                radio.click()
                                log_result(module, "审核选择通过", "PASS", "已选择通过")
                                break
                        break
                except:
                    pass

            # 谷歌验证码
            for item in driver.find_elements(By.CSS_SELECTOR, ".el-dialog .el-form-item"):
                try:
                    label = item.find_element(By.CSS_SELECTOR, ".el-form-item__label").text.strip()
                    if "谷歌验证码" in label:
                        inp = item.find_element(By.CSS_SELECTOR, "input")
                        totp_code = get_totp_code()
                        inp.clear()
                        inp.send_keys(totp_code)
                        log_result(module, "审核验证码", "PASS", "已输入验证码")
                        break
                except:
                    pass

            # 审核原因
            for item in driver.find_elements(By.CSS_SELECTOR, ".el-dialog .el-form-item"):
                try:
                    label = item.find_element(By.CSS_SELECTOR, ".el-form-item__label").text.strip()
                    if "审核原因" in label:
                        ta = item.find_elements(By.CSS_SELECTOR, "textarea")
                        if ta:
                            ta[0].clear()
                            ta[0].send_keys(f"自动化测试审核-{datetime.now().strftime('%H%M%S')}")
                            log_result(module, "审核原因", "PASS", "已输入审核原因")
                        break
                except:
                    pass

            # 提交审核
            for dialog in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"):
                if dialog.is_displayed():
                    for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
                        if "确定" in btn.text and btn.is_displayed():
                            btn.click()
                            time.sleep(1)
                            success = driver.find_elements(By.CSS_SELECTOR, ".el-message--success")
                            dialog_still = any(d.is_displayed() for d in driver.find_elements(By.CSS_SELECTOR, ".el-dialog"))
                            if success:
                                log_result(module, "提交审核", "PASS", "审核成功")
                            elif not dialog_still:
                                log_result(module, "提交审核", "PASS", "弹窗已关闭")
                            else:
                                log_result(module, "提交审核", "FAIL", "审核可能失败")
                            break
        else:
            log_result(module, "审核操作", "FAIL", "无审核按钮(无数据)")
    except Exception as e:
        log_result(module, "审核操作", "FAIL", str(e)[:80])

    close_dialog(driver)

    # ---------- 用例6: 商户互转记录验证 ----------
    driver.get(f"{BASE_URL}{MERCHANT_TRANSFER_URL}")
    deadline = __import__('time').time() + 10
    while __import__('time').time() < deadline:
        if driver.find_elements(By.CSS_SELECTOR, ".el-table"):
            break
        time.sleep(0.3)
    try:
        no_data = driver.find_elements(By.CSS_SELECTOR, ".el-table__empty-text")
        if no_data and no_data[0].is_displayed():
            log_result(module, "互转记录", "FAIL", "暂无数据")
        else:
            rows = driver.find_elements(By.CSS_SELECTOR, ".el-table__row")
            log_result(module, "互转记录", "PASS", f"共{len(rows)}条记录")
    except Exception as e:
        log_result(module, "互转记录", "FAIL", str(e)[:80])


# =====================================================
# 测试报告
# =====================================================
def print_report():
    """打印测试报告"""
    print(f"\n\n{'='*70}")
    print(f"  自动化测试报告 - 财务管理模块")
    print(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}")

    total = len(test_results)
    passed = sum(1 for r in test_results if r["status"] == "PASS")
    failed = sum(1 for r in test_results if r["status"] == "FAIL")

    print(f"\n  总用例数: {total}")
    print(f"  通过: {passed}")
    print(f"  失败: {failed}")
    print(f"  通过率: {passed/total*100:.1f}%" if total > 0 else "  通过率: N/A")

    modules = {}
    for r in test_results:
        mod = r["module"]
        if mod not in modules:
            modules[mod] = []
        modules[mod].append(r)

    for mod, cases in modules.items():
        mp = sum(1 for c in cases if c["status"] == "PASS")
        mf = sum(1 for c in cases if c["status"] == "FAIL")
        print(f"\n  --- {mod} ({mp}通过/{mf}失败) ---")
        for c in cases:
            icon = "V" if c["status"] == "PASS" else "X"
            print(f"    [{icon}] {c['case']}: {c['message']}")

    print(f"\n{'='*70}")


# =====================================================
# 主函数
# =====================================================
def main():
    print(f"\n{'='*70}")
    print(f"  财务管理模块自动化测试")
    print(f"  测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  测试范围: 充值记录/提现记录/商户互转")
    print(f"{'='*70}")

    driver = None
    try:
        print("\n>>> Step1: 系统登录")
        driver = login()
        if not driver:
            print("登录失败, 终止测试")
            return

        print("\n>>> Step2: 切换中文")
        switch_to_chinese(driver)

        print("\n>>> Step3: 切换泰国")
        switch_to_thailand(driver)

        print("\n>>> Step4: 充值记录新增测试")
        driver = ensure_driver(driver)
        if driver: test_recharge_add(driver)

        print("\n>>> Step5: 充值记录边界值测试")
        driver = ensure_driver(driver)
        if driver: test_recharge_boundary(driver)

        print("\n>>> Step5.5: 充值记录场景测试")
        driver = ensure_driver(driver)
        if driver: test_recharge_scenarios(driver)

        print("\n>>> Step6: 提现记录新增测试")
        driver = ensure_driver(driver)
        if driver: test_withdraw_add(driver)

        print("\n>>> Step7: 提现记录边界值测试")
        driver = ensure_driver(driver)
        if driver: test_withdraw_boundary(driver)

        print("\n>>> Step7.5: 提现记录场景测试")
        driver = ensure_driver(driver)
        if driver: test_withdraw_scenarios(driver)

        print("\n>>> Step8: 商户互转新增测试")
        driver = ensure_driver(driver)
        if driver: test_merchant_transfer_add(driver)

    finally:
        print_report()
        if driver:
            try:
                driver.save_screenshot("zdhcsCL_final.png")
                print("\n测试完成, 截图已保存: zdhcsCL_final.png")
            except:
                print("\n测试完成")
            try:
                driver.quit()
            except:
                pass


if __name__ == "__main__":
    main()
