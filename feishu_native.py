#!/app/.venv/bin/python3
"""
Feishu native event loop for Bub - using lark-oapi SDK ws.Client.

Replaces the previous lark-cli subprocess approach with native SDK WebSocket
for improved stability, auto-reconnect, and heartbeat management.

Usage:
    python feishu_native.py

Design: AI self-bootstrapping. No channel listener, no bridge.
External event → AI wake-up → AI uses skill → AI exits.
"""

import base64
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from lark_oapi.ws import Client
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.core.enum import LogLevel

EVENT_KEY = "im.message.receive_v1"
BUB_BIN = os.environ.get("BUB_BIN", "/app/.venv/bin/bub")
RUN_LOG = Path(os.environ.get("FEISHU_RUN_LOG", "/data/feishu-native/runs.log"))
STATE_DIR = RUN_LOG.parent
MEDIA_DIR = Path("/data/feishu-native/media")
LOCK_FILE = STATE_DIR / "consumer.lock"

APP_ID: str = ""
APP_SECRET: str = ""
PROVIDER_ENV_KEYS = (
    "BUB_MODEL",
    "BUB_API_KEY",
    "BUB_API_BASE",
    "BUB_API_FORMAT",
    "BUB_CLIENT_ARGS",
    "BUB_FALLBACK_MODELS",
)


def setup():
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "seen").mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "error-sent").mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return subprocess.check_output(["date", "-Is"]).decode().strip()


def log_line(message: str) -> None:
    with RUN_LOG.open("a", encoding="utf-8") as log:
        log.write(f"[{now_iso()}] {message}\n")


def bub_run_env() -> dict[str, str]:
    """Return a subprocess env that lets each bub run reload provider config."""
    env = dict(os.environ)
    for key in PROVIDER_ENV_KEYS:
        env.pop(key, None)
    return env


@contextlib.contextmanager
def single_consumer_lock():
    """Keep one Feishu consumer process active per state directory."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log_line("another feishu_native.py instance is already running; exiting")
            print("[feishu_native] Another consumer is already running. Exiting.", flush=True)
            yield False
            return
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()} started_at={now_iso()}\n")
        lock_file.flush()
        try:
            yield True
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def parse_ws_event(payload: bytes) -> dict:
    """Parse WebSocket event payload from lark-oapi SDK into normalized event."""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}

    # Already in lark-cli simplified format?
    if "chat_id" in data and "message_type" in data:
        return {
            "chat_id": data.get("chat_id", ""),
            "chat_type": data.get("chat_type", ""),
            "sender_id": data.get("sender_id", ""),
            "message_id": data.get("message_id") or data.get("id", ""),
            "message_type": data.get("message_type", ""),
            "content": data.get("content", ""),
            "create_time": str(data.get("create_time", "")),
            "event_id": data.get("event_id", ""),
            "raw": data,
        }

    # --- v2 schema (SDK WebSocket) ---
    schema = data.get("schema", "")
    header = data.get("header", {})
    event_data = data.get("event", {})

    event_type = ""
    if schema == "2.0":
        event_type = header.get("event_type", "")
    elif data.get("uuid"):
        event_type = data.get("type", "")

    if "im.message.receive_v1" not in event_type:
        return {}

    message = event_data.get("message", {})
    sender = event_data.get("sender", {})
    sender_id = ""
    if isinstance(sender, dict):
        sender_id_obj = sender.get("sender_id", {})
        if isinstance(sender_id_obj, dict):
            sender_id = sender_id_obj.get("union_id", "") or sender_id_obj.get("open_id", "")
        else:
            sender_id = str(sender_id_obj)

    chat_id = message.get("chat_id", "")
    if not chat_id:
        chat = event_data.get("chat", {})
        chat_id = chat.get("chat_id", "")

    return {
        "chat_id": chat_id,
        "chat_type": event_data.get("chat_type", ""),
        "sender_id": sender_id,
        "message_id": message.get("message_id", ""),
        "message_type": message.get("message_type", ""),
        "content": message.get("content", ""),
        "create_time": str(message.get("create_time", "")),
        "event_id": header.get("event_id", "") if schema == "2.0" else data.get("uuid", ""),
        "raw": data,
    }


def _safe_key(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in value)


def _event_dedupe_key(event: dict) -> str:
    event_id = str(event.get("event_id") or "").strip()
    if event_id:
        return f"event_{_safe_key(event_id)}"
    message_id = str(event.get("message_id") or "").strip()
    if message_id:
        return f"message_{_safe_key(message_id)}"
    return ""


def _claim_event(event: dict) -> tuple[bool, str]:
    """Atomically claim a Feishu event before waking the AI."""
    key = _event_dedupe_key(event)
    if not key:
        return False, ""
    seen_dir = STATE_DIR / "seen"
    marker = seen_dir / key
    try:
        marker.touch(exist_ok=False)
        return True, key
    except FileExistsError:
        return False, key


def download_media(message_id: str, file_key: str, media_type: str = "image") -> bytes | None:
    """Download image/file from Feishu message using lark-cli."""
    output_path = MEDIA_DIR / f"{media_type}_{file_key}"
    cmd = [
        "lark-cli", "im", "+messages-resources-download",
        "--as", "bot",
        "--message-id", message_id,
        "--file-key", file_key,
        "--type", media_type,
        "--output", str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    downloaded = None
    if output_path.exists():
        downloaded = output_path
    else:
        for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".docx"]:
            candidate = output_path.with_suffix(ext)
            if candidate.exists():
                downloaded = candidate
                break
    if downloaded is None:
        return None
    try:
        data = downloaded.read_bytes()
        downloaded.unlink(missing_ok=True)
        return data
    except OSError:
        return None


def extract_media_info(event: dict) -> dict | None:
    """Extract image/file key from Feishu message content."""
    msg_type = event.get("message_type", "")
    content_str = event.get("content", "")
    if not content_str:
        return None
    try:
        content = json.loads(content_str)
    except json.JSONDecodeError:
        return None

    if msg_type == "image":
        image_key = content.get("image_key")
        if image_key:
            return {"type": "image", "key": image_key}
    elif msg_type == "file":
        file_key = content.get("file_key")
        if file_key:
            return {"type": "file", "key": file_key, "name": content.get("file_name", "unknown")}
    elif msg_type == "audio":
        file_key = content.get("file_key")
        if file_key:
            return {"type": "audio", "key": file_key}
    return None


def build_system_layer() -> str:
    """Layer 1: System Prompt — identity, rules, available tools/skills.

    This layer is static (does not change per event). It defines who the AI
    is, what tools it has, and how it should behave.  The persona comes from
    AGENTS.md (loaded by Bub's system_prompt hook), so this layer only adds
    Feishu-specific behavioural guidance.
    """
    return (
        "<general_instruct>\n"
        "Call tools or skills to finish the task.\n"
        "</general_instruct>\n"
        "<response_instruct>\n"
        "Before ending this run, you MUST determine whether a response needs to be sent via channel.\n"
        "When responding to a channel message, identify the channel from the message metadata (e.g., $feishu).\n"
        "Send your message as instructed by the channel skill (e.g., feishu-send skill for $feishu).\n"
        "</response_instruct>\n"
        "<available_tools>\n"
        "- bash: Run a shell command.\n"
        "- fs_read / fs_write / fs_edit: File operations.\n"
        "- skill: Load a skill by name.\n"
        "- tape_info / tape_search / tape_handoff: Tape management.\n"
        "- web_fetch: Fetch web content.\n"
        "- subagent: Spawn a sub-agent.\n"
        "</available_tools>\n"
        "<available_skills>\n"
        "- feishu-send: Send messages, cards, images, reactions to Feishu.\n"
        "- feishu-ws-receiver: Receive Feishu messages (this is the receiver side).\n"
        "</available_skills>\n"
        "逗号命令：你可以用 ,xxx 的形式直接调用工具/技能，例如：\n"
        "- ,help — 查看可用命令\n"
        "- ,tape_info — 查看当前会话状态\n"
        "- ,bash <命令> — 执行 shell 命令\n"
        "如果是逗号命令，直接执行并回复结果；否则正常聊天。\n"
    )


def build_context_layer(event: dict) -> str:
    """Layer 2: Context Layer — conversation metadata, sender info, channel state.

    This layer carries the *who, where, when* of the event.  It is stable
    across multiple events in the same session, but may change between
    sessions.  Keeping it separate from the raw event makes prompt caching
    more effective.
    """
    chat_id = event.get("chat_id", "")
    chat_type = event.get("chat_type", "")
    sender_id = event.get("sender_id", "")
    session_id = f"feishu:{chat_id}" if chat_id else "feishu:default"

    return (
        f"<channel>$feishu</channel>\n"
        f"<chat_id>{chat_id}</chat_id>\n"
        f"<chat_type>{chat_type}</chat_type>\n"
        f"<sender_id>{sender_id}</sender_id>\n"
        f"<session_id>{session_id}</session_id>\n"
        f"<event_time>{event.get('create_time', '')}</event_time>\n"
        "<context_contract>\n"
        "Excessively long context may cause model call failures. "
        "Use tape.info to check token usage and tape.handoff to shorten history.\n"
        "</context_contract>\n"
    )


def build_event_layer(event: dict) -> str:
    """Layer 3: Event Layer — the actual message payload.

    This layer contains the *what* that changed: the message content,
    attachments, and any media that needs to be processed.
    """
    msg_type = event.get("message_type", "")
    message_id = event.get("message_id", "")
    media_data = None

    # Try to download media if present
    media_info = extract_media_info(event)
    if media_info:
        downloaded = download_media(
            event.get("message_id", ""),
            media_info["key"],
            media_info["type"]
        )
        if downloaded:
            media_data = downloaded
            event["media_downloaded"] = True
            event["media_type"] = media_info["type"]
            event["media_size"] = len(downloaded)

    event_json = json.dumps(event, ensure_ascii=False, indent=2)

    layer = f"<event_type>im.message.receive_v1</event_type>\n"
    layer += f"<message_id>{message_id}</message_id>\n"
    layer += f"<message_type>{msg_type}</message_type>\n"

    if msg_type in ("image", "file", "audio"):
        layer += (
            f"<note>This message contains a {msg_type} attachment. "
            "The raw content shows the file/image key only. "
            "You cannot directly view the attachment content.</note>\n"
        )

    layer += (
        "<event_payload>\n"
        f"```json\n{event_json}\n```\n"
        "</event_payload>\n"
    )

    return layer, media_data


def build_prompt(event: dict) -> tuple[str, bytes | None]:
    """Build the Feishu wake-up prompt as three layers.

    Returns (prompt_string, media_data_or_None).
    """
    system_layer = build_system_layer()
    context_layer = build_context_layer(event)
    event_layer, media_data = build_event_layer(event)

    prompt = (
        "<system_prompt>\n"
        f"{system_layer}"
        "</system_prompt>\n\n"
        "<context>\n"
        f"{context_layer}"
        "</context>\n\n"
        "<event>\n"
        f"{event_layer}"
        "</event>\n"
    )

    return prompt, media_data


def send_feishu_error(chat_id: str, message: str) -> None:
    """Send an error notification to Feishu."""
    try:
        subprocess.run(
            [
                sys.executable,
                os.path.expanduser("~/.agents/skills/feishu-send/scripts/feishu_send.py"),
                "--chat-id", chat_id,
                "--content", message,
            ],
            capture_output=True,
        )
    except Exception:
        pass


def run_bub_for_event(event: dict) -> bool:
    """Spawn bub run for a Feishu event with retry."""
    chat_id = event.get("chat_id", "")
    sender_id = event.get("sender_id", "unknown")
    message_id = event.get("message_id", "")
    session_id = f"feishu:{chat_id}" if chat_id else "feishu:default"
    event_id = event.get("event_id", "")

    prompt, media_data = build_prompt(event)
    max_retries = 2

    with RUN_LOG.open("a", encoding="utf-8") as log:
        log.write(
            f"\n[{now_iso()}] "
            f"event_id={event_id} chat_id={chat_id} message_id={message_id} "
            f"msg_type={event.get('message_type', '')}\n"
        )
        log.flush()

        last_error = ""
        for attempt in range(max_retries + 1):
            try:
                result = subprocess.run(
                    [
                        BUB_BIN, "run",
                        "--channel", "feishu",
                        "--chat-id", chat_id,
                        "--sender-id", sender_id,
                        "--session-id", session_id,
                        prompt,
                    ],
                    capture_output=True,
                    env=bub_run_env(),
                    text=True,
                )
                log.write(f"exit_code={result.returncode} attempt={attempt}\n")
                if result.returncode != 0:
                    log.write(f"stderr={result.stderr[:500]}\n")
                    last_error = f"exit_code={result.returncode}"
                    if attempt < max_retries:
                        log.write("retrying in 3s...\n")
                        log.flush()
                        time.sleep(3)
                        continue
                else:
                    last_error = ""
                log.flush()
                break
            except Exception as e:
                log.write(f"ERROR attempt={attempt} exception={e}\n")
                last_error = str(e)
                if attempt < max_retries:
                    log.write("retrying in 3s...\n")
                    log.flush()
                    time.sleep(3)
                    continue
                log.flush()
                break

        # Note: comma command stdout is NOT echoed back here.
        # Bub's agent loop handles responses via the feishu-send skill.
        # Echoing stdout would cause double replies.

        if last_error:
            log.write(f"final_error={last_error}, sending notification\n")
            log.flush()
            send_feishu_error(
                chat_id,
                "⚠️ 请求处理失败，喵喵没来得及回复。请稍后再试。"
            )
            return False
        return True


class RawEventHandler(EventDispatcherHandler):
    """Custom event handler that processes raw WebSocket payloads.

    Inherits from EventDispatcherHandler so the SDK recognises it as a
    legitimate handler (duck-typing would work, but inheritance makes the
    contract explicit and avoids silent skips).
    """

    def __init__(self) -> None:
        # EventDispatcherHandler has no required constructor args
        super().__init__()

    def _do_without_validation(self, payload: bytes) -> None:
        """Called by lark-oapi ws.Client for each event message."""
        try:
            event = parse_ws_event(payload)
            chat_id = event.get("chat_id", "")
            if not chat_id:
                log_line("skip: no chat_id in event")
                return None

            claimed, dedupe_key = _claim_event(event)
            if not claimed:
                log_line(f"skip Feishu event: duplicate dedupe_key={dedupe_key}")
                return None

            ok = run_bub_for_event(event)
            status = "done" if ok else "failed"
            log_line(f"event {event.get('event_id', '')} handled: {status}")
        except Exception as e:
            log_line(f"handler exception: {e}")
        return None


def _load_credentials():
    """Load Lark app credentials from environment or config file."""
    global APP_ID, APP_SECRET

    APP_ID = os.environ.get("BUB_LARK_APP_ID", "")
    APP_SECRET = os.environ.get("BUB_LARK_APP_SECRET", "")

    if not APP_ID or not APP_SECRET:
        try:
            import yaml
            config_path = Path("/workspace/config.yml")
            if config_path.exists():
                with config_path.open("r", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                lark_cfg = config.get("lark", {})
                APP_ID = lark_cfg.get("app_id", "")
                APP_SECRET = lark_cfg.get("app_secret", "")
        except Exception:
            pass

    if not APP_ID or not APP_SECRET:
        print(
            "[feishu_native] ERROR: Missing Lark app credentials. "
            "Set BUB_LARK_APP_ID and BUB_LARK_APP_SECRET, "
            "or add lark.app_id and lark.app_secret to /workspace/config.yml",
            flush=True,
        )
        sys.exit(1)


def consume_loop():
    """Main WebSocket consumer loop using lark-oapi SDK."""
    _load_credentials()

    handler = RawEventHandler()

    print("[feishu_native] Starting lark-oapi ws.Client...", flush=True)

    while True:
        try:
            client = Client(
                app_id=APP_ID,
                app_secret=APP_SECRET,
                event_handler=handler,
                auto_reconnect=True,
                log_level=LogLevel.INFO,
            )
        except Exception as e:
            print(f"[feishu_native] Failed to create ws.Client: {e}. Retrying in 10s...", flush=True)
            time.sleep(10)
            continue

        ws_thread = threading.Thread(target=client.start, daemon=True)
        ws_thread.start()

        print("[feishu_native] ws.Client started in background thread", flush=True)

        try:
            while ws_thread.is_alive():
                time.sleep(5)
        except KeyboardInterrupt:
            print("[feishu_native] Shutting down.", flush=True)
            break

        if not ws_thread.is_alive():
            print("[feishu_native] ws.Client thread died. Restarting in 5s...", flush=True)
            time.sleep(5)
            continue

    print("[feishu_native] Consumer loop exited.", flush=True)


def main():
    setup()
    with single_consumer_lock() as acquired:
        if not acquired:
            return
        consume_loop()


if __name__ == "__main__":
    main()
