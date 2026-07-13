# 操作日志（Ops Log）

记录对本仓库有结构性影响的变更：删除模块、配置项废弃、行为变更。  
**现行说明**以 `README.md` 与其它 `docs/*.md` 为准；本文件只归档「做过什么」，不复述当前用法。

书写约定：

- 新条目置顶（时间倒序）
- 日期使用 `YYYY-MM-DD`
- 只记事实与影响范围，不写长篇设计

---

## 2026-07-12

### src layout

| 项 | 说明 |
| --- | --- |
| 代码 | `src/grok_register/` |
| 入口 | 根目录 `register_cli.py` 薄封装；实现 `grok_register.cli` |
| 路径 | `paths.PROJECT_ROOT` 统一解析仓库根 |


### 单包 grok_register + 根目录 exports

| 项 | 说明 |
| --- | --- |
| 代码 | 全部业务包收入 `grok_register/`（browser/proxy/mail/export/core/engine） |
| 导出 | 根目录 `exports/{cpa,sub2api}/` |
| 账号 | 根目录 `accounts/` |
| 日志 | 根目录 `logs/` |
| 删除 | 顶层 `data/`、分散领域包、grok_register 名 |


### registration 并入 grok_register

| 项 | 说明 |
| --- | --- |
| 合并 | `registration/*` → `grok_register/`（engine/transport/types…） |
| 删除 | 根目录全部兼容 shim；仅保留 `register_cli.py` 入口 |

### 根目录 shim 清理


| 项 | 说明 |
| --- | --- |
| 删除 | 根目录 `proxy_pool.py` 等兼容 shim、`cpa_xai/` 别名包、`grok_register_ttk.py` |
| 入口 | 仅 `register_cli.py`；或 `python -m grok_register` |
| import | 统一 `proxy.pool` / `mail.pool` / `export.cpa_*` / `grok_register.core` |

### 完整目录重构（包分层 + data/）

| 项 | 说明 |
| --- | --- |
| 包 | `grok_register/` 核心；`browser/` `proxy/` `mail/` `export/` 领域包 |
| 实现 | 原 `grok_register_ttk` 主体迁入 `grok_register/core.py`；根文件为兼容 shim |
| 数据 | 运行时统一 `data/`（accounts、exports、logs、snapshots、capture） |
| 扩展 | `browser/extensions/turnstilePatch` |
| 兼容 | 根目录旧 import 路径仍可用（module alias shim） |

### 移除 GUI + 根目录整理

| 项 | 说明 |
| --- | --- |
| 删除 | Tk GUI（`GrokRegisterGUI`、主题/控件、`launch_gui.ps1`） |
| 入口 | 仅 CLI：`register_cli.py`；兼容 `grok_register_ttk.py cli` |
| 移动 | `cf_mail_debug.py`、`optimization_checks.py`、`verify_config_safe.*` → `scripts/` |
| 保留 | 浏览器注册 / 邮箱 / CPA / Sub2API 业务路径不变 |

### 导出目录：`exports/{cpa,sub2api}`

| 项 | 说明 |
| --- | --- |
| 新结构 | `exports/cpa/`（CPA）、`exports/sub2api/`（Sub2API） |
| 配置 | `export_root`、`cpa_auth_dir`、`sub2api_export_dir`、`sub2api_combined_file` 默认对齐 |
| 兼容 | 旧 `cpa_auths/`、`sub2api_exports/` 仍可手动指定；建议迁入新路径 |

### 有界面 CF「验证失败/故障排除」

| 项 | 说明 |
| --- | --- |
| 根因 | macOS 强行 Windows UA；headed 加载 `turnstilePatch`；对失败 widget 连点/reset |
| 改动 | `user_agent` 默认空=原生 Chrome UA；跨平台伪装 UA 自动忽略 |
| 改动 | `turnstile_extension=auto`：headed 不加载扩展 |
| 改动 | headed 先被动等 Turnstile，少点、不 reset；识别失败 UI 后停止连点 |

### 移除 Chrome 黄条：`AutomationControlled` CLI

| 项 | 说明 |
| --- | --- |
| 现象 | 有界面启动出现「不受支持的命令行标记: --disable-blink-features=AutomationControlled」，CF 明显变差 |
| 改动 | **所有模式**不再传该 CLI；headed 仅用最小启动参数（对齐 gpt 有界面策略） |
| 改动 | 去掉无效的 `--excludeSwitches` / `--useAutomationExtension` 命令行写法 |
| 保留 | 后台模式 CDP stealth；`turnstilePatch` 仅 pure/offscreen（auto） |

### Turnstile 协助加固（资料页 CF 验证失败）

| 项 | 说明 |
| --- | --- |
| 改动 | `getTurnstileToken`：CDP 坐标点击 + shadow checkbox + 窗口前置 |
| 改动 | `fill_profile_and_submit`：更早协助、后台超时拉长、CF 超时明确报错 |
| 改动 | `apply_page_stealth`：`Page.addScriptToEvaluateOnNewDocument` |
| 改动 | `turnstilePatch/content.js`：iframe 内 checkbox、screenXY、更长轮询 |
| 限制 | 无打码服务时，纯无头 / 强风控 IP 仍可能失败；优先 `--headed` 或代理 |

### 后台浏览器：`headless_mode`（offscreen / pure）

| 项 | 说明 |
| --- | --- |
| 新增 | `resolve_headless_mode`、`headless_mode` 配置、CLI `--headless-mode` |
| `auto` | 桌面默认 **offscreen**（真 Chrome 离焦/角落窗口，易过 CF 首页） |
| `pure` | `--headless=new` + stealth；直连仍易 Attention Required |
| Turnstile | 后台模式不 reset widget；加长轮询；仍可能失败（无打码服务时） |
| 文档 | `docs/registration.md` 补充用法 |

---

## 2026-07-11

### 移除：协议注册（HTTP-first）

| 项 | 说明 |
| --- | --- |
| 删除 | 包 `protocol_register/`（含 ProtocolTransport、api、discovery、grpcweb、fallback 等） |
| 删除 | 设计文档 `docs/design-protocol-first-registration.md` |
| 删除 | 测试 `tests/test_protocol_engine_fallback.py`、`test_discovery_tools.py`、`test_grpcweb_codec.py`；`tests/fixtures/protocol/` |
| 配置废弃 | `register_mode`、`protocol_enabled`、全部 `protocol_*` 键 |
| CLI 废弃 | `--register-mode` |
| 替代 | 包 `grok_register/`：仅 Chromium DOM 注册（`RegistrationEngine` + `BrowserTransport`） |
| 入口 | `register_cli.py` / GUI / `grok_register_ttk` CLI 均只走浏览器引擎 |
| 不变 | CPA mint（`cpa_xai/`）、Sub2API 导出、邮箱 provider、代理池 |

### 重构：注册与 mint 边界

| 项 | 说明 |
| --- | --- |
| 注册 | 产出 `email----password----sso`（及 cookie） |
| Mint | 注册成功后的 OIDC device 流程；可热浏览器或 `--remint-missing` 补 mint |
| Cookie 注入 | 浏览器只导航 `accounts.x.ai` 相关 URL；OIDC HTTP 走 `auth.x.ai` 的 device/token API，不打开 `https://auth.x.ai/` 根路径 |

### 其它同期行为修正（仍在现行代码中）

| 项 | 说明 |
| --- | --- |
| CPA HTTP 代理 | 直连时用 `DirectProxyHandler`，不继承 macOS 系统代理 |
| accounts 行解析 | `parse_account_line` 按 JWT（`eyJ`）锚定，避免密码中 `-` 拆坏 SSO |
| TempMail 轮询 | 间隔强制 ≥3s |
| 邮箱预创建池 | `mail_pool.py` + `mail_pool_size` |
| 阶段耗时 | 成功日志输出 `timing: init=… email=…` |

---

## 模板（新条目复制）

```markdown
## YYYY-MM-DD

### 标题

| 项 | 说明 |
| --- | --- |
| 变更 | … |
| 影响 | … |
| 文档 | 已同步 / 无需同步 |
```
