#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "lark-oapi>=1.5.0",
# ]
# ///

"""
Feishu bot startup protocol for Bub (SDK mode).

Wraps lark-oapi SDK ws.Client into a persistent inbound listener,
parses messages into Bub ChannelMessage format, and routes them
into the conversation system.

Usage:
    python feishu_startup.py [--quiet] [--card-actions]

Design: zero enforcement. This script only receives and normalizes.
The AI decides whether and how to respond.
"""
import argparse
import json
import os
import sys

from lark_oapi.ws import Client as WsClient
from lark_oapi.core.enum import LogLevel
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

MSG_EVENT = "im.message.receive_v1"
CARD_EVENT = "card.action.trigger"

APP_ID = os.environ.get("BUB_LARK_APP_ID", os.environ.get("LARK_APP_ID", ""))
APP_SECRET = os.environ.get("BUB_LARK_APP_SECRET", os.environ.get("LARK_APP_SECRET", ""))


def _to_plain(obj):
    """Recursively convert SDK model objects to plain Python types."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_to_plain(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return {k: _to_plain(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)


def _extract_text(content: str, msg_type: str) -> str:
    """Extract plain text from Feishu message content JSON."""
    if msg_type != "text" or not content:
        return content
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed.get("text", content)
    except (json.JSONDecodeError, TypeError):
        pass
    return content


def parse_message_event(event) -> dict:
    """Normalize a Feishu event into Bub-style message envelope."""
    msg = event.event.message if event.event else None
    sender = event.event.sender if event.event else None
    header = event.header

    sender_id = None
    if sender and sender.sender_id:
        sender_id = sender.sender_id.open_id or sender.sender_id.union_id or sender.sender_id.user_id

    msg_type = msg.message_type if msg else "unknown"
    raw_content = msg.content if msg else ""
    content = _extract_text(raw_content, msg_type)
    chat_type = msg.chat_type if msg else "unknown"
    chat_id = msg.chat_id if msg else ""
    message_id = msg.message_id if msg else ""
    create_time = str(msg.create_time) if msg and msg.create_time else ""
    event_id = header.event_id if header else ""

    # Session ID format: feishu:<chat_id>
    session_id = f"feishu:{chat_id}" if chat_id else ""

    # Activation heuristics (AI decides, we just expose)
    is_p2p = chat_type == "p2p"
    has_mention = "@" in str(content)
    is_command = str(content).startswith(",")

    return {
        "channel": "$feishu",
        "session_id": session_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "message_id": message_id,
        "message_type": msg_type,
        "content": content,
        "create_time": create_time,
        "event_id": event_id,
        "is_p2p": is_p2p,
        "has_mention": has_mention,
        "is_command": is_command,
        "raw": _to_plain(event),
    }


def parse_card_event(event) -> dict:
    """Normalize a card action into Bub-style envelope."""
    action = event.event.action if event.event else {}
    context = event.event.context if event.event else {}
    header = event.header
    sender_id = event.event.open_id if event.event else None

    chat_id = context.get("open_chat_id") if isinstance(context, dict) else None
    message_id = context.get("open_message_id") if isinstance(context, dict) else None

    return {
        "channel": "$feishu",
        "session_id": f"feishu:{chat_id}" if chat_id else "",
        "chat_id": chat_id,
        "chat_type": "card_action",
        "sender_id": sender_id,
        "message_id": message_id,
        "message_type": "card_action",
        "content": action.get("value") if isinstance(action, dict) else None,
        "is_p2p": False,
        "has_mention": False,
        "is_command": False,
        "event_id": header.event_id if header else None,
        "raw": _to_plain(event),
    }


def handle_event(event):
    """Process a single event and emit JSON to stdout."""
    event_type = event.header.event_type if event.header else ""
    if event_type == CARD_EVENT or "card.action" in event_type:
        envelope = parse_card_event(event)
    else:
        envelope = parse_message_event(event)
    print(json.dumps(envelope, ensure_ascii=False), flush=True)


def run():
    parser = argparse.ArgumentParser(description="Feishu bot startup listener (SDK mode)")
    parser.add_argument("--quiet", action="store_true", help="Suppress SDK log messages")
    parser.add_argument("--card-actions", action="store_true", help="Also listen for card.action.trigger events")
    args = parser.parse_args()

    if not APP_ID or not APP_SECRET:
        print(
            json.dumps(
                {"startup": "feishu", "status": "error", "error": "Missing BUB_LARK_APP_ID or BUB_LARK_APP_SECRET"}
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    log_level = LogLevel.WARNING if args.quiet else LogLevel.INFO
    handler = EventDispatcherHandler()
    builder = handler.builder(encrypt_key="", verification_token="")

    builder.register_p2_im_message_receive_v1(lambda ctx, event: handle_event(event))
    if args.card_actions:
        builder.register_p2_card_action_trigger(lambda ctx, event: handle_event(event))

    handler = builder.build()

    client = WsClient(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        log_level=log_level,
        event_handler=handler,
        auto_reconnect=True,
    )

    # Print startup protocol header so parent process knows we're ready
    print(
        json.dumps(
            {"startup": "feishu", "status": "connecting", "event_key": MSG_EVENT},
            ensure_ascii=False,
        ),
        flush=True,
    )

    try:
        client.start()
    except KeyboardInterrupt:
        pass
    finally:
        print(
            json.dumps(
                {"startup": "feishu", "status": "disconnected"},
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    run()