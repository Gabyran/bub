#!/usr/bin/env bash

set -Eeuo pipefail

BUB_BIN="${BUB_BIN:-/app/.venv/bin/bub}"
PYTHON_BIN="${PYTHON_BIN:-/app/.venv/bin/python}"
WORKSPACE_DIR="${WORKSPACE_DIR:-/workspace}"
export BUB_RUNTIME_SRC="${BUB_RUNTIME_SRC:-${WORKSPACE_DIR}/src/bub-runtime/src}"
STATE_DIR="${BUB_HOME:-/data}/telegram-native"
FEISHU_STATE_DIR="${BUB_HOME:-/data}/feishu-native"
OFFSET_FILE="${STATE_DIR}/offset"
RUN_LOG="${STATE_DIR}/runs.log"
FEISHU_RUN_LOG="${FEISHU_STATE_DIR}/runs.log"
OPENCLI_DAEMON_PORT="${OPENCLI_DAEMON_PORT:-19825}"
OPENCLI_HOST_DAEMON_HOST="${OPENCLI_HOST_DAEMON_HOST:-host.docker.internal}"
OPENCLI_HOST_DAEMON_PORT="${OPENCLI_HOST_DAEMON_PORT:-${OPENCLI_DAEMON_PORT}}"
OPENCLI_FORWARD_LOG="${BUB_HOME:-/data}/opencli-forwarder.log"

mkdir -p "${STATE_DIR}"
cd "${WORKSPACE_DIR}"
if [[ -d "${BUB_RUNTIME_SRC}/bub" ]]; then
  export PYTHONPATH="${BUB_RUNTIME_SRC}${PYTHONPATH:+:${PYTHONPATH}}"
fi

load_config_env() {
  eval "$("${PYTHON_BIN}" - <<'PY'
from pathlib import Path
import json
import shlex
import yaml

config_path = Path("/workspace/config.yml")
data = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
data = data or {}
telegram = data.get("telegram") or {}
lark = data.get("lark") or {}

exports = {}
if telegram.get("token"):
    exports["BUB_TELEGRAM_TOKEN"] = str(telegram["token"])
if telegram.get("allow_users"):
    exports["BUB_TELEGRAM_ALLOW_USERS"] = str(telegram["allow_users"])
if telegram.get("allow_chats"):
    exports["BUB_TELEGRAM_ALLOW_CHATS"] = str(telegram["allow_chats"])
if lark.get("app_id"):
    exports["BUB_LARK_APP_ID"] = str(lark["app_id"])
if lark.get("app_secret"):
    exports["BUB_LARK_APP_SECRET"] = str(lark["app_secret"])
if lark.get("brand"):
    exports["BUB_LARK_BRAND"] = str(lark["brand"])
# Nowledge Mem config
nmem = data.get("nmem") or {}
if nmem.get("api_url"):
    exports["NMEM_API_URL"] = str(nmem["api_url"])

for key, value in exports.items():
    print(f"export {key}={shlex.quote(value)}")
PY
  )"
}

bub_run_env() {
  env \
    -u BUB_MODEL \
    -u BUB_API_KEY \
    -u BUB_API_BASE \
    -u BUB_API_FORMAT \
    -u BUB_CLIENT_ARGS \
    -u BUB_FALLBACK_MODELS \
    "$@"
}

setup_lark_cli() {
  if [[ -z "${BUB_LARK_APP_ID:-}" || -z "${BUB_LARK_APP_SECRET:-}" ]]; then
    return 0
  fi

  # Always refresh the bot app config from config.yml. The container may keep an
  # old /root/.lark-cli/config.json after the app secret is rotated.
  printf '%s' "${BUB_LARK_APP_SECRET}" \
    | lark-cli config init \
        --app-id "${BUB_LARK_APP_ID}" \
        --app-secret-stdin \
        --brand "${BUB_LARK_BRAND:-feishu}" \
        --force-init \
        >/dev/null
}

telegram_api() {
  local method="$1"
  shift
  local stderr_file rc
  stderr_file="$(mktemp)"
  if curl -fsS \
    --retry 5 \
    --retry-all-errors \
    --retry-delay 2 \
    --connect-timeout 10 \
    --max-time 75 \
    "$@" "https://api.telegram.org/bot${BUB_TELEGRAM_TOKEN}/${method}" \
    2>"${stderr_file}"; then
    rm -f "${stderr_file}"
    return 0
  else
    rc=$?
    echo "$(date -Is) startup.sh: telegram ${method} failed after retries: $(tr '\n' ' ' < "${stderr_file}")" >&2
    rm -f "${stderr_file}"
    return "${rc}"
  fi
}

csv_contains() {
  local csv="$1"
  local needle="$2"
  [[ -z "${csv}" ]] && return 1
  IFS=',' read -r -a items <<< "${csv}"
  for item in "${items[@]}"; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [[ "${item}" == "${needle}" ]] && return 0
  done
  return 1
}

is_allowed() {
  local message_json="$1"
  local chat_id sender_id username
  chat_id="$(jq -r '.chat.id | tostring' <<< "${message_json}")"
  sender_id="$(jq -r '.from.id // "" | tostring' <<< "${message_json}")"
  username="$(jq -r '.from.username // ""' <<< "${message_json}")"

  if [[ -n "${BUB_TELEGRAM_ALLOW_CHATS:-}" ]] && ! csv_contains "${BUB_TELEGRAM_ALLOW_CHATS}" "${chat_id}"; then
    return 1
  fi
  if [[ -n "${BUB_TELEGRAM_ALLOW_USERS:-}" ]] \
    && ! csv_contains "${BUB_TELEGRAM_ALLOW_USERS}" "${sender_id}" \
    && ! csv_contains "${BUB_TELEGRAM_ALLOW_USERS}" "${username}"; then
    return 1
  fi
  return 0
}

init_offset() {
  if [[ -s "${OFFSET_FILE}" ]]; then
    return
  fi
  local response latest
  response="$(telegram_api getUpdates --get --data-urlencode "timeout=0" --data-urlencode "limit=1" --data-urlencode "offset=-1" || true)"
  if [[ -z "${response}" ]]; then
    response="{}"
  fi
  latest="$(jq -r '.result[-1].update_id // empty' <<< "${response}")"
  if [[ -n "${latest}" ]]; then
    printf '%s\n' "$((latest + 1))" > "${OFFSET_FILE}"
  else
    printf '0\n' > "${OFFSET_FILE}"
  fi
}

run_bub_for_update() {
  local update_json="$1"
  local message_json chat_id message_id sender_id session_id prompt

  message_json="$(jq -c '.message // empty' <<< "${update_json}")"
  [[ -z "${message_json}" ]] && return 0

  if ! is_allowed "${message_json}"; then
    return 0
  fi

  chat_id="$(jq -r '.chat.id | tostring' <<< "${message_json}")"
  message_id="$(jq -r '.message_id | tostring' <<< "${message_json}")"
  sender_id="$(jq -r '.from.id // "unknown" | tostring' <<< "${message_json}")"
  session_id="telegram:${chat_id}"

  prompt="$(jq -n --argjson update "${update_json}" --arg chat_id "${chat_id}" --arg message_id "${message_id}" '
"你是喵喵，被一条 Telegram 消息吵醒了。\n\n" +
"事情是这样的：\n" +
"- 普通文字只会进日志，不会发到 Telegram。\n" +
"- 如果你决定回消息，记得用 $telegram skill。\n" +
"- 优先回复 message_id=" + $message_id + "，在 chat_id=" + $chat_id + "。\n" +
"- BUB_TELEGRAM_TOKEN 已经在环境变量里了。\n" +
"- 不要跑交互式设置命令。如果缺凭证，就在 Telegram 里问用户要。\n" +
"- 如果是连 Lark/Feishu 的请求，也别跑交互式命令。\n" +
"- 不是所有消息都需要回，自己判断。\n\n" +
"Telegram update JSON:\n```json\n" +
($update | tostring) +
"\n```"
')"


  {
    printf '\n[%s] update_id=%s chat_id=%s message_id=%s\n' \
      "$(date -Is)" \
      "$(jq -r '.update_id' <<< "${update_json}")" \
      "${chat_id}" \
      "${message_id}"
    bub_run_env "${BUB_BIN}" run \
      --channel telegram \
      --chat-id "${chat_id}" \
      --sender-id "${sender_id}" \
      --session-id "${session_id}" \
      "${prompt}"
  } >> "${RUN_LOG}" 2>&1
}

run_feishu_loop() {
  FEISHU_RUN_LOG="${FEISHU_RUN_LOG}" \
  BUB_BIN="${BUB_BIN}" \
    exec "${PYTHON_BIN}" "${WORKSPACE_DIR}/feishu_native.py"
}

start_opencli_forwarder() {
  if ! command -v socat >/dev/null 2>&1; then
    echo "$(date -Is) startup.sh: socat not found; opencli host forwarding disabled" | tee -a "${OPENCLI_FORWARD_LOG}" >&2
    return 0
  fi

  # opencli's daemon client is currently fixed to 127.0.0.1:<port>.
  # Inside Docker, forward that local address to the macOS host daemon so
  # browser commands use the user's real Chrome profile and cookies.
  echo "$(date -Is) startup.sh: forwarding opencli daemon 127.0.0.1:${OPENCLI_DAEMON_PORT} -> ${OPENCLI_HOST_DAEMON_HOST}:${OPENCLI_HOST_DAEMON_PORT}" \
    | tee -a "${OPENCLI_FORWARD_LOG}" >&2
  socat \
    "TCP-LISTEN:${OPENCLI_DAEMON_PORT},bind=127.0.0.1,fork,reuseaddr" \
    "TCP:${OPENCLI_HOST_DAEMON_HOST}:${OPENCLI_HOST_DAEMON_PORT}" \
    >> "${OPENCLI_FORWARD_LOG}" 2>&1 &
}

run_telegram_loop() {
  echo "$(date -Is) startup.sh: native Telegram loop starting"
  init_offset

  while true; do
    offset="$(cat "${OFFSET_FILE}")"
    response="$(telegram_api getUpdates \
      --get \
      --data-urlencode "offset=${offset}" \
      --data-urlencode "timeout=30" \
      --data-urlencode "limit=20" \
      --data-urlencode 'allowed_updates=["message"]' || true)"

    if [[ -z "${response}" ]]; then
      sleep 3
      continue
    fi

    jq -c '.result[]?' <<< "${response}" | while IFS= read -r update_json; do
      next_offset="$(jq -r '.update_id + 1' <<< "${update_json}")"
      printf '%s\n' "${next_offset}" > "${OFFSET_FILE}"
      run_bub_for_update "${update_json}" || true
    done
  done
}

main() {
  load_config_env
  : "${BUB_TELEGRAM_TOKEN:?BUB_TELEGRAM_TOKEN is required}"
  setup_lark_cli
  start_opencli_forwarder

  # Start Feishu loop in background if lark is configured
  if [[ -n "${BUB_LARK_APP_ID:-}" && -n "${BUB_LARK_APP_SECRET:-}" ]]; then
    echo "$(date -Is) startup.sh: starting Feishu native loop in background"
    run_feishu_loop &
  fi

  # Run Telegram loop in foreground (keeps container alive)
  run_telegram_loop
}

main "$@"
