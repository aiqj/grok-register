#!/usr/bin/env bash
# =============================================================================
# CPA 模型探测 + 自动排除失败模型
#
# 放在部署了 CLIProxyAPI 的服务器上执行。
#
# 流程:
#   1) 业务 API Key → GET /v1/models
#   2) 对每个模型 POST /v1/chat/completions（404 时再试 /v1/responses）
#   3) 失败模型合并进 Management API: PUT /v0/management/oauth-excluded-models
#
# 依赖: bash, curl, jq（写排除必须 jq；仅探测可无 jq）
#
# 用法:
#   chmod +x cpa_probe_and_exclude_models.sh
#   ./cpa_probe_and_exclude_models.sh              # 探测 + 排除失败模型
#   ./cpa_probe_and_exclude_models.sh --dry-run    # 只探测不写
#   ./cpa_probe_and_exclude_models.sh --list-only  # 只列模型
#   ./cpa_probe_and_exclude_models.sh --show-excluded
#   ./cpa_probe_and_exclude_models.sh grok-4.5 claude-xxx   # 只测指定模型
# =============================================================================
set -euo pipefail

# ===================== 写死配置（部署前改这里）=====================
# CPA 地址（不要带 /v0/management；本机部署一般用 127.0.0.1）
API_BASE="http://127.0.0.1:8317"

# 业务 API Key = config.yaml 里 api-keys 中的一项（测 /v1/*）
API_KEY="sk-change-me"

# Management 明文密钥 = remote-management.secret-key 或 MANAGEMENT_PASSWORD
# （写 oauth-excluded-models 需要）
MANAGEMENT_KEY="mgmt-change-me"

# 探测超时秒数
PROBE_TIMEOUT=45

# 探测提示词
PROBE_PROMPT="Reply with exactly OK"
PROBE_MAX_TOKENS=16

# body 中出现这些子串（不区分大小写）也算失败
FAIL_BODY_RE='permission-denied|access denied|model_not_found|unknown model|not available|insufficient|quota|rate.?limit|invalid.?model'

# 排除列表写入的默认 provider 键（oauth-excluded-models 下）
# 官方文档常见: claude / codex / aistudio / antigravity / vertex
# Grok 视版本可能是 xai；可用 --provider 覆盖，或靠模型名自动猜
DEFAULT_EXCLUDE_PROVIDER="xai"

# 网络/curl 失败是否也加入排除（1=是 0=否）
EXCLUDE_ON_NETWORK_ERROR=1
# ==================================================================

DRY_RUN=0
LIST_ONLY=0
SHOW_EXCLUDED=0
ONLY_MODELS=()
FORCE_PROVIDER=""

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

json_escape() {
  # 优先 python；否则做最小转义
  if have python3; then
    python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()), end="")' <<<"$1"
  else
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}
    printf '"%s"' "$s"
  fi
}

usage() {
  cat <<'EOF'
用法: cpa_probe_and_exclude_models.sh [选项] [model_id ...]

选项:
  --dry-run          只探测，不写 oauth-excluded-models
  --list-only        只列出模型
  --show-excluded    显示当前排除表
  --provider NAME    强制所有失败模型写入该 provider 键
  -h, --help         帮助

无额外 model_id 时探测 /v1/models 返回的全部模型。
配置改脚本顶部 API_BASE / API_KEY / MANAGEMENT_KEY。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --list-only) LIST_ONLY=1; shift ;;
    --show-excluded) SHOW_EXCLUDED=1; shift ;;
    --provider) FORCE_PROVIDER="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --*) die "未知参数: $1" ;;
    *) ONLY_MODELS+=("$1"); shift ;;
  esac
done

API_BASE="${API_BASE%/}"
MGMT_BASE="${API_BASE}/v0/management"

[[ "$API_KEY" != "sk-change-me" && -n "$API_KEY" ]] || die "请编辑脚本设置 API_KEY"
if [[ "$LIST_ONLY" -eq 0 && "$DRY_RUN" -eq 0 ]]; then
  [[ "$MANAGEMENT_KEY" != "mgmt-change-me" && -n "$MANAGEMENT_KEY" ]] \
    || die "请编辑脚本设置 MANAGEMENT_KEY（或先用 --dry-run 只探测）"
fi

curl_api() {
  curl -sS -m "$PROBE_TIMEOUT" \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    "$@"
}

curl_mgmt() {
  curl -sS -m 60 \
    -H "Authorization: Bearer ${MANAGEMENT_KEY}" \
    -H "X-Management-Key: ${MANAGEMENT_KEY}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    "$@"
}

list_models_json() {
  curl_api "${API_BASE}/v1/models"
}

extract_ids() {
  local raw=$1
  if have jq; then
    echo "$raw" | jq -r '
      if type=="object" and (.data|type)=="array" then .data[]?.id // empty
      elif type=="array" then .[] | (if type=="object" then .id else . end) // empty
      else empty end
    ' | sed '/^null$/d;/^$/d' | sort -u
  else
    echo "$raw" | grep -oE '"id"[[:space:]]*:[[:space:]]*"[^"]+"' \
      | sed -E 's/.*"([^"]+)"[[:space:]]*$/\1/' | sort -u
  fi
}

# 打印: OK|code|snippet  /  FAIL|code|snippet  /  ERROR|0|msg
probe_one() {
  local model=$1
  local me prompt bodyf code snip lower payload

  me=$(json_escape "$model")
  prompt=$(json_escape "$PROBE_PROMPT")
  payload=$(printf '{"model":%s,"messages":[{"role":"user","content":%s}],"max_tokens":%s,"stream":false}' \
    "$me" "$prompt" "$PROBE_MAX_TOKENS")

  bodyf=$(mktemp)
  code=0
  if ! code=$(curl_api -o "$bodyf" -w '%{http_code}' \
      -X POST "${API_BASE}/v1/chat/completions" -d "$payload" 2>/dev/null); then
    rm -f "$bodyf"
    if [[ "$EXCLUDE_ON_NETWORK_ERROR" -eq 1 ]]; then
      echo "ERROR|0|curl_failed"
    else
      echo "SKIP|0|curl_failed"
    fi
    return 0
  fi

  # chat 404 时试 responses
  if [[ "$code" == "404" ]]; then
    payload=$(printf '{"model":%s,"input":%s,"stream":false}' "$me" "$prompt")
    code=$(curl_api -o "$bodyf" -w '%{http_code}' \
      -X POST "${API_BASE}/v1/responses" -d "$payload" 2>/dev/null || echo 0)
  fi

  snip=$(head -c 360 "$bodyf" | tr '\n\r' '  ')
  rm -f "$bodyf"
  lower=$(printf '%s' "$snip" | tr '[:upper:]' '[:lower:]')

  if [[ "$code" =~ ^2 ]]; then
    if [[ -n "$FAIL_BODY_RE" ]] && echo "$lower" | grep -Eqi "$FAIL_BODY_RE"; then
      echo "FAIL|${code}|${snip}"
    else
      echo "OK|${code}|${snip}"
    fi
    return 0
  fi
  echo "FAIL|${code}|${snip}"
}

get_excluded() {
  curl_mgmt "${MGMT_BASE}/oauth-excluded-models" 2>/dev/null || echo '{}'
}

guess_provider() {
  local m
  m=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  if [[ -n "$FORCE_PROVIDER" ]]; then
    echo "$FORCE_PROVIDER"
    return
  fi
  case "$m" in
    claude*|anthropic*) echo "claude" ;;
    gpt*|o1*|o3*|o4*|codex*|chatgpt*) echo "codex" ;;
    gemini*|gemma*) echo "aistudio" ;;
    grok*|xai*) echo "xai" ;;
    *) echo "$DEFAULT_EXCLUDE_PROVIDER" ;;
  esac
}

# 把 models[] 合并进 provider，PUT 整个 map
put_excluded_merge() {
  local provider=$1
  shift
  local -a add=("$@")

  have jq || die "自动写排除需要 jq（apt install jq / yum install jq）"

  local current map_json new_map code bodyf
  current=$(get_excluded)
  if echo "$current" | jq -e 'type=="object" and has("error")' >/dev/null 2>&1; then
    log "WARN: 读排除表失败: $(echo "$current" | head -c 200) ，使用空表"
    current='{}'
  fi

  map_json=$(echo "$current" | jq -c '
    if type=="object" and has("oauth-excluded-models") then .["oauth-excluded-models"]
    elif type=="object" and has("items") then .items
    elif type=="object" then .
    else {} end
  ')

  new_map=$(echo "$map_json" | jq -c --arg p "$provider" --args '
    . as $m
    | (($m[$p] // []) + $ARGS.positional | unique) as $list
    | $m + {($p): $list}
  ' "${add[@]}")

  log "合并排除 provider=$provider <- ${add[*]}"
  echo "$new_map" | jq . >&2 || true

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "[dry-run] 不 PUT"
    return 0
  fi

  bodyf=$(mktemp)
  code=$(curl_mgmt -o "$bodyf" -w '%{http_code}' \
    -X PUT "${MGMT_BASE}/oauth-excluded-models" \
    -d "$new_map" || echo 0)
  log "PUT oauth-excluded-models -> HTTP $code $(head -c 200 "$bodyf")"
  rm -f "$bodyf"
  [[ "$code" =~ ^2 ]] || die "写入排除失败 HTTP $code（检查 MANAGEMENT_KEY / allow-remote / 路径）"
}

# ----- main -----

log "API_BASE=$API_BASE dry_run=$DRY_RUN"

if [[ "$SHOW_EXCLUDED" -eq 1 ]]; then
  log "当前 oauth-excluded-models:"
  get_excluded | (have jq && jq . || cat)
  echo
fi

log "GET ${API_BASE}/v1/models ..."
RAW=$(list_models_json) || die "无法访问 /v1/models（API_BASE / API_KEY / 服务是否启动）"

mapfile -t ALL_MODELS < <(extract_ids "$RAW")
if [[ ${#ALL_MODELS[@]} -eq 0 ]]; then
  log "响应片段: $(echo "$RAW" | head -c 400)"
  die "未解析到模型 id（建议安装 jq）"
fi

log "在线模型 ${#ALL_MODELS[@]} 个:"
printf '  - %s\n' "${ALL_MODELS[@]}" >&2

if [[ "$LIST_ONLY" -eq 1 ]]; then
  printf '%s\n' "${ALL_MODELS[@]}"
  exit 0
fi

MODELS=()
if [[ ${#ONLY_MODELS[@]} -gt 0 ]]; then
  MODELS=("${ONLY_MODELS[@]}")
  log "仅探测指定 ${#MODELS[@]} 个模型"
else
  MODELS=("${ALL_MODELS[@]}")
fi

OK_LIST=()
FAIL_LIST=()

for m in "${MODELS[@]}"; do
  r=$(probe_one "$m")
  kind=${r%%|*}
  rest=${r#*|}
  code=${rest%%|*}
  snip=${rest#*|}
  case "$kind" in
    OK)
      log "OK   $m  http=$code"
      OK_LIST+=("$m")
      ;;
    FAIL|ERROR)
      log "FAIL $m  http=$code  $snip"
      FAIL_LIST+=("$m")
      ;;
    *)
      log "SKIP $m  $r"
      ;;
  esac
done

log "======== 汇总 OK=${#OK_LIST[@]} FAIL=${#FAIL_LIST[@]} ========"
[[ ${#OK_LIST[@]} -gt 0 ]] && printf '  OK:   %s\n' "${OK_LIST[@]}" >&2
[[ ${#FAIL_LIST[@]} -gt 0 ]] && printf '  FAIL: %s\n' "${FAIL_LIST[@]}" >&2

if [[ ${#FAIL_LIST[@]} -eq 0 ]]; then
  log "没有失败模型，结束"
  exit 0
fi

# 按 provider 分组
# bash 3.2 兼容：用临时文件分组
GROUP_DIR=$(mktemp -d)
trap 'rm -rf "$GROUP_DIR"' EXIT

for m in "${FAIL_LIST[@]}"; do
  p=$(guess_provider "$m")
  printf '%s\n' "$m" >> "$GROUP_DIR/$p"
done

for gf in "$GROUP_DIR"/*; do
  [[ -f "$gf" ]] || continue
  prov=$(basename "$gf")
  mapfile -t arr < "$gf"
  # 去重
  mapfile -t arr < <(printf '%s\n' "${arr[@]}" | sort -u)
  put_excluded_merge "$prov" "${arr[@]}"
done

log "完成。核对: $0 --show-excluded"
log "客户端请重新请求 GET /v1/models"
exit 0
