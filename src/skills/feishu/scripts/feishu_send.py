#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "lark-oapi>=1.5.0",
# ]
# ///

"""
Send messages to Feishu (Lark) via lark-oapi SDK.

Replaces lark-cli high-level commands with native SDK calls.

Supports: text, markdown (auto-converted to interactive card), image,
          interactive (card), reply, reaction, card-update.

Usage:
    echo '{"chat_id":"oc_xxx","content":"hello"}' | python feishu_send.py
    python feishu_send.py --chat-id oc_xxx --content "hello"
    python feishu_send.py --chat-id oc_xxx --content "hello" --reply-to om_xxx
    python feishu_send.py --chat-id oc_xxx --image /path/to/img.png
    python feishu_send.py --chat-id oc_xxx --card '{"config":{},"elements":[]}'
    python feishu_send.py --reaction om_xxx --emoji OK

Input (stdin): JSON object with fields:
    - chat_id (required): target chat ID (oc_xxx for groups, ou_xxx for p2p)
    - content (optional): message text / card JSON / post JSON
    - image (optional): local image file path to upload and send
    - reply_to (optional): message_id to reply to
    - msg_type (optional): default "text", also "image","interactive","post"
    - reaction (optional): message_id to add reaction to
    - emoji (optional): emoji key for reaction (default "OK")

Output: JSON response {code, msg, data?}
"""
import argparse
import json
import os
import sys
from typing import Any

from lark_oapi import Client
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
)

APP_ID: str = ""
APP_SECRET: str = ""
client: Client | None = None


def _init_client() -> None:
    global client, APP_ID, APP_SECRET
    if client is not None:
        return
    app_id = os.environ.get("BUB_LARK_APP_ID")
    app_secret = os.environ.get("BUB_LARK_APP_SECRET")
    if not app_id or not app_secret:
        config_path = os.path.expanduser("~/.lark-cli/config.json")
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            apps = cfg.get("apps", [])
            if apps:
                app = apps[0]
                app_id = app.get("appId")
                # app_secret is stored in keychain; cannot read from JSON
    if not app_id or not app_secret:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Missing Lark app credentials. "
                    "Set BUB_LARK_APP_ID and BUB_LARK_APP_SECRET env vars.",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(1)
    APP_ID = app_id
    APP_SECRET = app_secret
    client = Client.builder().app_id(app_id).app_secret(app_secret).build()


def _to_value(obj: Any) -> Any:
    """Recursively convert SDK model objects to plain Python types."""
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, list):
        return [_to_value(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_value(v) for k, v in obj.items()}
    # SDK model instances: iterate public attributes
    if hasattr(obj, "__dict__"):
        d: dict[str, Any] = {}
        for k, v in obj.__dict__.items():
            if k.startswith("_"):
                continue
            d[k] = _to_value(v)
        return d
    return obj


def _to_dict(resp) -> dict:
    """Convert SDK response to plain dict matching lark-cli output shape."""
    result: dict[str, Any] = {"code": resp.code, "msg": resp.msg}
    if resp.data:
        result["data"] = _to_value(resp.data)
    return result


def _ensure_card_v2(card_content: dict | str) -> dict:
    """Ensure card content is in card2.0 format.
    If input is card1.0 (has top-level 'elements'), convert to card2.0 schema.
    If already card2.0 ('schema': '2.0'), return as-is."""
    if isinstance(card_content, str):
        try:
            card = json.loads(card_content)
        except json.JSONDecodeError:
            return card_content  # type: ignore[return-value]
    else:
        card = dict(card_content)

    if not isinstance(card, dict):
        return card_content  # type: ignore[return-value]

    if card.get("schema") == "2.0":
        return card

    # Convert legacy card1.0 to card2.0
    new_card: dict[str, Any] = {"schema": "2.0"}
    if "config" in card:
        new_card["config"] = card["config"]
    if "header" in card:
        new_card["header"] = card["header"]
    if "card_link" in card:
        new_card["card_link"] = card["card_link"]

    elements = card.get("elements")
    if elements is not None:
        new_card["body"] = {"elements": elements}
    elif "body" in card:
        new_card["body"] = card["body"]

    return new_card


def _has_markdown(content: str) -> bool:
    if not content:
        return False
    return bool(
        "```" in content
        or "**" in content
        or "`" in content
        or "#" in content
        or "|" in content
        or "[" in content
    )


def _build_markdown_card(content: str) -> str:
    card = {
        "schema": "2.0",
        "body": {
            "elements": [
                {"tag": "markdown", "content": content}
            ]
        },
    }
    return json.dumps(card, ensure_ascii=False)


def _build_content(content: Any, msg_type: str, image_key: str | None = None) -> str:
    if image_key:
        return json.dumps({"image_key": image_key}, ensure_ascii=False)
    if msg_type == "interactive":
        card = _ensure_card_v2(content)
        return json.dumps(card, ensure_ascii=False)
    if msg_type == "text":
        if isinstance(content, str):
            return json.dumps({"text": content}, ensure_ascii=False)
        return json.dumps(content, ensure_ascii=False)
    if msg_type == "post":
        if isinstance(content, str):
            return json.dumps(
                {"zh_cn": {"title": "", "content": [[{"tag": "text", "text": content}]]}},
                ensure_ascii=False,
            )
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def upload_image(image_path: str) -> str:
    if not os.path.isfile(image_path):
        print(
            json.dumps(
                {"ok": False, "error": f"File not found: {image_path}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(1)
    _init_client()
    with open(image_path, "rb") as f:
        req = CreateImageRequest.builder().request_body(
            CreateImageRequestBody.builder()
            .image(f)
            .image_type("message")
            .build()
        ).build()
        resp = client.im.v1.image.create(req)
    if resp.code != 0:
        print(
            json.dumps(
                {"ok": False, "error": resp.msg, "code": resp.code},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(1)
    return resp.data.image_key


def send_reply(message_id: str, content: Any, msg_type: str = "text", image_key: str | None = None) -> dict:
    _init_client()
    body_content = _build_content(content, msg_type, image_key)
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(body_content)
            .msg_type("image" if image_key else msg_type)
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.reply(req)
    return _to_dict(resp)


def send_message(
    chat_id: str,
    content: str | None = None,
    reply_to: str | None = None,
    msg_type: str = "text",
    image_key: str | None = None,
) -> dict:
    if reply_to:
        return send_reply(reply_to, content, msg_type, image_key)
    _init_client()
    body_content = _build_content(content, msg_type, image_key)
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("image" if image_key else msg_type)
            .content(body_content)
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.create(req)
    return _to_dict(resp)


def update_card(message_id: str, card_content: dict | str) -> dict:
    _init_client()
    card = _ensure_card_v2(card_content)
    req = (
        PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            PatchMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.patch(req)
    return _to_dict(resp)


def add_reaction(message_id: str, emoji_type: str = "OK") -> dict:
    _init_client()
    req = (
        CreateMessageReactionRequest.builder()
        .message_id(message_id)
        .request_body(
            CreateMessageReactionRequestBody.builder()
            .reaction_type({"emoji_type": emoji_type})
            .build()
        )
        .build()
    )
    resp = client.im.v1.message_reaction.create(req)
    return _to_dict(resp)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send Feishu message via lark-oapi SDK")
    parser.add_argument("--chat-id", help="Target chat ID")
    parser.add_argument("--content", help="Message text / card JSON / post JSON")
    parser.add_argument("--reply-to", help="Message ID to reply to")
    parser.add_argument(
        "--msg-type", default="text", help="Message type: text|image|interactive|post"
    )
    parser.add_argument("--image", help="Local image file path to upload and send")
    parser.add_argument(
        "--card", help="Card JSON string (shorthand for --msg-type interactive)"
    )
    parser.add_argument(
        "--update-card", help="Message ID of an existing card to update"
    )
    parser.add_argument("--reaction", help="Message ID to add reaction to")
    parser.add_argument("--emoji", default="OK", help="Emoji type for reaction")
    args = parser.parse_args()

    if args.update_card:
        resp = update_card(args.update_card, args.content or args.card or "{}")
        print(json.dumps(resp, ensure_ascii=False))
        return

    if args.reaction:
        resp = add_reaction(args.reaction, args.emoji)
        print(json.dumps(resp, ensure_ascii=False))
        return

    chat_id = args.chat_id
    content = args.content
    # Read content from stdin if "-" was passed (heredoc pattern)
    if content == "-":
        content = sys.stdin.read()
    msg_type = args.msg_type
    image_key = None

    if args.card:
        msg_type = "interactive"
        content = args.card

    if args.image:
        msg_type = "image"
        image_key = upload_image(args.image)

    # Auto-detect markdown -> wrap in interactive card to preserve formatting
    if content and msg_type == "text" and _has_markdown(content):
        msg_type = "interactive"
        content = _build_markdown_card(content)

    if chat_id and (content or image_key):
        resp = send_message(chat_id, content, args.reply_to, msg_type, image_key)
        print(json.dumps(resp, ensure_ascii=False))
        return

    data = sys.stdin.read().strip()
    if not data:
        print("No input provided", file=sys.stderr)
        sys.exit(1)
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    chat_id = payload.get("chat_id")
    content = payload.get("content")
    msg_type = payload.get("msg_type", "text")
    reply_to = payload.get("reply_to")
    image_path = payload.get("image")
    reaction_msg_id = payload.get("reaction")
    emoji = payload.get("emoji", "OK")

    if reaction_msg_id:
        resp = add_reaction(reaction_msg_id, emoji)
        print(json.dumps(resp, ensure_ascii=False))
        return

    image_key = None
    if image_path:
        msg_type = "image"
        image_key = upload_image(image_path)

    if payload.get("card"):
        msg_type = "interactive"
        content = payload.get("card")

    if payload.get("update_card"):
        resp = update_card(
            payload.get("update_card"), content or payload.get("card") or "{}"
        )
        print(json.dumps(resp, ensure_ascii=False))
        return

    if content and msg_type == "text" and _has_markdown(content):
        msg_type = "interactive"
        content = _build_markdown_card(content)

    resp = send_message(chat_id, content, reply_to, msg_type, image_key)
    print(json.dumps(resp, ensure_ascii=False))


if __name__ == "__main__":
    main()