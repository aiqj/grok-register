# CPA 与 Sub2API 导出说明

本文说明注册成功后两种 OAuth 凭证格式的导出流程、文件结构、字段映射与配置项。

若导入后测模型出现 **403 permission-denied**，请先读：

- **[grok-403-investigation.md](grok-403-investigation.md)** — 问题现象、已做尝试、源码对照、后续方向

相关代码：

| 模块 | 路径 |
| --- | --- |
| CLI 入口 / mint 调度 | `register_cli.py` |
| 导出编排 | `cpa_export.py` |
| OIDC mint + 写 CPA | `cpa_xai/mint.py`、`schema.py`、`writer.py`、`browser_confirm.py`、`oauth_device.py` |
| CPA → Sub2API 转换 | `cpa_to_sub2api.py` |

---

## 1. 总览

两种格式**不是并行独立注册**，而是同一条 mint 流水线的两个产物：

1. **先 mint OIDC**，写出 **CPA / CLIProxyAPI** 兼容文件  
2. **仅当 CPA 成功**，再读取该文件，转换并写出 **Sub2API** 导入包  

```text
注册成功（默认按「批次」分子目录）
  └─ exports/
        YYYYMMDD_HHMMSS[_name]/           ← 每次 CLI 批量跑独立一子目录
          ├─ accounts.txt                 email----password----sso（本批）
          ├─ meta.json
          ├─ cpa/
          │    └─ xai-<email>.json        ← 格式 A：CPA
          └─ sub2api/
               ├─ sub2api-xai-....json
               └─ sub2api-accounts.json

可选：export_batch_also_global_accounts=true 时，额外追加到 accounts/accounts_cli.txt
关闭批次：--no-batch 或 export_batch_enabled=false → 扁平 exports/cpa、exports/sub2api
```

| 产物 | 是否依赖 mint | 用途 |
| --- | --- | --- |
| `accounts_*.txt` | 否 | 账号/密码/SSO 文本备份 |
| CPA `xai-*.json` | **是** | CLIProxyAPI / 兼容 xAI OAuth 热加载 |
| Sub2API JSON | **是**（且依赖 CPA 文件已写出） | Sub2API 管理端数据导入（`platform=grok`） |

开关：

| 配置项 | 默认 | 作用 |
| --- | --- | --- |
| `cpa_export_enabled` | `true` | 关闭则整条 mint + 两种导出都不跑 |
| `sub2api_export_enabled` | `true` | 关闭则只写 CPA，不写 Sub2API |

---

## 2. 调用链

### 2.1 CLI 注册成功后

文件：`register_cli.py`

1. 写入 `accounts_file`：`email----password----sso`  
2. 收集 cookies / SSO，尽量保留注册浏览器 tab  
3. 默认 `cpa_mint_prefer_warm_browser=true`：在**回收浏览器之前**调用 `_run_mint_job(..., page=page)`  
4. 再 `prepare_browser_for_next_account` 清理/复用浏览器  

### 2.2 编排：`cpa_export.export_cpa_xai_for_account`

1. 解析代理、headless、是否强制独立浏览器  
2. 若 `cpa_mint_prefer_warm_browser` 且传入了 `page` → `force_standalone=false`（热浏览器 mint）  
3. 准备 cookies（含 SSO 多域名克隆）  
4. 调用 `cpa_xai.mint_and_export(...)`  
5. 成功且存在 `path` 时：`cpa_to_sub2api.export_after_cpa_result(...)`  
6. 若 `cpa_cloud_upload_enabled`：`upload_cpa_auth_file_to_cloud` → 线上 CPA `POST /v0/management/auth-files`  


### 2.3 时序图

```text
register_cli._run_mint_job
        │
        ▼
cpa_export.export_cpa_xai_for_account
        │
        ├─► cpa_xai.mint_and_export
        │         ├─ request_device_code（auth.x.ai）
        │         ├─ approve_device_code（浏览器）
        │         ├─ poll_device_token
        │         ├─ build_cpa_xai_auth
        │         └─ write_cpa_xai_auth  →  exports/cpa/xai-*.json
        │
        └─► (仅 result.ok) cpa_to_sub2api.export_after_cpa_result
                  ├─ convert_cpa_file   →  sub2api-xai-*.json
                  └─ rebuild_combined  →  sub2api-accounts.json
```

---

## 3. 格式 A：CPA（CLIProxyAPI xAI auth）

### 3.1 如何拿到 token（两条 mint 路径）

实现：`cpa_xai/mint.py` 编排；`sso_to_build.py` / `oauth_device.py` + `browser_confirm.py`。

**仍是 Grok Build / OAuth device-code 体系**，只是默认优先用「有 SSO 时自动批准」，不再总是弹出浏览器设备码页。

#### 路径选择（`mint_and_export`）

| 优先级 | 路径 | 配置 | 何时用 | 日志 |
| --- | --- | --- | --- | --- |
| **默认优先** | **A. SSO→Build** | `cpa_mint_prefer_sso_build=true`（默认） | 注册成功后有 `sso` / `sso-rw` cookie | `[cpa] mint try SSO→Build` → `mint SSO→Build ok` |
| **回退** | **B. 浏览器设备码** | SSO 缺失/失败，或 `cpa_mint_prefer_sso_build=false` | 与旧版 CPA 观感一致 | `mint SSO→Build failed, fallback browser device` 或 `mint_source=browser_device` |

结果字段：`mint_source` = `sso_to_build` | `browser_device`（或 `browser_device_retry`）。

强制只走浏览器设备码（跳过 A）：

```json
"cpa_mint_prefer_sso_build": false
```

#### 路径 A：SSO→Build（对齐 Sub2API `ConvertSSOToBuild`）

实现：`cpa_xai/sso_to_build.py`。全程 **HTTP + SSO cookie**，无交互浏览器：

1. 校验 `accounts.x.ai`（带 sso cookie）  
2. `POST auth.x.ai/oauth2/device/code`（scope 含 `conversations:read/write`，见 `SSO_BUILD_SCOPE`）  
3. 打开 verification 页 → `device/verify` → `device/approve`（自动 allow）  
4. `POST …/token` 轮询拿到 access/refresh  

观感：注册后往往**看不到**「填 user_code / 点允许」的设备码浏览器页，但底层仍是 **device-code grant**。

#### 路径 B：浏览器设备码（CPA 经典）

实现：`oauth_device.py` + `browser_confirm.py`。

#### Host 分工（路径 B；路径 A 无 Chromium 确认）

| Host / URL | 谁用 | 说明 |
| --- | --- | --- |
| `POST https://auth.x.ai/oauth2/device/code` | urllib | 申请 device code（A/B 都会） |
| `POST https://auth.x.ai/oauth2/token` | urllib | 轮询换 token（见 §3.5 代理行为） |
| `https://accounts.x.ai/oauth2/device?user_code=…` | Chromium（**仅 B**） | 点「继续」「允许」 |
| `https://accounts.x.ai/` | Chromium / HTTP | cookie 注入或 SSO 校验 |

路径 B 步骤：

1. **设备码** — `POST …/device/code`（client_id 见 `schema.py`；scope 与 SSO 路径对齐，含 conversations）  
2. **浏览器确认** — 打开 device 页；优先热 tab（`cpa_mint_prefer_warm_browser`）；补 mint：`--remint-missing --headed` + SSO cookie  
3. **轮询 token** — grant_type `urn:ietf:params:oauth:grant-type:device_code`  

### 3.2 组包与落盘

| 步骤 | 函数 | 说明 |
| --- | --- | --- |
| 组对象 | `build_cpa_xai_auth` | 对齐 CLIProxyAPI `internal/auth/xai` |
| 写文件 | `write_cpa_xai_auth` | 原子写（临时文件 + `replace`），权限 `0600` |

- **目录**：`config.cpa_auth_dir`（默认 `./exports/cpa`，根目录见 `export_root`）  
- **文件名**：`xai-<sanitize(email)>.json`（`credential_file_name`）  

### 3.3 主要字段（概念）

```json
{
  "type": "xai",
  "auth_kind": "oauth",
  "access_token": "...",
  "refresh_token": "...",
  "id_token": "...",
  "token_type": "Bearer",
  "expires_in": 21600,
  "expired": "2026-07-11T12:00:00Z",
  "last_refresh": "2026-07-11T06:00:00Z",
  "email": "user@example.com",
  "sub": "...",
  "base_url": "https://cli-chat-proxy.grok.com/v1",
  "token_endpoint": "https://auth.x.ai/oauth2/token",
  "redirect_uri": "http://127.0.0.1:56121/callback",
  "disabled": false,
  "headers": {
    "x-grok-client-version": "...",
    "User-Agent": "grok-shell/..."
  }
}
```

要点：

- **`type` 为 `xai`**（CPA 侧类型名）  
- 默认 **`base_url` 为 `https://cli-chat-proxy.grok.com/v1`**（Grok Build 免费/代理路径，**不是** `api.x.ai`）  
- 必须有 **`refresh_token`**，否则 CPA 无法续期  

### 3.4 可选后处理

| 配置 | 行为 |
| --- | --- |
| `cpa_probe_after_write` | 用 token 拉 models，检查是否含 grok-4.5 等 |
| `cpa_probe_chat` | 可选小对话探测 |
| `cpa_copy_to_hotload` | 拷贝到本机 CPA 的 `auth-dir`（`cpa_hotload_dir`） |
| `cpa_cloud_upload_enabled` | mint 成功后 POST 到**线上** CLIProxyAPI Management API |
| mint 失败 | 追加 `exports/cpa/cpa_auth_failed.txt` |

### 3.5 导入线上 CLIProxyAPI（推荐）

本机写出的 `exports/cpa/xai-*.json` 与 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 的 **auth-dir JSON** 格式一致（`type: "xai"`）。导入线上服务有两种方式：

| 方式 | 场景 | 配置 / 命令 |
| --- | --- | --- |
| **A. Management API 远程上传** | CPA 跑在远端服务器 / Docker | `cpa_cloud_upload_*` 或 `--upload-cpa-cloud` |
| **B. 本机 hotload 目录** | 本机同机跑 CPA，`auth-dir` 可写 | `cpa_copy_to_hotload` + `cpa_hotload_dir` |

#### A. Management API（线上）

CLIProxyAPI 文档：`POST /v0/management/auth-files`  
- multipart：`file=@xai-user@example.com.json`  
- 认证：`Authorization: Bearer <plaintext-key>` 或 `X-Management-Key: <plaintext-key>`  
- 成功响应：`{ "status": "ok" }`，凭证立刻进入运行时 auth manager  
- 远端需在 CPA 的 `config.yaml` 开启：

```yaml
remote-management:
  allow-remote: true
  secret-key: "your-plain-management-key"   # 启动时会 bcrypt 写回
# 或环境变量 MANAGEMENT_PASSWORD（内存口令，不写盘，且强制允许远程）
auth-dir: "~/.cli-proxy-api"               # 上传后的 JSON 落盘目录
```

本项目 `config.json`：

```json
{
  "cpa_cloud_upload_enabled": true,
  "cpa_cloud_api_base": "https://your-cpa-host:8317",
  "cpa_cloud_management_key": "your-plain-management-key",
  "cpa_cloud_upload_timeout": 30,
  "cpa_cloud_upload_retries": 3
}
```

| 配置项 | 说明 |
| --- | --- |
| `cpa_cloud_upload_enabled` | `true` 时，每次 mint 成功自动上传该账号 JSON |
| `cpa_cloud_api_base` | CPA 根地址，如 `http://1.2.3.4:8317`；也可写完整 `…/v0/management`（会自动归一化） |
| `cpa_cloud_management_key` | 与 CPA `remote-management.secret-key` **明文**一致（或 `MANAGEMENT_PASSWORD`） |
| `cpa_cloud_upload_timeout` | 单次 HTTP 超时（秒） |
| `cpa_cloud_upload_retries` | 可重试状态码（408/429/5xx）的次数 |
| `cpa_cloud_upload_workers` | **批量上传并行线程**（默认 `8`）；菜单「上传全部」/ `--cpa-upload-all` / `--cpa-upload-latest` 生效。CLI 可用 `--cpa-upload-workers N` 覆盖 |

环境变量（优先于 config，适合不把密钥写进文件）：

| 环境变量 | 对应 |
| --- | --- |
| `CPA_CLOUD_API_BASE` | `cpa_cloud_api_base` |
| `CPA_CLOUD_MANAGEMENT_KEY` / `CLI_PROXY_MANAGEMENT_KEY` / `MANAGEMENT_PASSWORD` | management key |

**注册流水线自动上传**：`cpa_cloud_upload_enabled=true` 后，`export_cpa_xai_for_account` 在写完本地 JSON 后调用 `upload_cpa_auth_file_to_cloud`（CLI mint / remint / 内联导出都会走）。

**交互菜单（推荐）**：不记参数时可直接

```bash
python register_cli.py
# 或
python register_cli.py --menu
```

菜单内可完成：注册、补 mint、**上传到线上平台**（全部 / 仅 CPA / 仅 Sub2API × 最新/全部/指定）、线上列表、模糊删除、全删。

**已有本地文件导入**（不重新 mint）：

```bash
# 只上传最新一批（推荐快捷）
uv run python register_cli.py --cpa-upload-latest
# 等价
uv run python register_cli.py --upload-cpa-cloud --cpa-upload-latest

# 指定某一批目录
uv run python register_cli.py --upload-cpa-cloud \
  --cpa-upload-dir ./exports/20260712_153045

# 只上传某几个文件
uv run python register_cli.py --upload-cpa-cloud \
  --cpa-upload-files \
  ./exports/20260712_153045/cpa/xai-a@x.com.json \
  ./exports/20260712_153045/cpa/xai-b@x.com.json

# 递归上传 exports/ 下所有批次的 xai-*.json
uv run python register_cli.py --upload-cpa-cloud --cpa-upload-all

# 旧扁平目录
uv run python register_cli.py --upload-cpa-cloud --cpa-upload-dir ./exports/cpa

```

#### 列出 / 模糊删除 / 全删线上凭证

前提：已配置 `cpa_cloud_api_base` + `cpa_cloud_management_key`（与上传相同）。  
实现方式：本机 `GET /auth-files` 后匹配，再 `DELETE ?name=`；全删走 `DELETE ?all=true`。

```bash
# 列出线上全部凭证
uv run python register_cli.py --cpa-list

# 模糊删除 —— 默认 dry-run（只预览，不删）
uv run python register_cli.py --cpa-delete "@actionvspot.com"
uv run python register_cli.py --cpa-delete "luby" "xai-bob*"

# 确认后真正删除（必须 --yes）
uv run python register_cli.py --cpa-delete "@actionvspot.com" --yes
uv run python register_cli.py --cpa-delete "xai-*" --yes
uv run python register_cli.py --cpa-delete "re:^xai-.*@foo\\.com\\.json$" --yes

# 快速全删 —— 先预览
uv run python register_cli.py --cpa-delete-all
# 确认清空线上全部 auth JSON
uv run python register_cli.py --cpa-delete-all --yes
```

模糊匹配规则（多个 PATTERN 为 **OR**）：

| 写法 | 含义 | 示例 |
| --- | --- | --- |
| 普通字符串 | 子串，匹配 name/email/id/label 等（默认忽略大小写） | `luby`、`@foo.com` |
| 含 `*` `?` | shell glob | `xai-*`、`*?@bar.com.json` |
| `re:…` | 正则 | `re:^xai-.*@foo\.com\.json$` |

安全与注意：

| 事项 | 说明 |
| --- | --- |
| 默认 dry-run | 不加 `--yes` 只打印匹配列表，**不会**调用删除 |
| `--yes` / `-y` | 真正执行删除；全删会提示 `DELETE ?all=true` |
| 全删范围 | 仅 CPA `auth-dir` 上的 **磁盘 JSON**；runtime-only 内存凭证不受影响 |
| 全删失败回退 | 若服务端不支持 `all=true`，会回退为逐文件 `DELETE ?name=` |
| 不可恢复 | 删除后需重新 `--cpa-upload-*` 导入 |
| 密钥 | 使用 **Management Key**，不是业务 `api-keys` |
| 与本地目录 | 只影响**线上** CPA；本地 `exports/` 不会被删 |

等价 curl：

```bash
# 列表
curl -H "Authorization: Bearer YOUR_MGMT_KEY" \
  "https://your-cpa-host:8317/v0/management/auth-files"

# 精确删一个
curl -X DELETE -H "Authorization: Bearer YOUR_MGMT_KEY" \
  "https://your-cpa-host:8317/v0/management/auth-files?name=xai-user@example.com.json"

# 全删
curl -X DELETE -H "Authorization: Bearer YOUR_MGMT_KEY" \
  "https://your-cpa-host:8317/v0/management/auth-files?all=true"
```

### 3.7 导入线上 Sub2API（≥ v0.1.153）

本地 `exports/**/sub2api/sub2api-*.json` 为官方 `sub2api-data` 格式。线上导入：

```http
POST {sub2api}/api/v1/admin/accounts/data
x-api-key: <管理员 API Key>
# 或 Authorization: Bearer <admin JWT>
```

```json
{
  "data": { "type": "sub2api-data", "version": 1, "proxies": [], "accounts": [ ... ] },
  "skip_default_group_bind": false
}
```

> **注意（v0.1.153）**：若省略 `skip_default_group_bind`，服务端默认为 **true（不绑默认组）**。本项目上传时始终显式传该字段。

本项目配置：

```json
{
  "sub2api_cloud_upload_enabled": true,
  "sub2api_cloud_api_base": "https://your-sub2api.example.com",
  "sub2api_cloud_admin_key": "管理员API-Key",
  "sub2api_cloud_jwt": "",
  "sub2api_cloud_skip_default_group_bind": false,
  "sub2api_cloud_timeout": 60,
  "sub2api_cloud_retries": 3
}
```

| 环境变量 | 对应 |
| --- | --- |
| `SUB2API_BASE_URL` / `SUB2API_CLOUD_API_BASE` | api base |
| `SUB2API_ADMIN_API_KEY` / `SUB2API_CLOUD_ADMIN_KEY` | admin key |
| `SUB2API_JWT` | 管理员 JWT（无 key 时） |

CLI：

```bash
# 最新一批
python register_cli.py --sub2api-upload-latest

# 全部批次
python register_cli.py --upload-sub2api-cloud --sub2api-upload-all

# 指定目录 / 文件
python register_cli.py --upload-sub2api-cloud --sub2api-upload-dir ./exports/20260712_153045
python register_cli.py --upload-sub2api-cloud --sub2api-upload-files ./exports/.../sub2api-accounts.json
```

mint 成功且 `sub2api_cloud_upload_enabled=true` 时，会自动上传刚写出的单账号 Sub2API JSON。

响应需看 `data.account_created` / `account_failed`（HTTP 200 且 code=0 仍可能部分失败）。

> **导入不覆盖**：Sub2API `POST /accounts/data` 只做 **CreateAccount**。已存在的账号不会被更新 `base_url`/token。要刷新须先删除再导入。

### 3.8 线上 Sub2API 列表 / 删除 / 替换

| HTTP | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/admin/accounts?platform=grok&page=&page_size=` | 分页列表 |
| DELETE | `/api/v1/admin/accounts/:id` | 按数字 id 删除（无 bulk 接口） |

本项目 CLI：

```bash
# 列出（默认 platform=grok；* 表示全部）
python register_cli.py --sub2api-list
python register_cli.py --sub2api-list --sub2api-platform '*'

# 模糊删除（子串 / glob / re:正则；默认 dry-run）
python register_cli.py --sub2api-delete "@gardianwaves.org"
python register_cli.py --sub2api-delete "@gardianwaves.org" --yes

# 按本地最新批 email/name 删除线上匹配（只删不传）
python register_cli.py --sub2api-delete-latest
python register_cli.py --sub2api-delete-latest --yes

# 删除该 platform 下全部账号（逐条 DELETE）
python register_cli.py --sub2api-delete-all --yes

# 按本地最新批 email/name 删线上匹配，再上传最新批
python register_cli.py --sub2api-replace-latest --yes

# 危险：先清空该 platform 全部线上账号，再上传最新批
python register_cli.py --sub2api-replace-latest --sub2api-replace-all-platform --yes
```

鉴权与上传相同：`sub2api_cloud_admin_key`（`x-api-key`）或 JWT。

### 3.9 上传前 OAuth token 健康检查

Sub2API 在 access 过期后会走 refresh；若 refresh 已被撤销，线上会出现：

```text
GROK_OAUTH_TOKEN_REFRESH_FAILED / invalid_grant: Refresh token has been revoked
```

本项目在 **上传前** 做离线检查（**不能** 预知 refresh 是否已 revoked）：

| 检查 | 行为 |
| --- | --- |
| 无 `access_token` | 标记 unhealthy，默认跳过 |
| 无 `refresh_token` | 标记 unhealthy，默认跳过 |
| JWT `exp` 已过期（含 `sub2api_token_skew_sec` 余量） | 标记 unhealthy，默认跳过并提示 remint |
| 即将过期（`sub2api_token_soon_sec`，默认 1h） | 仅警告，仍上传 |
| access 仍有效 | 记录 exp/ttl/`base_url` 后上传 |

配置：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `sub2api_upload_check_tokens` | `true` | 是否检查 |
| `sub2api_upload_skip_unhealthy` | `true` | 是否跳过 unhealthy |
| `sub2api_token_skew_sec` | `120` | 提前多少秒视为过期 |
| `sub2api_token_soon_sec` | `3600` | 多少秒内过期仅警告 |

强制仍上传不健康账号：

```bash
python register_cli.py --sub2api-upload-latest --sub2api-upload-allow-unhealthy
```

不健康时请：

```bash
python register_cli.py --remint-missing --headed
python register_cli.py --sub2api-replace-latest --yes
```

### 批次导出配置

| 配置 / 参数 | 说明 |
| --- | --- |
| `export_batch_enabled` | 默认 `true`：每次注册跑新建 `exports/YYYYMMDD_HHMMSS` |
| `export_batch_parent` | 批次父目录，默认 `./exports` |
| `export_batch_also_global_accounts` | 本批 accounts 是否再镜像到 `accounts/accounts_cli.txt` |
| `--batch-name NAME` | 批次目录名后缀 |
| `--batch-dir PATH` | 使用已有批次目录（续跑同一批） |
| `--no-batch` | 关闭批次，写回扁平 `exports/cpa` |

等价 curl（单文件）：

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_MGMT_KEY" \
  -F "file=@exports/cpa/xai-user@example.com.json" \
  "https://your-cpa-host:8317/v0/management/auth-files"
```

常见错误：

| HTTP | 含义 |
| --- | --- |
| 401 | management key 错误 / 缺失 |
| 403 | 远端未开 `allow-remote` 且无 `MANAGEMENT_PASSWORD` |
| 404 | Management API 未启用（secret-key 与 MANAGEMENT_PASSWORD 皆空） |
| 503 | core auth manager 不可用（CPA 未完全启动） |

#### B. 本机 hotload

```json
{
  "cpa_copy_to_hotload": true,
  "cpa_hotload_dir": "~/.cli-proxy-api"
}
```

CPA 监视 `auth-dir`，拷贝后热加载；适合本机开发，**不能**代替远端 Management API。

### 3.6 CPA HTTP 代理行为

- 配置项 `cpa_proxy` / 注册线程代理 / `proxy` 可指定 mint HTTP 出口。  
- **默认直连**时：`cpa_xai.proxyutil.build_opener` 使用 `DirectProxyHandler`，**不会**读取 macOS「系统代理」或 `urllib.getproxies()`（避免 `127.0.0.1:1082` → `Tunnel 503`）。  
- 仅当显式配置代理或 `USE_SYSTEM_PROXY=1` 时才走系统/环境代理。  

---

## 4. 格式 B：Sub2API 数据导入

### 4.1 实现方式

**纯文件转换**，不再开浏览器、不再重新 mint。

入口：`cpa_to_sub2api.export_after_cpa_result(result, config)`

| 步骤 | 函数 | 输出 |
| --- | --- | --- |
| 单账号 | `convert_cpa_file` | `exports/sub2api/sub2api-<cpa 文件 stem>.json` |
| 合集 | `rebuild_combined` | 扫描 `exports/cpa/xai-*.json` → `exports/sub2api/sub2api-accounts.json` |

路径配置：

| 配置 | 默认 |
| --- | --- |
| `export_root` | `./exports`（统一根目录） |
| `cpa_auth_dir` | `./exports/cpa`（CPA 子目录；合集扫描源） |
| `sub2api_export_dir` | `./exports/sub2api` |
| `sub2api_combined_file` | `./exports/sub2api/sub2api-accounts.json` |

### 4.2 外层文档结构

对齐 Sub2API admin data-import（`type=sub2api-data`）：

```json
{
  "type": "sub2api-data",
  "version": 1,
  "exported_at": "2026-07-11T12:00:00Z",
  "proxies": [],
  "accounts": [
    {
      "name": "user@example.com",
      "platform": "grok",
      "type": "oauth",
      "concurrency": 1,
      "priority": 50,
      "expires_at": 1720700000,
      "auto_pause_on_expired": true,
      "credentials": { },
      "extra": { }
    }
  ]
}
```

### 4.3 关键约束

| 项 | 要求 |
| --- | --- |
| `platform` | 必须是 **`grok`**（**不是** `xai`） |
| `type` | `oauth` |
| 账号级 `expires_at` | **Unix 秒** |
| credentials 内 `expires_at` | **RFC3339 字符串**（若有） |
| credentials.`base_url` | 默认 **保留 CPA**（通常 `https://cli-chat-proxy.grok.com/v1`） |

### 4.4 CPA → Sub2API 字段映射

实现：`cpa_xai_to_sub2api_account`

| CPA 侧 | Sub2API 侧（对齐可用参考导出） |
| --- | --- |
| `access_token` / `refresh_token` / `id_token` | `credentials.*` |
| `email` / JWT | `credentials.email`；`extra.email` |
| id_token `given_name` / `family_name` | `account.name`（无则回退 email） |
| JWT `iat` | `credentials._token_version`（unix ms） |
| JWT `exp` / `expired` | credentials `expires_at`（RFC3339）+ 可选账号 unix `expires_at` |
| `base_url` | **默认保留** `cli-chat-proxy` |
| — | `platform: "grok"`、`type: "oauth"` |
| — | `concurrency: 1`、`priority: 1`、`rate_multiplier: 1` |
| client / scope | `credentials.client_id`、`scope` |

**不导出**（参考里没有）：`token_endpoint`、`redirect_uri`、`sub`、`expires_in`、CPA `headers`、多余 import 元数据。

> **403 permission-denied：** 完整排查记录（思路、尝试、Sub2API/CPA 源码对照、实测与后续方向）见  
> **[grok-403-investigation.md](grok-403-investigation.md)**。  
> 摘要：直连上游 `/responses` 仍 403 时为账号/token entitlement；`/models` 200 ≠ 能对话；仅改导入 JSON 无法修复。

### 4.5 base_url 策略（重要）

能正常调用模型的 Sub2API 参考导出使用 **`https://cli-chat-proxy.grok.com/v1`**，而不是 `api.x.ai`。

配置 `sub2api_base_url_mode`：

| 模式 | 行为 |
| --- | --- |
| `preserve`（**默认**） | 保留 CPA 的 `base_url`；空则 `cli-chat-proxy` |
| `cli_chat_proxy` | 强制 `https://cli-chat-proxy.grok.com/v1` |
| `api_xai` | 旧行为：强制 `https://api.x.ai/v1`（free token 常不可用） |

| 格式 | 典型 base_url | 含义 |
| --- | --- | --- |
| CPA | `https://cli-chat-proxy.grok.com/v1` | CLIProxyAPI / Build 免费路径 |
| Sub2API（默认） | 同上 | 对齐可用参考 |
| Sub2API（`api_xai`） | `https://api.x.ai/v1` | 公共 API；free OAuth 常 403 |

已导出的旧 JSON 若仍是 `api.x.ai`，需从 CPA **重新转换**后再导入。

---

## 5. 与注册热浏览器的关系

热浏览器（`cpa_mint_prefer_warm_browser`）**只影响 mint 成功率**（能否过 Cloudflare、拿到 token），**不改变**两种 JSON 的字段定义与转换逻辑。

推荐配置：

```json
{
  "cpa_export_enabled": true,
  "cpa_mint_prefer_warm_browser": true,
  "cpa_force_standalone": true,
  "sub2api_export_enabled": true,
  "export_root": "./exports",
  "cpa_auth_dir": "./exports/cpa",
  "sub2api_export_dir": "./exports/sub2api",
  "sub2api_combined_file": "./exports/sub2api/sub2api-accounts.json"
}
```

说明：

- `cpa_force_standalone=true` 时，若仍传入注册 `page` 且 prefer_warm，编排层会**临时关闭** standalone，优先热 tab。  
- 独立 mint worker（无 page）更容易被 CF 拦；默认 CLI 路径会在注册线程上 mint。  

---

## 6. 日志与成功判定

成功时 CLI 大致日志：

```text
+ 注册成功: user@example.com
[cpa] mint on register thread (warm browser preferred)
[cpa] prefer warm register browser for mint (force_standalone=false)
[cpa] OK path=.../exports/cpa/xai-user@example.com.json
[sub2api] OK single=.../exports/sub2api/sub2api-xai-....json combined=.../sub2api-accounts.json
+ CPA auth: ...
+ Sub2API: ... (combined=...)
```

失败常见：

| 现象 | 含义 |
| --- | --- |
| 注册成功、CPA 失败 | 有 accounts 行，无 CPA/Sub2API → 见 §7.2 补 mint |
| `cloudflare_blocked` | mint 浏览器被 CF 硬拦；用热浏览器或 `--headed` |
| `Tunnel connection failed: 503` | HTTP 出口/代理异常；确认直连或 `cpa_proxy` 可用 |
| `SSL: UNEXPECTED_EOF` | token 轮询网络抖动（会自动重试） |
| `authorization_pending` 超时 | 设备码未在超时内批准 |
| Sub2API 未出现 | `sub2api_export_enabled=false` 或 CPA `ok=false`（含 probe 失败） |

注意：`cpa_probe_after_write=true` 时，token 写出后若 models 探测无 grok-4.5，`result.ok` 可能被置为 `false`，此时**可能已写 CPA 文件**，但编排层可能不跑 Sub2API（取决于 `mint_and_export` 返回的 `ok`）。排查时请同时看 `exports/cpa/` 与 `exports/cpa/cpa_auth_failed.txt`。

---

## 7. 手动 / 补导出

### 7.1 仅从已有 CPA 转 Sub2API

```bash
# 代码入口（示例）
python -c "
from pathlib import Path
import cpa_to_sub2api
p = Path('exports/cpa')
for f in p.glob('xai-*.json'):
    out, _ = cpa_to_sub2api.convert_cpa_file(f, out_dir='exports/sub2api')
    print(out)
cpa_to_sub2api.rebuild_combined('exports/cpa', 'exports/sub2api/sub2api-accounts.json')
"
```

### 7.2 对已注册账号补 mint（推荐 CLI）

从 `accounts_cli.txt`（或 `--accounts-file`）里找出**尚无** `exports/cpa/xai-*.json` 的行，逐个补 OIDC mint + Sub2API：

```bash
# 必须有界面：冷启动 headless 几乎必被 CF 拦
python register_cli.py --remint-missing --headed
```

行为要点（`register_cli.remint_missing_from_accounts`）：

| 项 | 说明 |
| --- | --- |
| 强制 headed | 忽略 `config.cpa_headless=true`；并设 `HEADLESS=0` |
| 账号行解析 | `parse_account_line`：按 JWT（`eyJ…`）锚定，避免密码末尾 `-` 把 SSO 拆成 `-eyJ…` |
| 浏览器 | 默认每号新开 standalone Chromium（不复用上号会话） |
| 间隔 / 重试 | 账号间约 4s；失败列表再自动跑一轮 |
| cookie | SSO 注入多域名（`accounts.x.ai` / `.x.ai` 等） |

备选脚本（功能类似，参数以脚本 `--help` 为准）：

```bash
uv run python scripts/backfill_cpa_xai_from_accounts.py --accounts accounts_cli.txt
```

成功后的 CPA 若走 `cpa_export` 完整路径，会连带 Sub2API；若脚本只调 `mint_and_export`，可能需再跑 7.1 转换。

### 7.3 accounts 行格式

```text
email----password----sso_jwt
```

- SSO 为 JWT，以 `eyJ` 开头。  
- 历史密码可能含 `token_urlsafe` 的 `-`，**禁止**用简单 `split("----")` 解析（会破坏 JWT）。  
- 新注册密码已去掉 `-`/`_`，降低歧义；读盘仍请用 `parse_account_line`。  

---

## 8. 安全注意

- `exports/`（含 `cpa/`、`sub2api/`）、`accounts_*.txt` 含长期凭证，**勿提交 Git**（通常已在 `.gitignore`）。  
- CPA 文件权限尽量保持 `0600`。  
- 合集 `sub2api-accounts.json` 含目录内**全部** `xai-*.json` 账号，分发时注意范围。  
- 旧路径 `cpa_auths/`、`sub2api_exports/` 若仍有文件，可手动迁入 `exports/cpa`、`exports/sub2api`。

---

## 9. 相关文件一览

```text
cpa_export.py                 # 编排：mint → CPA → Sub2API
cpa_to_sub2api.py             # CPA 文件 → Sub2API data-import
cpa_xai/
  mint.py                     # mint_and_export
  schema.py                   # build_cpa_xai_auth / 文件名
  writer.py                   # 原子写 xai-*.json
  oauth_device.py             # 设备码 + 轮询 token
  browser_confirm.py          # 浏览器确认授权
register_cli.py               # 注册成功后触发 mint
docs/export-cpa-and-sub2api.md  # 本文档
```

---

## 10. 小结

| 问题 | 答案 |
| --- | --- |
| 两种格式如何产生？ | 先 CPA mint 落盘，再文件转换 Sub2API |
| 能否只出 Sub2API？ | 需要先有 CPA 文件；可只对已有 CPA 跑转换 |
| platform 写什么？ | Sub2API 必须 `grok`；CPA 文件 `type` 为 `xai` |
| base_url 为何曾不同？ | 旧版把 Sub2API 改成 api.x.ai；现默认保留 cli-chat-proxy（`sub2api_base_url_mode`） |
| 注册成功为何没有 JSON？ | mint 失败（常见 CF）；用热浏览器或 `--remint-missing --headed` |
| 补 mint 命令？ | `python register_cli.py --remint-missing --headed` |
