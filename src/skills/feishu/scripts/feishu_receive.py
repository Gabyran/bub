#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "lark-oapi>=1.5.0",
# ]
# ///

"""
Feishu real-time event receiver using lark-oapi SDK ws.Client.
Reads im.message.receive_v1 events and emits parsed inbound messages.

Usage:
    python feishu_receive.py [--quiet] [--card-actions]

Output (stdout, one JSON per line):
    {
        "channel": "$feishu",
        "chat_id": "oc_xxx",
        "chat_type": "p2p|group",
        "sender_id": "ou_xxx",
        "message_id": "om_xxx",
        "message_type": "text|post|image|file|...",
        "content": "...",
        "create_time": "1234567890000",
        "event_id": "...",
        "raw": { ... }
    }

Design principle: zero enforcement. This script only RECEIVES and PARSES.
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
    """Parse im.message.receive_v1 into normalized envelope."""
    msg = event.event.message if event.event else None
    sender = event.event.sender if event.event else None
    header = event.header

    sender_id = None
    if sender and sender.sender_id:
        sender_id = sender.sender_id.open_id or sender.sender_id.union_id or sender.sender_id.user_id

    raw_content = msg.content if msg else None
    msg_type = msg.message_type if msg else None
    content = _extract_text(raw_content, msg_type)

    return {
        "channel": "$feishu",
        "event_type": "message",
        "chat_id": msg.chat_id if msg else None,
        "chat_type": msg.chat_type if msg else None,
        "sender_id": sender_id,
        "message_id": msg.message_id if msg else None,
        "message_type": msg_type,
        "content": content,
        "create_time": str(msg.create_time) if msg and msg.create_time else None,
        "event_id": header.event_id if header else None,
        "raw": _to_plain(event),
    }


def parse_card_event(event) -> dict:
    """Parse card.action.trigger into normalized envelope."""
    action = event.event.action if event.event else {}
    context = event.event.context if event.event else {}
    header = event.header

    return {
        "channel": "$feishu",
        "event_type": "card_action",
        "chat_id": context.get("open_chat_id") if isinstance(context, dict) else None,
        "chat_type": context.get("open_message_id") if isinstance(context, dict) else None,
        "sender_id": event.event.open_id if event.event else None,
        "message_id": context.get("open_message_id") if isinstance(context, dict) else None,
        "message_type": "card_action",
        "content": action.get("value") if isinstance(action, dict) else None,
        "action_tag": action.get("tag") if isinstance(action, dict) else None,
        "action_option": action.get("option") if isinstance(action, dict) else None,
        "create_time": str(header.create_time) if header and header.create_time else None,
        "event_id": header.event_id if header else None,
        "raw": _to_plain(event),
    }


def handle_event(event):
    """Process a single event and emit JSON to stdout."""
    event_type = event.header.event_type if event.header else ""
    if event_type == CARD_EVENT or "card.action" in event_type:
        parsed = parse_card_event(event)
    else:
        parsed = parse_message_event(event)
    print(json.dumps(parsed, ensure_ascii=False), flush=True)


def run():
    parser = argparse.ArgumentParser(description="Feishu inbound message receiver (SDK mode)")
    parser.add_argument("--timeout", default="0", help="Ignored in SDK mode (legacy compat)")
    parser.add_argument("--max-events", type=int, default=0, help="Ignored in SDK mode (legacy compat)")
    parser.add_argument("--quiet", action="store_true", help="Reduce log verbosity")
    parser.add_argument("--card-actions", action="store_true", help="Also listen for card.action.trigger events")
    args = parser.parse_args()

    if not APP_ID or not APP_SECRET:
        print(
            json.dumps({"error": "BUB_LARK_APP_ID and BUB_LARK_APP_SECRET must be set"}),
            file=sys.stderr,
        )
        sys.exit(1)

    log_level = LogLevel.WARNING if args.quiet else LogLevel.INFO
    handler = EventDispatcherHandler()
    builder = handler.builder(encrypt_key="", verification_token="")

    builder.register_p2_im_message_receive_v1(lambda event: handle_event(event))
    if args.card_actions:
        builder.register_p2_card_action_trigger(lambda event: handle_event(event))

    handler = builder.build()

    client = WsClient(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        log_level=log_level,
        event_handler=handler,
        auto_reconnect=True,
    )

    try:
        client.start()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()