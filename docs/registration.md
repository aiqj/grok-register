# 注册架构

当前仅支持 **Chromium 浏览器注册**（DOM + Turnstile）。

```text
register_cli / python -m grok_register
    → grok_register.RegistrationEngine
        → BrowserTransport
            → grok_register.core（open / fill / wait SSO）
    → 成功后可选 CPA mint（cpa_xai）
         ├─ 默认优先：SSO→Build（HTTP 自动 device 批准）
         └─ 失败/关闭时：浏览器设备码（CPA 经典）
```

| 路径 / 包 | 职责 |
| --- | --- |
| `grok_register/` | 注册引擎、浏览器 transport、成功副作用配置 |
| `grok_register/core.py` | 页面操作、临时邮箱、浏览器生命周期（无 GUI） |
| `register_cli.py` | 批量 CLI、线程、CPA mint 队列、`--remint-missing` |
| `cpa_xai/` | OIDC mint：`sso_to_build` + 浏览器设备码 + CPA 写出 |
| `cpa_export.py` / `cpa_to_sub2api.py` | mint 编排与 Sub2API 转换 |

### 注册后 CPA mint 两条路径

仍属 **Grok Build / device-code OAuth**，不是取消设备码。

| 优先级 | 名称 | 默认 | 说明 |
| --- | --- | --- | --- |
| 1 | **SSO→Build** | `cpa_mint_prefer_sso_build=true` | 用注册得到的 `sso` cookie，HTTP 自动 device/code→approve→token（对齐 Sub2API ConvertSSOToBuild）。通常**无**浏览器设备码页。 |
| 2 | **浏览器设备码** | 回退 | SSO 缺失/失败，或配置关闭优先时；打开 Chromium 确认 user_code。 |

强制只用浏览器设备码：

```json
"cpa_mint_prefer_sso_build": false
```

日志：

```text
[cpa] mint try SSO→Build ...
[cpa] mint SSO→Build ok scope=...
# 或
[cpa] mint SSO→Build failed, fallback browser device: ...
```

细节见 [export-cpa-and-sub2api.md §3.1](export-cpa-and-sub2api.md)；403 排查见 [grok-403-investigation.md](grok-403-investigation.md)。

## 后台 / 无交互浏览器

| `headless_mode` | 含义 | 与 Cloudflare |
| --- | --- | --- |
| `auto`（推荐） | 桌面用 **offscreen**；Linux 无 DISPLAY 用 **pure** | 桌面成功率远高于 pure |
| `offscreen` | 真实 Chrome 小窗（角落），非 `--headless` | 首页 CF 较好；资料页 Turnstile 靠 CDP 点击协助，仍可能失败 |
| `pure` | `--headless=new` + 隐身参数 | 直连几乎必拦 Attention Required |

资料页 **token 长度一直为 0** 时：优先 `--headed`，或配置 `PROXY_POOL` 后用 `--headless --headless-mode offscreen`。无第三方打码时无法保证 100% 过 Turnstile。

```bash
# 推荐「无交互」批量（默认 auto → 桌面 offscreen）
python register_cli.py --extra 5 --threads 1 --headless

# 强制纯无头（服务器无显示；建议配合代理）
python register_cli.py --extra 5 --threads 1 --headless --headless-mode pure

# 有界面最稳
python register_cli.py --extra 5 --threads 1 --headed
```

环境变量：`HEADLESS=1`、`HEADLESS_MODE=auto|offscreen|pure`。

相关文档：

- [export-cpa-and-sub2api.md](export-cpa-and-sub2api.md) — CPA / Sub2API  
- [batch-speed.md](batch-speed.md) — 批量耗时与提速  
- [ops-log.md](ops-log.md) — 历史变更记录  
