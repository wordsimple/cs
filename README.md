# OTG 支付系统自动化测试研究资料

## 文档导航

本目录包含对OTG支付系统的完整研究和分析。以下文件提供不同粒度的信息:

### 快速开始
- **快速参考.txt** - 最常用的关键信息速查表，5分钟快速上手
- **研究总结.txt** - 详细的研究总结，包含所有关键发现和技术细节

### 完整参考
- **研究报告.md** - 超长的完整研究报告（473行），包含12个章节的深度分析

## 关键信息速查

### 系统URL
```
登录: https://operator1.otgpaytest.com/#/login
基础: https://operator1.otgpaytest.com/#
```

### 测试账户
```
用户名: ceshi
密码: qw123456
TOTP密钥: 6gnkpcdu22be5pahlbvfvffmnxcqzjzv
```

### 表单字段快速查询

| 表单 | 字段 | 类型 | 必填 |
|-----|------|------|------|
| 充值 | 用户编码 | 级联下拉 | 是 |
| 充值 | 商户名称 | 级联下拉 | 是 |
| 充值 | 充值金额 | 数值输入 | 是 |
| 充值 | 收款地址 | 下拉选择 | 是 |
| 充值 | 谷歌验证码 | 文本 | 是 |
| 充值 | 商户凭证 | 特殊 | 否 |
| 充值 | 备注 | 多行文本 | 否 |
| 提现 | 商户名称 | 下拉选择 | 是 |
| 提现 | 提现金额 | 数值输入 | 是 |
| 提现 | 收款地址 | 文本输入 | 是 |
| 提现 | 谷歌验证码 | 文本 | 是 |
| 互转审核 | 审核状态 | 单选框 | 是 |
| 互转审核 | 谷歌验证码 | 文本 | 是 |
| 互转审核 | 审核原因 | 多行文本 | 是 |

## 文件资源

### HTML页面文件
收录完整的页面HTML，用于离线分析和参考:
- `/Users/apple/Documents/ceshi/recharge_add_form.html` - 充值表单
- `/Users/apple/Documents/ceshi/withdrawals_add_form.html` - 提现表单
- `/Users/apple/Documents/ceshi/merchant_transfer_add_form.html` - 商户互转表单
- `/Users/apple/Documents/ceshi/recharge_page.html` - 充值列表
- `/Users/apple/Documents/ceshi/page_after_login_full.html` - 登录后首页

### Python自动化脚本
参考实现，可直接使用或修改:
- `/Users/apple/Documents/ceshi/zdh.py` - 基础登录与菜单点击
- `/Users/apple/Documents/ceshi/zdhcs.py` - 日志完整的登录脚本
- `/Users/apple/Documents/ceshi/zdhcsCL.py` - **完整测试脚本 (推荐参考)**
- `/Users/apple/Documents/ceshi/zdhcsGX.py` - 全模块遍历测试
- `/Users/apple/Documents/ceshi/explore_system.py` - 系统探索脚本

### 截图资源
可视化参考:
- `01_login_page.png` - 登录页面
- `01_after_login.png` - 登录后首页
- `02_finance_menu.png` - 财务菜单
- `03_recharge_page.png` - 充值列表
- `04_recharge_add_form.png` - 充值表单
- 其他各版本exploration截图

## 核心技术要点

### 1. TOTP验证码生成
```python
import pyotp
totp = pyotp.TOTP('6gnkpcdu22be5pahlbvfvffmnxcqzjzv')
code = totp.now()  # 6位验证码
```

### 2. 表单字段定位
```python
# 通用方法：按标签文本定位
get_form_item_by_label(driver, "用户编码")

# 下拉框选项定位
XPath: //li[contains(@class, 'el-select-dropdown__item')]
```

### 3. 级联表单处理
```python
# 选择第一个字段
select_el_dropdown(driver, item, option_text="299")
time.sleep(2-3)  # 等待级联加载

# 然后选择第二个字段
select_el_dropdown(driver, item, index=0)
```

### 4. 语言切换 (切到中文)
```python
# 点击语言按钮
button = driver.find_element(By.CSS_SELECTOR, ".international-icon")
driver.execute_script("arguments[0].click()", button)
time.sleep(0.5)

# 点击中文选项
chinese = driver.find_element(By.XPATH, "//li[contains(@class, 'el-dropdown-menu__item') and contains(., '中文')]")
driver.execute_script("arguments[0].click()", chinese)
time.sleep(2)
```

### 5. 泰国选择 (切到泰铢)
```python
# 点击货币下拉
currency = driver.find_element(By.CSS_SELECTOR, ".currency-select input")
driver.execute_script("arguments[0].click()", currency)
time.sleep(0.5)

# 点击泰国/泰铢选项
thailand = driver.find_element(By.XPATH, "//li[contains(@class, 'el-select-dropdown__item') and (contains(., 'Thailand') or contains(., '泰铢'))]")
driver.execute_script("arguments[0].click()", thailand)
time.sleep(2)
```

## 系统架构特点

- **框架**: Vue.js + Element UI
- **路由**: Hash路由 (#/路径)
- **认证**: 3层 (用户名 + 密码 + TOTP)
- **表单**: 级联关系，异步加载
- **国际化**: 支持中文、泰国(泰铢)

## 关键CSS类和ID

```
主容器:          #OtgPay
侧边栏:          .sidebar-container
菜单链接:        a[href^='#/']

对话框:          .el-dialog
表单:            .el-form
表单项:          .el-form-item
标签:            .el-form-item__label
输入框:          .el-input > input
下拉框:          .el-select
下拉选项:        .el-select-dropdown__item
单选框:          .el-radio
按钮:            .el-button
成功提示:        .el-message--success
错误提示:        .el-form-item__error

语言按钮:        .international-icon
货币选择:        .currency-select
```

## 常见问题解决

### Q: 下拉框无法点击
**A:** 使用JS点击
```python
driver.execute_script("arguments[0].click()", element)
```

### Q: 级联字段加载超时
**A:** 增加等待时间
```python
time.sleep(3)  # 可能需要等待3秒以上
```

### Q: 输入框值无法改变
**A:** 使用JS设置值
```python
driver.execute_script("""
    arguments[0].value = arguments[1];
    arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
""", element, value)
```

### Q: TOTP验证码错误
**A:** 确保密钥正确，使用pyotp库生成
```python
import pyotp
totp = pyotp.TOTP('6gnkpcdu22be5pahlbvfvffmnxcqzjzv')
print(totp.now())  # 验证生成的码
```

## 推荐阅读顺序

1. **第一次接触**: 先读 `快速参考.txt` (3分钟)
2. **深入理解**: 再读 `研究总结.txt` (15分钟)
3. **完整参考**: 需要详细时查看 `研究报告.md` (30分钟)
4. **代码参考**: 查看 `zdhcsCL.py` 了解实现细节

## 相关技术资源

- **Selenium**: Web自动化框架
- **pyotp**: TOTP验证码生成库
- **Element UI**: Vue前端组件库
- **RFC 6238**: TOTP标准协议

---

最后更新时间: 2026-05-22
研究范围: 登录、充值、提现、商户互转、语言/国家切换
