# Grok Register

浏览器自动化注册 Grok 账号，导出 CPA / Sub2API，并支持上传到线上服务。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.13](https://img.shields.io/badge/Python-3.13-3776AB.svg)

> 仅供学习与测试。请遵守目标站点服务条款与当地法律。

## 功能

- **注册**：Chrome / Chromium + Turnstile 流程，支持多线程批量
- **邮箱**：`tempmail_lol` / `yyds` / `cloudflare` / `duckmail`
- **导出**（按批分子目录）：
  - 账号：`exports/<批次>/accounts.txt`
  - CPA：`exports/<批次>/cpa/xai-*.json`（CLIProxyAPI 兼容）
  - Sub2API：`exports/<批次>/sub2api/sub2api-*.json`
- **补缺**：缺 CPA 则浏览器 remint；有 CPA 缺 Sub2API 则本地转换
- **线上上传**：
  - CPA → Management API `auth-files`
  - Sub2API → Admin API `accounts/data`（兼容 ≥ v0.1.153）
- **交互菜单**：`python register_cli.py`（无参数进入）
- **代理**：可选单代理 / 代理池；不配则直连

## 环境

| 项 | 要求 |
| --- | --- |
| Python | 3.13（`pyproject.toml`：`>=3.13,<3.14`） |
| 浏览器 | Chrome 或 Chromium |
| 系统 | macOS / Windows / Linux |

## 安装

```bash
git clone https://github.com/aiqj/grok-register.git
cd grok-register
pip install -r requirements.txt
# 或
uv sync

cp config.example.json config.json
# 编辑 config.json：邮箱服务商、CPA/Sub2API 云配置等
```

`config.json` 含密钥，已在 `.gitignore` 中忽略。

## 使用

### 交互菜单（推荐）

```bash
python register_cli.py
# 或
python register_cli.py --menu
```

```text
  1. 注册账号
  2. 补缺 CPA / Sub2API
  3. 上传到线上
  4. 管理线上 CPA 凭证
  5. 说明
  0. 退出
```

### 命令行

```bash
# 注册
python register_cli.py --count 5 --threads 1 --headed
python register_cli.py --extra 10 --headed

# 补缺（缺 CPA → remint；有 CPA 缺 Sub2API → 本地转换）
python register_cli.py --remint-missing --headed

# 上传 CPA
python register_cli.py --cpa-upload-latest
python register_cli.py --upload-cpa-cloud --cpa-upload-all

# 上传 Sub2API
python register_cli.py --sub2api-upload-latest
python register_cli.py --upload-sub2api-cloud --sub2api-upload-all

# 线上 CPA 凭证
python register_cli.py --cpa-list
python register_cli.py --cpa-delete "@example.com"          # 预览
python register_cli.py --cpa-delete "@example.com" --yes    # 删除
python register_cli.py --cpa-delete-all --yes               # 全删
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--count N` | 目标账号总数（含已有） |
| `--extra N` | 本批再注册 N 个 |
| `--threads N` | 注册并发 |
| `--headed` / `--headless` | 有界面 / 无头 |
| `--batch-name NAME` | 批次目录名后缀 |
| `--batch-dir DIR` | 使用已有批次目录 |
| `--no-batch` | 扁平导出到 `exports/cpa` 等 |
| `--proxy` / `--proxy-pool` | 代理 |

## 配置要点

编辑 `config.json`（参考 `config.example.json`）：

| 配置 | 说明 |
| --- | --- |
| `email_provider` | 邮箱通道 |
| `export_batch_enabled` | 是否按批分子目录（默认 true） |
| `cpa_export_enabled` | 是否 mint CPA |
| `sub2api_export_enabled` | 是否写 Sub2API |
| `cpa_cloud_upload_enabled` | mint 后自动上传 CPA |
| `cpa_cloud_api_base` | 线上 CPA 地址 |
| `cpa_cloud_management_key` | CPA Management 密钥 |
| `sub2api_cloud_upload_enabled` | mint 后自动上传 Sub2API |
| `sub2api_cloud_api_base` | 线上 Sub2API 地址 |
| `sub2api_cloud_admin_key` | Sub2API 管理员 API Key |
| `sub2api_cloud_skip_default_group_bind` | 导入时是否跳过默认分组（默认 false，会绑 `grok-default`） |

环境变量可覆盖密钥：`CPA_CLOUD_*`、`SUB2API_BASE_URL`、`SUB2API_ADMIN_API_KEY` 等。

## 输出布局

```text
exports/
  20260713_160945/           # 每批一个目录
    accounts.txt
    cpa/
      xai-<email>.json
    sub2api/
      sub2api-xai-<email>.json
      sub2api-accounts.json  # 合集
accounts/
  accounts_cli.txt           # 可选全局镜像
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/export-cpa-and-sub2api.md](docs/export-cpa-and-sub2api.md) | CPA / Sub2API 导出与线上导入 |
| [docs/registration.md](docs/registration.md) | 注册流程说明 |
| [docs/batch-speed.md](docs/batch-speed.md) | 批量与性能 |

## 目录

```text
.
├── register_cli.py              # 入口
├── config.example.json          # 配置模板（复制为 config.json）
├── pyproject.toml
├── requirements.txt
├── src/grok_register/
│   ├── cli.py                   # CLI 参数与批量调度
│   ├── menu.py                  # 交互菜单
│   ├── core.py                  # 注册主流程 / 云上传
│   ├── engine.py
│   ├── browser/                 # 浏览器、CF 预热、turnstile 扩展
│   ├── proxy/                   # 代理池
│   ├── mail/                    # 邮箱池
│   ├── transport/               # 浏览器传输层
│   └── export/                  # CPA mint、Sub2API 转换
│       ├── cpa_export.py
│       ├── cpa_to_sub2api.py
│       └── cpa_xai/
├── scripts/                     # 辅助脚本
├── docs/
├── tests/
├── exports/                     # 运行时导出（不入库）
│   └── <批次>/
│       ├── accounts.txt
│       ├── cpa/
│       └── sub2api/
├── accounts/                    # 运行时账号（不入库）
└── logs/                        # 运行时日志（不入库）
```

## 说明

- 注册成功 ≠ Grok Build 对话一定可用；上游对 OAuth 可能返回 `permission-denied`（403），与是否导入 CPA/Sub2API 无关。
- 线上 CPA 删除只影响远端凭证，不删本地 `exports/`。

## 已知问题（Sub2API / 上游，未解决）

以下问题已验证或对照实现确认，**本仓库当前无法从导入侧彻底修复**，记在此处便于后续对照；细节见 [docs/export-cpa-and-sub2api.md](docs/export-cpa-and-sub2api.md)。

### 1. 上游 403 / `permission-denied`（根因在 xAI，不在导入）

| 现象 | 说明 |
| --- | --- |
| 注册 + mint + 导入均成功，对话/调用仍 403 | 免费 OAuth 账号在上游可能无对应 entitlement |
| 同一 `access_token` 在 CPA 与 Sub2API 都可能失败 | 换平台导入**不能**解除上游权限拒绝 |
| 本机 probe（`cli-chat-proxy` + CPA 完整 headers）仍可能 403 | 进一步说明不是「只差某几个请求头」就能修好 |

**结论**：403 多为 **xAI 对 free OAuth / Build 路径的权限策略**，不是「没导进 Sub2API」或「JSON 字段写错」这一类本工具可单独闭环的问题。

### 2. CPA 与 Sub2API 的请求路径 / 凭证形态不一致

| 项 | CPA（CLIProxyAPI） | Sub2API（本项目导出） |
| --- | --- | --- |
| 典型 `base_url` | `https://cli-chat-proxy.grok.com/v1`（Build 免费路径） | `https://api.x.ai/v1`（官方 Grok OAuth 导入约定） |
| 客户端 headers | CPA JSON 内带 `User-Agent`、`x-xai-token-auth`、`x-grok-client-*` 等 | **不写入** credentials（转换时故意剥离 CPA-only 字段，避免污染 Sub2API schema） |
| 上游调用方 | CPA 按自身 xAI 客户端逻辑发请求 | Sub2API 服务端按 Grok OAuth 实现发请求 |

影响：

- 转换层把 `cli-chat-proxy` **改写**为 `api.x.ai/v1` 是为对齐 Sub2API 约定；**不等于** free Build token 在 `api.x.ai` 上一定等价可用。
- 即便把 CPA headers 塞进 Sub2API 导入 JSON，**服务端若不使用这些字段发上游请求，仍无效**——需要 Sub2API 侧改 Grok OAuth 请求构造（本仓库范围外）。

### 3. 本项目已做 vs 仍做不到的边界

| 已完成 | 仍未解决 / 不在本仓 |
| --- | --- |
| CPA → Sub2API 本地转换（`platform=grok`、`type=oauth`） | 上游 free OAuth 403 / entitlement |
| 线上导入 Admin `accounts/data`（≥ v0.1.153） | Sub2API 上游请求头与 CPA/cli-chat-proxy 对齐（需上游 PR） |
| 批次导出、最新批/全量上传、默认组绑定可配置 | 导入成功 ≠ 模型列表/对话一定可用 |
| 缺 CPA 浏览器 remint；有 CPA 缺 Sub2API 时本地转换 | 同一 token 在 `api.x.ai` 与 `cli-chat-proxy` 行为差异的根治 |

### 4. 使用与排查时注意

- **HTTP 200 且 code=0** 仍可能部分账号失败：看响应里的 `account_created` / `account_failed`。
- v0.1.153：服务端若省略 `skip_default_group_bind` 默认**不绑**默认组；本项目上传会**显式**传该字段（配置项 `sub2api_cloud_skip_default_group_bind`，默认 `false` 即会尝试绑 `grok-default`）。
- 排查 403 时优先区分：**导入失败**（管理 API / 鉴权 / 字段）vs **导入成功后上游拒绝**（token/entitlement/路径）；后者不要反复改导出格式期待奇迹。
- 需要「更像 CPA」的上游行为时，短期仍以 **CPA / cli-chat-proxy** 路径验证；Sub2API 侧对齐依赖其 Grok OAuth 实现演进。

## License

[MIT](LICENSE)
