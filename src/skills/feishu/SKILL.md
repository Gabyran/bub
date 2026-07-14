---
name: feishu
description: |
  Feishu (Lark) bot skill for sending and receiving messages via lark-oapi SDK.
  Use when Bub needs to: (1) Send a message to a Feishu user or group chat,
  (2) Reply to a specific Feishu message, (3) Send images, interactive cards, or reactions,
  (4) Receive inbound Feishu messages via WebSocket event subscription.
  Prefer markdown card formatting for any content beyond a single short paragraph.
metadata:
  channel: feishu
---

# Feishu Skill

Agent-facing execution guide for Feishu (Lark) outbound and inbound communication.

Env vars:

- `BUB_LARK_APP_ID` — Lark app ID
- `BUB_LARK_APP_SECRET` — Lark app secret

## Required Inputs

Collect these before execution:

- `chat_id` (required for send) — `oc_xxx` for group, `ou_xxx` for p2p
- `message_id` (required for reply or reaction) — `om_xxx`
- message content (required for send/reply)

## Sending Messages

Use `${SKILL_DIR}/scripts/feishu_send.py` for all outbound operations.

### Execution Policy

1. If handling a direct user message and `message_id` is known, **always use `--reply-to`**.
2. **Add reaction first** (`--reaction <message_id> --emoji OK`) as immediate ack, then send the actual reply.
3. **Prefer markdown card** — if content contains `**bold**`, `` `code` ``, `# headers`, or `| tables`, the script auto-wraps in card2.0 markdown element.
4. **ALWAYS pass message content via stdin using heredoc pipe and `--content -`.** NEVER embed message text directly in shell arguments.
5. Private chats and group chats use the **same reply logic** — do not vary behavior by chat_id.

### Command Templates

```bash
# Send text message (ALWAYS use heredoc stdin)
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/feishu_send.py --chat-id <CHAT_ID> --content -
Your message content here.
Special characters are safe: $100, "quotes", 'apostrophes'
EOF

# Reply to a specific message
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/feishu_send.py --chat-id <CHAT_ID> --content - --reply-to <MESSAGE_ID>
Reply content here.
EOF

# Add reaction (immediate ack)
uv run ${SKILL_DIR}/scripts/feishu_send.py --reaction <MESSAGE_ID> --emoji OK

# Send image
uv run ${SKILL_DIR}/scripts/feishu_send.py --chat-id <CHAT_ID> --image /path/to/photo.png

# Send interactive card (card1.0 auto-converted to card2.0)
uv run ${SKILL_DIR}/scripts/feishu_send.py --chat-id <CHAT_ID> --card '{"config":{},"elements":[...]}'

# Update an existing card
uv run ${SKILL_DIR}/scripts/feishu_send.py --update-card <MESSAGE_ID> --card '{"schema":"2.0","body":{"elements":[...]}}'
```

### Active Response Pattern

```
User message received
  ├─ Step 1: Add reaction (OK emoji) → immediate ack
  ├─ Step 2: Process the message
  └─ Step 3: Send actual reply (text or card via --reply-to)
```

### Message Types

| Type | How to Send | When to Use |
|------|-------------|-------------|
| **Text** | `--content "text"` | Default for all conversations |
| **Markdown** | Auto-detected when content has `**`, `` ` ``, `#`, `|` | Rich formatting, code, tables |
| **Image** | `--image /path/to/file.png` | Visual content, screenshots |
| **Card** | `--card '{...}'` | Structured content, buttons, multi-section |
| **Reaction** | `--reaction <msg_id> --emoji OK` | Lightweight ack, or when user requests |

### Error Codes

| Code | Meaning |
|------|---------|
| `9499` | Bot not in chat |
| `10014` | Auth expired |
| `11203` | Image too large |
| `99992402` | Field validation failed |

## Receiving Messages

Use `${SKILL_DIR}/scripts/feishu_receive.py` for inbound WebSocket event subscription.

### Quick Start

```bash
# Start listening (foreground, one JSON per line)
uv run ${SKILL_DIR}/scripts/feishu_receive.py

# Quiet mode
uv run ${SKILL_DIR}/scripts/feishu_receive.py --quiet

# Also listen for card button clicks
uv run ${SKILL_DIR}/scripts/feishu_receive.py --card-actions
```

### Output Format

Each line on stdout is a JSON object:

```json
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
```

### Activation Rules

A message is considered "active" when any of these is true:
- `chat_type == "p2p"` (private chat)
- Content contains `@bot` mention
- Content starts with `,` (comma command prefix)
- Message is a reply to a previous bot message

### Long-Running Listener

Use the startup script for persistent background listening:

```bash
uv run ${SKILL_DIR}/scripts/feishu_startup.py --quiet --card-actions
```

Or via the manager shell script if available:

```bash
~/.bub/feishu_startup.sh start     # Start receiver
~/.bub/feishu_startup.sh watchdog  # Start + auto-restart
~/.bub/feishu_startup.sh status    # Check status
~/.bub/feishu_startup.sh stop      # Stop everything
```

## Script Reference

### `feishu_send.py`

| Arg | Description |
|-----|-------------|
| `--chat-id` | Target chat ID (required for send) |
| `--content` | Message text / card JSON (use `-` for stdin) |
| `--reply-to` | Message ID to reply to |
| `--msg-type` | `text` (default), `image`, `interactive`, `post` |
| `--image` | Local image file path to upload and send |
| `--card` | Card JSON string (shorthand for `--msg-type interactive`) |
| `--update-card` | Message ID of existing card to update (PATCH) |
| `--reaction` | Message ID to add reaction to |
| `--emoji` | Emoji key for reaction (default `OK`) |

### `feishu_receive.py`

| Arg | Description |
|-----|-------------|
| `--quiet` | Reduce log verbosity |
| `--card-actions` | Also listen for `card.action.trigger` events |

### `feishu_startup.py`

| Arg | Description |
|-----|-------------|
| `--quiet` | Suppress SDK log messages |
| `--card-actions` | Also listen for card action events |

## Prerequisites

- `lark-oapi` Python SDK (`pip install lark-oapi`)
- Bot scopes: `im:message:send_as_bot`, `im:message.reaction:write_as_bot`, `im:image:write_as_bot`, `im:message.p2p_msg:readonly`
- Event `im.message.receive_v1` subscribed in Feishu app console
