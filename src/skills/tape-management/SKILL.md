---
name: tape-management
description: |
  Efficient tape and context management patterns for long-running agent sessions.
  Use ALWAYS — this skill provides operational guidelines for every turn:
  (1) How to use tape.handoff to create context checkpoints,
  (2) How to query tape entries efficiently near anchors instead of full-text search,
  (3) How to use programmatic batch processing to minimize context accumulation,
  (4) When to empty context and refill only what's needed.
  This skill should be loaded at every turn to guide context-aware behavior.
---

# Tape Management

Operational guidelines for efficient context and tape usage in long-running sessions.

## Core Principle: Context is a Public Good

The context window has finite capacity. Every entry consumes tokens shared with the user's request, skills, and tool results. Manage it like memory in a resource-constrained system.

## 1. Handoff as Context Checkpoints

`tape.handoff` creates an **anchor** that summarizes and shields all preceding history. After a handoff:
- Entries before the anchor are hidden from direct context
- Only the anchor summary and entries after it remain visible
- This effectively "empties" the context of historical noise

**When to handoff:**
- After completing a major task or phase
- When switching to a different topic or domain
- When `entries_since_last_anchor` exceeds ~200
- When context feels cluttered with stale information

**Handoff pattern:**
```
Complete task → tape.handoff(name="task-complete", summary="...") → Fresh context
```

## 2. Query Near Anchors, Not Global Search

**Avoid:** Loading full tape history or broad `tape.search` results that dump large amounts of text into context.

**Prefer:** Navigate from known anchor positions and extract only what's needed.

### Anchor-first navigation:
```
tape.anchors()           → List all anchors with positions
tape.info()              → Get entry counts and last anchor position
tape.search(query=...)   → Targeted search, limit results
```

### Programmatic extraction pattern:
Use shell scripts to parse tape data instead of loading raw entries into context:
```bash
# Extract just the fields you need, not full entries
tape_output=$(...)
echo "$tape_output" | python3 -c "
import sys, json
for line in sys.stdin:
    entry = json.loads(line)
    # Extract only relevant fields
    print(f'{entry[\"date\"]}: {entry[\"content\"][:100]}')
"
```

## 3. Small Batches, Multiple Passes

Process tape data in small chunks rather than loading everything at once:

1. **First pass:** Get anchor list and entry counts (lightweight)
2. **Second pass:** Search for specific entries near the relevant anchor
3. **Third pass:** Extract only the needed content fields
4. **Act** on the extracted data
5. **Handoff** if the task is complete

Never accumulate tape entries across multiple unrelated tasks. Each task should start from a clean anchor point.

## 4. Empty and Refill Strategy

Before starting a new task or phase:

```
1. tape.handoff()  → Clear historical context
2. tape.info()     → Confirm clean state
3. Load only what's needed for the new task
```

This "empty and refill" pattern prevents context pollution from previous tasks bleeding into new ones.

## 5. Anti-Patterns to Avoid

| Anti-Pattern | Why It's Bad | Do This Instead |
|---|---|---|
| Broad `tape.search` with no limits | Dumps hundreds of entries into context | Use `limit` param and specific queries |
| Reading full entry content | Wastes tokens on metadata/formatting | Extract only needed fields programmatically |
| Skipping handoff between tasks | Old context pollutes new reasoning | Always handoff after completing a task |
| Accumulating entries across phases | Context grows unbounded | Reset at phase boundaries |

## 6. Session Health Check

Run this periodically to assess tape health:
```
tape.info()
```

**Healthy indicators:**
- `entries_since_last_anchor` < 200
- `last_token_usage` within comfortable range
- Recent handoff anchor exists

**Warning signs:**
- `entries_since_last_anchor` > 500 → Time to handoff
- No anchors at all → Create one now
- Token usage near limits → Handoff immediately
