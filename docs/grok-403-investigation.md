# Grok 对话 403（permission-denied）排查记录

> 记录批量注册 → CPA / Sub2API 导出导入后，调用 grok-4.5 仍返回上游 403 的完整思路、已做尝试、源码对照与后续方向。  
> 状态：**未彻底解决**（2026-07 结论：根因在 xAI 对 free OAuth 账号的 chat entitlement，非本仓库导入 JSON 字段 alone）。  
> 相关实现：`cpa_xai/`、`cpa_to_sub2api.py`、Sub2API 线上导入；对照 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api)、[router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)。

---

## 1. 问题现象

### 1.1 典型报错（Sub2API 测模型）

```text
Grok Responses API returned 403:
{
  "code": "permission-denied",
  "error": "Access to the chat endpoint is denied. Please ensure you're using the correct credentials.
            If you believe this is a mistake, please log into console.x.ai and update the permissions,
            or contact support."
}
```

### 1.2 其它相关错误（曾出现）

| 错误 | 含义 |
| --- | --- |
| `GROK_OAUTH_TOKEN_REFRESH_FAILED` / `invalid_grant` / `Refresh token has been revoked` | refresh 已作废；需 remint，改 JSON 无效 |
| HTTP **426** CLI version outdated | 请求缺 Grok CLI 版本相关头；与 403 不同 |
| HTTP **402**（部分参考账号 probe） | 额度/付费门槛；与 permission-denied 不同 |

### 1.3 常伴随的「假阳性」

| 观察 | 易误解为 | 实际 |
| --- | --- | --- |
| 注册成功 | 一定能聊天 | 只代表 Web 注册 / SSO 拿到了 |
| CPA mint / Sub2API 导入 HTTP 200 | token 可用 | 只代表文件写出 / 管理 API 入库 |
| `GET …/models` 返回 200 且含 `grok-4.5` | 能调对话 | **≠** `POST …/responses` 有权限 |

---

## 2. 排查思路（分层）

按「由近到远」排除，避免在错误层反复改格式：

```text
1) 导入层：Sub2API Admin 是否 create 成功？account_created / account_failed
2) 凭证形态：base_url、scope、JWT exp、refresh 是否还在
3) 网关层：Sub2API 是否 refresh 失败 / 路由到错误 base
4) 上游层：同一 access_token 直连 cli-chat-proxy /responses 是否也 403
```

**判定规则（关键）：**

- **直连上游也 403** → 账号/token entitlement 问题，改导入 JSON、换 Sub2API/CPA 网关都无法单独修复。  
- **直连 OK、仅网关 403** → 再查网关 headers、模型映射、base_url 运行时改写、分组。  
- **refresh invalid_grant** → remint，不要重传旧 refresh。

---

## 3. 已做尝试（时间线式）

### 3.1 导入格式对齐「能用」的 Sub2API 参考导出

参考样本（用户提供、可调模型）特征：

- `platform=grok`、`type=oauth`
- `credentials.base_url = https://cli-chat-proxy.grok.com/v1`
- credentials 键：`_token_version`、tokens、`client_id`、`email`、`expires_at`、`scope`、`token_type`
- `priority=1`、`rate_multiplier=1`、`concurrency=1`
- `extra` 主要是 `email`（运行态可有 `grok_usage_snapshot`）
- 参考 JWT 常有 **`referrer: "sub2api"`**（Sub2API 授权码 OAuth 路径）

本项目已对齐（能改的字段）：

| 改动 | 说明 |
| --- | --- |
| `sub2api_base_url_mode=preserve` | 默认保留 CPA 的 cli-chat-proxy，不再强制 remap 到 `api.x.ai` |
| credentials 键集 | 对齐参考；去掉 `token_endpoint` / `redirect_uri` / `sub` / `expires_in` 等非参考字段 |
| `priority=1`、`rate_multiplier=1` | 对齐参考 |
| `_token_version` | JWT `iat * 1000` |
| `name` | 优先 id_token 姓名 |
| `extra` | 仅 `email` |

**结果：** 格式对齐后，批量临时邮账号 **仍 403**。

### 3.2 导入语义：create-only，不能覆盖

Sub2API `POST /api/v1/admin/accounts/data` 只 **CreateAccount**，不更新已有 credentials。

已实现：

- 列表 / 模糊删 / 删最新批 / 删全部 grok / replace-latest（先删匹配再上传）
- CLI：`--sub2api-list`、`--sub2api-delete`、`--sub2api-delete-latest`、`--sub2api-replace-latest` 等

**结果：** 解决「旧 `api.x.ai` 账号残留」类问题；**不解决** 新 token 本身 403。

### 3.3 Token 健康检查（上传前）

离线检查 JWT `exp`、是否缺 access/refresh；默认跳过不健康账号上传。

**局限：** **无法** 离线预知 refresh 是否已被 revoke，也 **无法** 预知 chat entitlement。

### 3.4 Mint 路径对齐 Sub2API

对照 [sub2api](https://github.com/Wei-Shaw/sub2api)：

| 路径 | 源码要点 |
| --- | --- |
| UI「Grok 账号授权」 | 授权码 + PKCE，`referrer=sub2api`，`plan=generic` |
| SSO → Build | `ConvertSSOToBuild`：Web `sso` cookie 自动 device verify/verify/approve |
| 上游转发头 | `User-Agent: sub2api-grok/1.0`、`X-Grok-Client-Version: 0.2.93`（服务端写死） |

本项目已做：

- 新增 `cpa_xai/sso_to_build.py`（移植 ConvertSSOToBuild）
- `mint_and_export` 默认优先 SSO→Build（`cpa_mint_prefer_sso_build`）
- device scope 扩展为含 `conversations:read conversations:write`（与 Sub2API `SSOBuildScope` 一致）

**结果：** 最新批次 JWT **已带** `conversations:*`，`GET /models` 200，**`POST /responses` 仍 403**（见 §4 实测）。

### 3.5 对照 CLIProxyAPI（CPA 能用 Grok）

对照 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) `internal/auth/xai` + `xai_executor.go`：

| 点 | CPA 行为 | 对本问题的含义 |
| --- | --- | --- |
| OAuth | 同样 device-code + 同 `client_id` | mint 与 CPA 同源 |
| 落盘 base_url | 常为 `api.x.ai/v1` | **runtime** 对 OAuth chat 改写到 cli-chat-proxy |
| 聊天头 | `X-XAI-Token-Auth`、`x-grok-client-version`、`User-Agent: xai-grok-workspace/0.2.93` | 过 426 版本门 |
| Scope | **不含** conversations | 说明 CPA 能用 **不是** 因为多了 conversations |
| 无版本头 | 上游 **426** | 与 403 不同层 |

本项目已把本地 probe 的 CPA 头对齐为上述 workspace 风格。

**结果：** 同一 free token 用 CPA 头 / Sub2API 头 / bare：

- bare → **426**（缺版本）
- 有版本头 → **403**（permission-denied）

→ 头已足够过版本检查；**403 是权限**。

---

## 4. 关键实测（本地直连上游）

样本：批次 `20260714_115457`（新注册 + 宽 scope mint 后导出）。

| 检查 | 结果 |
| --- | --- |
| `credentials.base_url` | `https://cli-chat-proxy.grok.com/v1` |
| JWT `scope` | 含 `grok-cli:access`、`conversations:read/write` |
| JWT `referrer` | 无（非 UI 授权码路径） |
| `GET {cli-chat-proxy}/models` | **200**，含 `grok-4.5` |
| `POST {cli-chat-proxy}/responses`（Sub2API 头） | **403** permission-denied |
| `POST …/responses`（CPA workspace 头） | **403** 同上 |
| `POST https://api.x.ai/v1/responses` | **403** 同上 |

**结论：** 问题可在 **不经过 Sub2API** 时复现 → **不是导入格式 / Sub2API 少字段** 单独导致。

与用户提供的「能用」参考对比：

| | 能用参考 | 批量新注册 |
| --- | --- | --- |
| 邮箱类型 | 个人 Gmail 等 | 临时邮域名 |
| mint | 常为 Sub2API UI OAuth（JWT 可有 `referrer=sub2api`） | SSO→Build / device-code |
| 直连 `/responses` | 可用 | 403 |

---

## 5. 研究结论（当前共识）

1. **注册成功、mint 成功、导入成功、models 200 都不等于能 chat。**  
2. **`permission-denied` on chat endpoint 是 xAI 上游对 token/账号的拒绝。**  
3. 本仓库已尽量对齐：  
   - 导出形态（cli-chat-proxy + 参考 credentials 形状）  
   - mint 路径（SSO→Build、宽 scope）  
   - 线上删除/替换流程  
   - 请求头（本地 probe 对齐 CPA；Sub2API 头由服务端负责）  
4. **批量临时邮 free OAuth 常处于「能列模型、不能对话」状态**；与 CPA 源码是否「能用」不矛盾——CPA 上能用的通常是 **已有 chat 权限的账号**，不是 CPA 对任意 free token 有特殊解锁。  
5. **`referrer=sub2api` 写在授权码 OAuth 发 token 时**，无法靠改导入 JSON 伪造。

---

## 6. 后续方向（按优先级）

### P0 — 验证「账号类型」假设（推荐先做）

1. 取 **CPA 里已能聊** 的 `xai-*.json`（或 Sub2API 能用参考账号），解码 JWT，记录：email 域名、scope、`referrer`、team_id。  
2. **同一 access_token** 直连 `cli-chat-proxy/responses`（CPA 头）。  
3. 再导入 Sub2API 测。  
   - 直连 OK、Sub2API 也 OK → 批量号是 entitlement 问题。  
   - 直连 OK、仅 Sub2API 挂 → 再查 Sub2API 版本/分组/模型映射。  
4. 用 **同类型个人邮箱** 走本项目 mint，对比临时邮。

### P1 — 产品/流水线护栏

- mint 后默认 **`/responses` probe**（Sub2API 头 + CPA 头）；**403 则不导出/不上传**，避免无效入库。  
- 导出 meta 记录：`mint_source`、JWT scope、`probe_chat` 结果、是否 remint_recommended。  
- 文档与菜单文案明确：导入成功 ≠ 可用对话。

### P2 — 可选工程探索（不保证修复 free 号）

| 方向 | 说明 | 风险/成本 |
| --- | --- | --- |
| 浏览器 PKCE + `referrer=sub2api` | 完整对齐 Sub2API UI 授权 | 实现重；对无 entitlement 账号可能仍 403 |
| 与 Sub2API 上游 PR | 可选把 CPA 的 `X-XAI-Token-Auth` 等也带上 | 对本仓 free 号实测头已非根因 |
| 账号来源策略 | 减少临时邮、提高「真人」邮箱比例 | 运营侧，非纯技术 |
| 付费 API Key / `using_api` 路径 | 走官方 API 计费 | 与 free Build 目标不同 |

### P3 — 不必再投入的方向

- 仅改 `priority` / `extra` / 去掉 `token_endpoint` 等再期望 403 消失。  
- 认为「导进 Sub2API 就会自动获得 chat 权限」。  
- 对已 `invalid_grant` 的 refresh 反复上传。

---

## 7. 快速自检清单

```bash
# 1) 看 JWT scope / exp（勿打印完整 token）
# 2) 直连 models / responses（项目 venv）
.venv/bin/python -c "
# 使用 probe_models / probe_mini_response，style=sub2api 与 cpa
"

# 3) 上传前 token 健康（过期会 skip）
.venv/bin/python register_cli.py --sub2api-upload-latest

# 4) 必须换新 token 时
.venv/bin/python register_cli.py --remint-missing --headed
.venv/bin/python register_cli.py --sub2api-replace-latest --yes
```

日志关键字：

- `[cpa] mint try SSO→Build` / `mint SSO→Build ok`
- `[sub2api] token UNHEALTHY` / `token ok`
- `[cloud-sub2api] token filter: skipped_unhealthy=…`

---

## 8. 相关代码与配置

| 项 | 位置 |
| --- | --- |
| SSO→Build | `src/grok_register/export/cpa_xai/sso_to_build.py` |
| mint 编排 | `src/grok_register/export/cpa_xai/mint.py` |
| CPA 头 / Sub2API 头常量 | `src/grok_register/export/cpa_xai/schema.py` |
| Sub2API 导出形状 | `src/grok_register/export/cpa_to_sub2api.py` |
| 上传前 token 检查 | `src/grok_register/core.py`（`analyze_sub2api_oauth_account` 等） |
| 删除/替换线上账号 | `--sub2api-delete*` / `--sub2api-replace-latest` |
| 配置 | `cpa_mint_prefer_sso_build`、`sub2api_base_url_mode`、`sub2api_upload_skip_unhealthy` |

---

## 9. 修订记录

| 日期 | 摘要 |
| --- | --- |
| 2026-07 | 初版：汇总 base_url 对齐、导入 create-only、refresh 失效、Sub2API/CPA 源码对照、直连 403 实测与后续方向 |

如有新的「能用 / 不能用」对照样本（脱敏 JWT claims + 直连 status），请追加到 §4 与 §9。
