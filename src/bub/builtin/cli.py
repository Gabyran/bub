"""Builtin CLI command adapter."""

# ruff: noqa: B008
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import typer
import yaml

from bub import __version__, configure
from bub.builtin.auth import app as login_app  # noqa: F401
from bub.channels.message import ChannelMessage
from bub.envelope import field_of
from bub.framework import BubFramework
from bub.types import TurnResult

ONBOARD_BANNER = r"""
 ███████████             █████
▒▒███▒▒▒▒▒███           ▒▒███
 ▒███    ▒███ █████ ████ ▒███████
 ▒██████████ ▒▒███ ▒███  ▒███▒▒███
 ▒███▒▒▒▒▒███ ▒███ ▒███  ▒███ ▒███
 ▒███    ▒███ ▒███ ▒███  ▒███ ▒███
 ███████████  ▒▒████████ ████████
▒▒▒▒▒▒▒▒▒▒▒    ▒▒▒▒▒▒▒▒ ▒▒▒▒▒▒▒▒  v{version}
""".strip("\n")

provider_app = typer.Typer(help="Inspect and switch model provider configuration.")

ProviderPreset = dict[str, Any]
ProviderProfile = dict[str, Any]
API_FORMAT_VALUES = ("completion", "responses", "messages")
PROVIDER_CATEGORY_VALUES = (
    "official",
    "cn_official",
    "cloud_provider",
    "aggregator",
    "third_party",
    "custom",
)
PROVIDER_CATEGORY_LABELS = {
    "official": "official",
    "cn_official": "cn_official",
    "cloud_provider": "cloud_provider",
    "aggregator": "aggregator",
    "third_party": "third_party",
    "custom": "custom",
}
PROVIDER_CATEGORY_ORDER = {category: index for index, category in enumerate(PROVIDER_CATEGORY_VALUES)}
API_NAME_TO_FORMAT = {
    "openai-completions": "completion",
    "openai-chat": "completion",
    "openai_chat": "completion",
    "chat-completions": "completion",
    "chat_completions": "completion",
    "openai-chat-completions": "completion",
    "openai_chat_completions": "completion",
    "openai-responses": "responses",
    "openai_responses": "responses",
    "codex-responses": "responses",
    "codex_responses": "responses",
    "anthropic-messages": "messages",
    "anthropic": "messages",
    "anthropic_messages": "messages",
    "anthropic_native": "messages",
    "completion": "completion",
    "chat": "completion",
    "responses": "responses",
    "messages": "messages",
}
CANONICAL_API_BY_FORMAT = {
    "completion": "openai-completions",
    "responses": "openai-responses",
    "messages": "anthropic-messages",
}
FORMAT_TO_API_NAME = {
    "completion": "openai-completions",
    "responses": "openai-responses",
    "messages": "anthropic-messages",
}
CONFIG_PROVIDER_FIELDS = (
    "model",
    "api_key",
    "api_base",
    "api_format",
    "client_args",
    "fallback_models",
    "max_tokens",
    "model_timeout_seconds",
)
CLEARABLE_CONFIG_PROVIDER_FIELDS = (
    "api_key",
    "api_base",
    "client_args",
    "fallback_models",
    "max_tokens",
    "model_timeout_seconds",
)
BUILTIN_PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "kimi-coding": {
        "name": "Kimi Coding",
        "description": "Kimi Coding Plan endpoint. Uses the OpenAI-compatible chat-completions API.",
        "aliases": ["kimi", "moonshot", "kimi-for-coding"],
        "category": "cn_official",
        "website_url": "https://www.kimi.com/code/docs/",
        "api_key_url": "https://platform.moonshot.cn/console/api-keys",
        "icon": "kimi",
        "icon_color": "#6366F1",
        "api": "openai-completions",
        "model_provider": "openai",
        "api_base": "https://api.kimi.com/coding/v1",
        "api_key_env": ["KIMI_API_KEY", "KIMI_CODING_API_KEY"],
        "default_model": "kimi-for-coding",
        "max_tokens": 32000,
        "models": [
            {"id": "kimi-for-coding", "name": "Kimi For Coding", "contextWindow": 131072},
            {"id": "kimi-k2.6", "name": "Kimi K2.6", "contextWindow": 262144},
            {"id": "kimi-k2.5", "name": "Kimi K2.5"},
        ],
        "client_args": {
            "default_headers": {
                "User-Agent": "claude-code/0.1.0",
            },
        },
    },
    "openrouter": {
        "name": "OpenRouter",
        "description": "OpenRouter OpenAI-compatible gateway with a curated coding model list.",
        "aliases": ["or"],
        "category": "aggregator",
        "website_url": "https://openrouter.ai/",
        "api_key_url": "https://openrouter.ai/keys",
        "models_url": "https://openrouter.ai/api/v1/models",
        "icon": "openrouter",
        "api": "openai-completions",
        "model_provider": "openrouter",
        "api_base": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "default_model": "openrouter/free",
        "models": [
            {"id": "openrouter/free", "name": "OpenRouter Free"},
            {"id": "moonshotai/kimi-k2.6", "name": "Kimi K2.6", "contextWindow": 262144},
            {"id": "anthropic/claude-sonnet-4.6", "name": "Claude Sonnet 4.6"},
            {"id": "openai/gpt-5.4-mini", "name": "GPT-5.4 Mini"},
            {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash", "reasoning": True},
            {"id": "qwen/qwen3.6-35b-a3b", "name": "Qwen 3.6 35B A3B"},
        ],
    },
    "openai-api": {
        "name": "OpenAI Official",
        "description": "OpenAI API using the Responses transport.",
        "aliases": ["openai", "gpt"],
        "category": "official",
        "website_url": "https://platform.openai.com/",
        "api_key_url": "https://platform.openai.com/api-keys",
        "models_url": "https://api.openai.com/v1/models",
        "icon": "openai",
        "icon_color": "#00A67E",
        "api": "openai-responses",
        "model_provider": "openai",
        "api_base": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "gpt-5.5",
        "models": [
            {"id": "gpt-5.5", "name": "GPT-5.5"},
            {"id": "gpt-5.5-pro", "name": "GPT-5.5 Pro"},
            {"id": "gpt-5.4", "name": "GPT-5.4"},
            {"id": "gpt-5.4-mini", "name": "GPT-5.4 Mini"},
            {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex"},
            {"id": "gpt-4.1", "name": "GPT-4.1"},
        ],
    },
    "anthropic-api": {
        "name": "Anthropic Official",
        "description": "Anthropic Messages API for Claude models.",
        "aliases": ["anthropic", "claude", "claude-code"],
        "category": "official",
        "website_url": "https://console.anthropic.com/",
        "api_key_url": "https://console.anthropic.com/settings/keys",
        "models_url": "https://api.anthropic.com/v1/models",
        "icon": "anthropic",
        "icon_color": "#D4915D",
        "api": "anthropic-messages",
        "model_provider": "anthropic",
        "api_base": "https://api.anthropic.com",
        "api_key_env": ["ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"],
        "default_model": "claude-sonnet-4-6",
        "models": [
            {"id": "claude-opus-4-8", "name": "Claude Opus 4.8"},
            {"id": "claude-opus-4-7", "name": "Claude Opus 4.7"},
            {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
            {"id": "claude-sonnet-4-5-20250929", "name": "Claude Sonnet 4.5"},
            {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5"},
        ],
    },
    "local-openai-responses": {
        "name": "Local OpenAI Responses Template",
        "description": "Local OpenAI-compatible Responses gateway. Docker reaches the host through host.docker.internal.",
        "aliases": ["local-responses"],
        "category": "custom",
        "is_custom_template": True,
        "api": "openai-responses",
        "model_provider": "openai",
        "api_base": "http://host.docker.internal:8317/v1",
        "api_key_env": "LOCAL_OPENAI_API_KEY",
        "default_model": "gpt-5.4",
        "models": [
            "gpt-5.4",
            "gpt-5.3-codex",
            "gpt-5.2-codex",
        ],
    },
    "local-anthropic-messages": {
        "name": "Local Anthropic Messages Template",
        "description": "Local Anthropic-compatible Messages gateway. Docker reaches the host through host.docker.internal.",
        "aliases": ["local-anthropic", "local-messages"],
        "category": "custom",
        "is_custom_template": True,
        "api": "anthropic-messages",
        "model_provider": "anthropic",
        "api_base": "http://host.docker.internal:8080/antigravity",
        "api_key_env": "LOCAL_ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
        "models": [
            "claude-sonnet-4-6",
            "claude-opus-4-6",
        ],
    },
    "deepseek": {
        "name": "DeepSeek",
        "description": "DeepSeek OpenAI-compatible API.",
        "aliases": ["deepseek-chat"],
        "category": "cn_official",
        "website_url": "https://platform.deepseek.com/",
        "api_key_url": "https://platform.deepseek.com/api_keys",
        "icon": "deepseek",
        "api": "openai-completions",
        "model_provider": "openai",
        "api_base": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "models": [
            {"id": "deepseek-chat", "name": "DeepSeek Chat"},
            {"id": "deepseek-reasoner", "name": "DeepSeek Reasoner", "reasoning": True},
            {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro", "reasoning": True},
            {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash", "reasoning": True},
        ],
    },
    "minimax": {
        "name": "MiniMax",
        "description": "MiniMax Anthropic-compatible endpoint.",
        "aliases": ["mini-max"],
        "category": "cn_official",
        "website_url": "https://api.minimax.io/",
        "api_key_url": "https://platform.minimax.io/subscribe/coding-plan",
        "icon": "minimax",
        "icon_color": "#FF6B6B",
        "api": "anthropic-messages",
        "model_provider": "anthropic",
        "api_base": "https://api.minimax.io/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "default_model": "MiniMax-M2.7",
        "models": [
            "MiniMax-M2.7",
            "MiniMax-M2.7-highspeed",
            "MiniMax-M2.1",
            "MiniMax-M2",
        ],
    },
    "openai-compatible-completions": {
        "name": "OpenAI-Compatible Completions",
        "description": "Template for an OpenAI-compatible chat-completions endpoint. Provide --api-base and usually --api-key.",
        "aliases": ["openai-chat-template", "chat-completions-template"],
        "category": "custom",
        "is_custom_template": True,
        "api": "openai-completions",
        "model_provider": "openai",
        "api_base": "",
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "modelroute",
        "models": ["modelroute"],
        "requires_api_base": True,
    },
    "openai-compatible-responses": {
        "name": "OpenAI-Compatible Responses",
        "description": "Template for an OpenAI-compatible Responses endpoint. Provide --api-base and usually --api-key.",
        "aliases": ["responses-template", "codex-responses-template"],
        "category": "custom",
        "is_custom_template": True,
        "api": "openai-responses",
        "model_provider": "openai",
        "api_base": "",
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "gpt-5.4",
        "models": ["gpt-5.4"],
        "requires_api_base": True,
    },
    "anthropic-compatible-messages": {
        "name": "Anthropic-Compatible Messages",
        "description": "Template for an Anthropic-compatible Messages endpoint. Provide --api-base and usually --api-key.",
        "aliases": ["anthropic-template", "messages-template"],
        "category": "custom",
        "is_custom_template": True,
        "api": "anthropic-messages",
        "model_provider": "anthropic",
        "api_base": "",
        "api_key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
        "models": ["claude-sonnet-4-6"],
        "requires_api_base": True,
    },
}


def run(
    ctx: typer.Context,
    message: str = typer.Argument(..., help="Inbound message content"),
    channel: str = typer.Option("cli", "--channel", help="Message channel"),
    chat_id: str = typer.Option("local", "--chat-id", help="Chat id"),
    sender_id: str = typer.Option("human", "--sender-id", help="Sender id"),
    session_id: str | None = typer.Option(None, "--session-id", help="Optional session id"),
) -> None:
    """Run one inbound message through the framework pipeline."""

    framework = ctx.ensure_object(BubFramework)
    inbound = ChannelMessage(
        session_id=f"{channel}:{chat_id}" if session_id is None else session_id,
        content=message,
        channel=channel,
        chat_id=chat_id,
        context={"sender_id": sender_id},
    )

    async def _run() -> TurnResult:
        async with framework.running():
            return await framework.process_inbound(inbound)

    result = asyncio.run(_run())
    for outbound in result.outbounds:
        rendered = str(field_of(outbound, "content", ""))
        target_channel = str(field_of(outbound, "channel", "stdout"))
        target_chat = str(field_of(outbound, "chat_id", "local"))
        typer.echo(f"[{target_channel}:{target_chat}]\n{rendered}")


def list_hooks(ctx: typer.Context) -> None:
    """Show hook implementation mapping."""
    framework = ctx.ensure_object(BubFramework)
    report = framework.hook_report()
    if not report:
        typer.echo("(no hook implementations)")
        return
    for hook_name, adapter_names in report.items():
        typer.echo(f"{hook_name}: {', '.join(adapter_names)}")


def gateway(
    ctx: typer.Context,
    enable_channels: list[str] = typer.Option([], "--enable-channel", help="Channels to enable for CLI (default: all)"),
) -> None:
    """Start message listeners(like telegram)."""
    from bub.channels.manager import ChannelManager

    framework = ctx.ensure_object(BubFramework)

    manager = ChannelManager(framework, enabled_channels=enable_channels or None)
    asyncio.run(manager.listen_and_run())


def chat(
    ctx: typer.Context,
    chat_id: str = typer.Option("local", "--chat-id", help="Chat id"),
    session_id: str | None = typer.Option(None, "--session-id", help="Optional session id"),
) -> None:
    """Start a REPL chat session."""
    from bub.channels.manager import ChannelManager

    framework = ctx.ensure_object(BubFramework)

    manager = ChannelManager(framework, enabled_channels=["cli"], stream_output=True)
    channel = manager.get_channel("cli")
    if channel is None:
        typer.echo("CLI channel not found. Please check your hook implementations.")
        raise typer.Exit(1)
    channel.set_metadata(chat_id=chat_id, session_id=session_id)  # type: ignore[attr-defined]
    asyncio.run(manager.listen_and_run())


def onboard(ctx: typer.Context) -> None:
    """Interactively collect plugin configuration and write it to Bub's config file."""

    framework = ctx.ensure_object(BubFramework)
    typer.echo(ONBOARD_BANNER.format(version=__version__))
    typer.echo("\nWelcome to Bub! Let's get you set up.\n")

    try:
        config_data = framework.collect_onboard_config()
        configure.save(framework.config_file, config_data)
    except (typer.Abort, typer.Exit):
        raise
    except Exception as exc:
        typer.secho(f"Onboarding failed: {exc}", err=True, fg="red")
        raise typer.Exit(1) from exc

    typer.echo(f"Saved config to {framework.config_file}")


def _load_yaml_file(path: Path) -> dict[str, Any]:
    try:
        return configure.load_yaml_mapping(path)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _write_yaml_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def _backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{path.name}.bak.provider-{stamp}"
    shutil.copy2(path, backup_path)
    return backup_path


def _provider_presets_dir(config_file: Path) -> Path:
    return config_file.parent / "provider-presets"


def _provider_preset_source(name: str, *, is_local: bool) -> str:
    return "local" if is_local else "builtin"


def _sanitize_preset_name(name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip().lower()).strip("-")
    if not normalized:
        raise typer.BadParameter("preset name must contain at least one letter or number.")
    return normalized


def _normalize_api_name(api_name: str | None, *, api_format: str | None = None) -> str:
    raw = (api_name or "").strip().lower().replace("_", "-")
    if not raw and api_format:
        normalized_format = api_format.strip().lower().replace("_", "-")
        raw = normalized_format if normalized_format in API_NAME_TO_FORMAT else FORMAT_TO_API_NAME.get(normalized_format, "")
    if raw not in API_NAME_TO_FORMAT:
        choices = ", ".join(CANONICAL_API_BY_FORMAT.values())
        raise typer.BadParameter(f"api must be one of: {choices}")
    return CANONICAL_API_BY_FORMAT[API_NAME_TO_FORMAT[raw]]


def _api_format_from_api(api_name: str) -> str:
    return API_NAME_TO_FORMAT[_normalize_api_name(api_name)]


def _coalesce(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def _as_str_list(raw_value: Any) -> list[str]:
    if raw_value in (None, ""):
        return []
    if isinstance(raw_value, str):
        return [raw_value.strip()] if raw_value.strip() else []
    if isinstance(raw_value, (list, tuple)):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    return []


def _normalize_aliases(raw_value: Any) -> list[str]:
    return _as_str_list(raw_value)


def _normalize_endpoint_candidates(raw_value: Any) -> list[str]:
    return _as_str_list(raw_value)


def _bool_from_preset(raw_value: Any) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    if raw_value is None:
        return False
    if isinstance(raw_value, str):
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw_value)


def _normalize_provider_category(raw_value: Any, *, is_custom_template: bool = False) -> str:
    raw = str(raw_value or "").strip()
    if raw in PROVIDER_CATEGORY_ORDER:
        return raw
    if raw in {"official_cn", "china_official", "domestic_official", "coding_plan"}:
        return "cn_official"
    if raw in {"cloud", "cloud-provider"}:
        return "cloud_provider"
    if raw in {"gateway", "router", "proxy", "aggregation"}:
        return "aggregator"
    if raw in {"third-party", "thirdparty", "vendor"}:
        return "third_party"
    if raw in {"local", "localhost", "local_gateway"}:
        return "custom"
    return "custom" if is_custom_template or not raw else "third_party"


def _normalize_model_entry(raw_model: Any, *, model_id: str | None = None) -> dict[str, Any] | None:
    if isinstance(raw_model, str):
        raw_id = raw_model
        raw_data: dict[str, Any] = {}
    elif isinstance(raw_model, dict):
        raw_data = raw_model
        raw_id = model_id or raw_data.get("id") or raw_data.get("model") or raw_data.get("name")
    else:
        return None

    if not isinstance(raw_id, str) or not raw_id.strip():
        return None

    entry: dict[str, Any] = {"id": raw_id.strip()}
    display_name = raw_data.get("displayName") or raw_data.get("display_name")
    if display_name is None and "id" in raw_data:
        display_name = raw_data.get("name")
    if isinstance(display_name, str) and display_name.strip() and display_name.strip() != entry["id"]:
        entry["name"] = display_name.strip()

    alias = raw_data.get("alias")
    if isinstance(alias, str) and alias.strip():
        entry["alias"] = alias.strip()

    int_fields = {
        "context_window": ("context_window", "contextWindow", "contextLength", "context_length"),
        "max_tokens": ("max_tokens", "maxTokens"),
    }
    for target, keys in int_fields.items():
        raw_value = _coalesce(raw_data, *keys)
        if raw_value in (None, ""):
            continue
        try:
            int_value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if int_value > 0:
            entry[target] = int_value

    if isinstance(raw_data.get("reasoning"), bool):
        entry["reasoning"] = raw_data["reasoning"]

    raw_input = raw_data.get("input")
    if isinstance(raw_input, list):
        entry["input"] = [str(item) for item in raw_input if str(item)]

    raw_cost = raw_data.get("cost")
    if isinstance(raw_cost, dict):
        entry["cost"] = raw_cost

    return entry


def _normalize_model_entries(raw_models: Any) -> list[dict[str, Any]]:
    if raw_models in (None, ""):
        return []
    if isinstance(raw_models, dict):
        raw_items = []
        for model_id, model_data in raw_models.items():
            if isinstance(model_data, dict):
                raw_items.append(_normalize_model_entry(model_data, model_id=str(model_id)))
            elif model_data in (None, ""):
                raw_items.append(_normalize_model_entry(str(model_id)))
            else:
                raw_items.append(_normalize_model_entry({"id": str(model_id), "name": str(model_data)}))
    elif isinstance(raw_models, list):
        raw_items = [_normalize_model_entry(item) for item in raw_models]
    else:
        raise typer.BadParameter("provider preset field 'models' must be a list or mapping.")

    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_items:
        if not item:
            continue
        model_id = item["id"]
        if model_id in seen:
            continue
        seen.add(model_id)
        models.append(item)
    return models


def _raw_models_from_catalog(data: dict[str, Any], settings: dict[str, Any]) -> Any:
    for raw_models in (
        data.get("models"),
        settings.get("models"),
        data.get("modelCatalog"),
        data.get("model_catalog"),
    ):
        if isinstance(raw_models, dict) and isinstance(raw_models.get("models"), list):
            return raw_models["models"]
        if raw_models not in (None, ""):
            return raw_models
    return None


def _parse_codex_template_config(config_text: Any) -> dict[str, str]:
    if not isinstance(config_text, str) or not config_text.strip():
        return {}

    parsed: dict[str, str] = {}
    current_section = ""
    for line in config_text.splitlines():
        section_match = re.match(r"\s*\[([^\]]+)\]\s*$", line)
        if section_match:
            current_section = section_match.group(1).strip()
            continue
        assign_match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*=\s*([\"'])(.*?)\2\s*(?:#.*)?$", line)
        if not assign_match:
            continue
        key = assign_match.group(1)
        value = assign_match.group(3)
        if not current_section and key in {"model", "model_provider"}:
            parsed[key] = value
        elif current_section.startswith("model_providers.") and key in {"name", "base_url", "wire_api"}:
            parsed[f"provider_{key}"] = value
    return parsed


def _normalize_key_env(raw_value: Any) -> list[str]:
    if raw_value in (None, ""):
        return []
    if isinstance(raw_value, str):
        return [raw_value]
    if isinstance(raw_value, (list, tuple)):
        values = [str(item).strip() for item in raw_value if str(item).strip()]
        return values
    raise typer.BadParameter("provider preset field 'api_key_env' must be a string or list.")


def _split_model(model: str | None) -> tuple[str, str]:
    if not model:
        return "", ""
    provider, separator, model_name = model.partition(":")
    if not separator:
        return "", model
    return provider, model_name


def _default_model_provider(api_name: str) -> str:
    return "anthropic" if _api_format_from_api(api_name) == "messages" else "openai"


def _read_provider_preset_file(path: Path) -> ProviderPreset:
    data = _load_yaml_file(path)
    for wrapper in ("preset", "provider", "profile"):
        wrapped = data.get(wrapper)
        if isinstance(wrapped, dict):
            data = wrapped
            break
    return _normalize_provider_preset(data, source=str(path))


def _settings_config_from_preset(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("settingsConfig", "settings_config"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _api_from_template(data: dict[str, Any], settings: dict[str, Any], codex_template: dict[str, str]) -> str | None:
    raw_api = _coalesce(data, "api", "apiFormat", "api_format")
    if raw_api is None:
        raw_api = _coalesce(settings, "api", "apiFormat", "api_format")
    if raw_api is None:
        raw_api = codex_template.get("provider_wire_api")
    return str(raw_api) if raw_api is not None else None


def _normalize_provider_preset(data: dict[str, Any], *, source: str) -> ProviderPreset:
    settings = _settings_config_from_preset(data)
    codex_template = _parse_codex_template_config(data.get("config"))
    raw_api = _api_from_template(data, settings, codex_template)
    raw_api_format = _coalesce(data, "api_format", "apiMode", "api_mode", "transport")
    if raw_api_format is None:
        raw_api_format = _coalesce(settings, "api_format", "apiMode", "api_mode", "transport")
    api_name = _normalize_api_name(raw_api, api_format=str(raw_api_format) if raw_api_format is not None else None)

    old_provider, old_model = _split_model(str(_coalesce(data, "model", default="") or ""))
    if not old_model:
        _, old_model = _split_model(str(codex_template.get("model") or ""))

    model_provider = (
        _coalesce(data, "model_provider", "provider_prefix", "provider_id", "providerKey", "provider_key")
        or _coalesce(settings, "model_provider", "provider_prefix", "provider_id", "providerKey", "provider_key", "name")
        or codex_template.get("provider_name")
        or codex_template.get("model_provider")
        or old_provider
        or _default_model_provider(api_name)
    )
    if not isinstance(model_provider, str) or not model_provider.strip():
        raise typer.BadParameter(f"{source} must define provider preset field 'model_provider'.")

    model_entries = _normalize_model_entries(_raw_models_from_catalog(data, settings))
    default_model = (
        _coalesce(data, "default_model", "defaultModel")
        or _coalesce(settings, "default_model", "defaultModel", "model")
        or old_model
        or codex_template.get("model")
    )
    if not default_model and model_entries:
        default_model = model_entries[0]["id"]
    if isinstance(default_model, str) and ":" in default_model:
        _, default_model_name = _split_model(default_model)
        default_model = default_model_name or default_model
    if isinstance(default_model, str):
        default_model = default_model.strip()
    if not isinstance(default_model, str) or not default_model:
        raise typer.BadParameter(f"{source} must define provider preset field 'default_model'.")
    if default_model not in [item["id"] for item in model_entries]:
        model_entries.insert(0, {"id": default_model})

    api_base = (
        _coalesce(data, "api_base", "baseUrl", "base_url", "inference_base_url")
        or _coalesce(settings, "api_base", "baseUrl", "base_url", "url", "api_url")
        or codex_template.get("provider_base_url")
        or ""
    )
    is_custom_template = _bool_from_preset(_coalesce(data, "is_custom_template", "isCustomTemplate"))
    is_full_url = _bool_from_preset(_coalesce(data, "is_full_url", "isFullUrl"))
    category = _normalize_provider_category(
        _coalesce(data, "category", default=""),
        is_custom_template=is_custom_template,
    )
    headers = _coalesce(data, "headers")
    if headers is None:
        headers = _coalesce(settings, "headers")
    client_args = data.get("client_args")
    if client_args is None and isinstance(headers, dict):
        client_args = {"default_headers": headers}

    preset: ProviderPreset = {
        "name": str(_coalesce(data, "display_name", "displayName", "name", "label", default="")).strip(),
        "description": str(_coalesce(data, "description", default="")).strip(),
        "aliases": _normalize_aliases(_coalesce(data, "aliases", "alias")),
        "category": category,
        "is_custom_template": is_custom_template,
        "website_url": str(_coalesce(data, "website_url", "websiteUrl", "signup_url", "signupUrl", default="")).strip(),
        "api_key_url": str(_coalesce(data, "api_key_url", "apiKeyUrl", default="")).strip(),
        "icon": str(_coalesce(data, "icon", default="")).strip(),
        "icon_color": str(_coalesce(data, "icon_color", "iconColor", default="")).strip(),
        "auth_type": str(_coalesce(data, "auth_type", "authType", default="api_key")).strip(),
        "api": api_name,
        "model_provider": model_provider.strip(),
        "api_base": api_base,
        "models_url": str(_coalesce(data, "models_url", "modelsUrl", default="")).strip(),
        "endpoint_candidates": _normalize_endpoint_candidates(_coalesce(data, "endpoint_candidates", "endpointCandidates")),
        "is_full_url": is_full_url,
        "api_key_env": _normalize_key_env(
            _coalesce(data, "api_key_env", "key_env", "apiKeyEnv", "env_vars", "envVars")
        ),
        "default_model": default_model,
        "models": model_entries,
        "requires_api_base": bool(_coalesce(data, "requires_api_base", "requiresBaseUrl", default=False)),
    }
    if client_args is not None:
        preset["client_args"] = client_args
    for field in ("fallback_models", "max_tokens", "model_timeout_seconds", "api_key"):
        if field in data:
            preset[field] = data[field]
    for raw_field, field in (("default_max_tokens", "max_tokens"), ("timeout_seconds", "model_timeout_seconds")):
        if raw_field in data and field not in preset:
            preset[field] = data[raw_field]
    return preset


def _load_local_provider_presets(config_file: Path) -> dict[str, ProviderPreset]:
    preset_dir = _provider_presets_dir(config_file)
    if not preset_dir.is_dir():
        return {}
    presets: dict[str, ProviderPreset] = {}
    for path in sorted((*preset_dir.glob("*.yml"), *preset_dir.glob("*.yaml"))):
        name = _sanitize_preset_name(path.stem)
        presets[name] = _read_provider_preset_file(path)
    return presets


def _all_provider_presets(config_file: Path) -> dict[str, ProviderPreset]:
    presets = {name: _normalize_provider_preset(copy.deepcopy(preset), source=f"builtin:{name}") for name, preset in BUILTIN_PROVIDER_PRESETS.items()}
    presets.update(_load_local_provider_presets(config_file))
    return presets


def _provider_preset_sources(config_file: Path) -> dict[str, str]:
    sources = {name: _provider_preset_source(name, is_local=False) for name in BUILTIN_PROVIDER_PRESETS}
    sources.update({name: _provider_preset_source(name, is_local=True) for name in _load_local_provider_presets(config_file)})
    return sources


def _provider_aliases(presets: dict[str, ProviderPreset]) -> dict[str, str]:
    aliases = {"openrouter-free": "openrouter"}
    for name, preset in presets.items():
        aliases[name] = name
        for alias in preset.get("aliases") or []:
            if isinstance(alias, str) and alias.strip():
                aliases[alias.strip()] = name
    return aliases


def _provider_category_label(preset: ProviderPreset) -> str:
    category = str(preset.get("category") or "custom")
    return PROVIDER_CATEGORY_LABELS.get(category, category)


def _is_custom_interface_template(preset: ProviderPreset) -> bool:
    return bool(preset.get("is_custom_template") or preset.get("category") == "custom")


def _provider_sort_key(item: tuple[str, ProviderPreset]) -> tuple[int, str]:
    name, preset = item
    return (
        PROVIDER_CATEGORY_ORDER.get(str(preset.get("category") or "custom"), len(PROVIDER_CATEGORY_ORDER)),
        str(preset.get("name") or name).casefold(),
    )


def _provider_choice_label(name: str, preset: ProviderPreset) -> str:
    display_name = str(preset.get("name") or name).strip() or name
    return display_name


def _provider_list_header(name: str, preset: ProviderPreset, *, source: str) -> str:
    display_name = str(preset.get("name") or name).strip() or name
    category = _provider_category_label(preset)
    template_marker = ", template" if preset.get("is_custom_template") else ""
    return f"{display_name} (id: {name}; {source}, {category}{template_marker})"


def _choose_preset_from_list(label: str, items: list[tuple[str, ProviderPreset]], *, default: str | None = None) -> str:
    if not items:
        raise typer.BadParameter(f"{label} choices must not be empty.")
    base_labels = [_provider_choice_label(name, preset) for name, preset in items]
    label_counts: dict[str, int] = {}
    for item_label in base_labels:
        label_counts[item_label] = label_counts.get(item_label, 0) + 1
    labels = [
        item_label if label_counts[item_label] == 1 else f"{item_label} ({name})"
        for item_label, (name, _preset) in zip(base_labels, items, strict=True)
    ]
    default_label = None
    if default:
        for index, (name, _preset) in enumerate(items):
            if name == default:
                default_label = labels[index]
                break
    selected_label = _choose_from_list(label, labels, default=default_label)
    return items[labels.index(selected_label)][0]


def _api_key_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_api_key_present(item) for item in value.values())
    return value not in (None, "")


def _api_key_status(value: Any) -> str:
    if isinstance(value, dict):
        if not value:
            return "missing (provider map empty)"
        present_keys: list[str] = []
        missing_keys: list[str] = []
        for key, item in sorted(value.items(), key=lambda item: str(item[0])):
            target = present_keys if _api_key_present(item) else missing_keys
            target.append(str(key))
        parts: list[str] = []
        if present_keys:
            parts.append(f"present: {', '.join(present_keys)}")
        if missing_keys:
            parts.append(f"missing: {', '.join(missing_keys)}")
        state = "present" if present_keys else "missing"
        return f"{state} (provider map; values masked; {'; '.join(parts)})"
    if _api_key_present(value):
        return "present (masked)"
    return "missing"


def _redacted_preset_for_print(preset: ProviderPreset) -> ProviderPreset:
    redacted = copy.deepcopy(preset)
    if "api_key" in redacted:
        redacted["api_key"] = _api_key_status(redacted.get("api_key"))
    return redacted


def _first_key_env(preset: ProviderPreset) -> str:
    key_env = preset.get("api_key_env") or []
    if isinstance(key_env, str):
        return key_env
    return str(key_env[0]) if key_env else ""


def _full_model_id(preset: ProviderPreset, model_name: str | None) -> str:
    selected = (model_name or preset.get("default_model") or "").strip()
    if not selected:
        raise typer.BadParameter("model is required.")
    if ":" in selected:
        return selected
    return f"{preset['model_provider']}:{selected}"


def _preset_models(preset: ProviderPreset) -> list[str]:
    models: list[str] = []
    for item in preset.get("models", []):
        if isinstance(item, dict):
            model_id = item.get("id") or item.get("model") or item.get("name")
        else:
            model_id = item
        if isinstance(model_id, str) and model_id.strip() and model_id.strip() not in models:
            models.append(model_id.strip())
    default_model = str(preset.get("default_model") or "").strip()
    if default_model and default_model not in models:
        return [default_model, *models]
    return models


def _preset_model_entries(preset: ProviderPreset) -> list[dict[str, Any]]:
    entries = _normalize_model_entries(preset.get("models"))
    by_id = {entry["id"]: entry for entry in entries}
    default_model = str(preset.get("default_model") or "").strip()
    if default_model and default_model not in by_id:
        entries.insert(0, {"id": default_model})
    return entries


def _format_model_entry(entry: dict[str, Any]) -> str:
    label = entry["id"]
    if entry.get("name"):
        label = f"{label} ({entry['name']})"
    details: list[str] = []
    if entry.get("context_window"):
        details.append(f"context={entry['context_window']}")
    if entry.get("max_tokens"):
        details.append(f"max_tokens={entry['max_tokens']}")
    if entry.get("reasoning") is True:
        details.append("reasoning")
    if entry.get("input"):
        details.append(f"input={','.join(entry['input'])}")
    if entry.get("cost"):
        cost = entry["cost"]
        if isinstance(cost, dict):
            price_parts = []
            for key in ("input", "output"):
                if key in cost:
                    price_parts.append(f"{key}={cost[key]}")
            if price_parts:
                details.append(f"cost({', '.join(price_parts)})")
    return f"{label}  [{'; '.join(details)}]" if details else label


def _choose_from_list(label: str, choices: list[str], *, default: str | None = None) -> str:
    if not choices:
        raise typer.BadParameter(f"{label} choices must not be empty.")

    default_index = choices.index(default) + 1 if default in choices else 1
    typer.echo(f"{label}:")
    for index, choice in enumerate(choices, start=1):
        marker = " *" if index == default_index else ""
        typer.echo(f"  {index}. {choice}{marker}")

    while True:
        answer = typer.prompt(f"Select {label}", default=str(default_index), show_default=True).strip()
        if answer in choices:
            return answer
        try:
            selected_index = int(answer)
        except ValueError:
            typer.secho(f"Enter a number from 1 to {len(choices)}.", err=True, fg="red")
            continue
        if 1 <= selected_index <= len(choices):
            return choices[selected_index - 1]
        typer.secho(f"Enter a number from 1 to {len(choices)}.", err=True, fg="red")


def _prompt_text(label: str, *, default: str = "", hide_input: bool = False) -> str:
    effective_hide_input = hide_input and sys.stdin.isatty()
    return str(
        typer.prompt(label, default=default, hide_input=effective_hide_input, show_default=bool(default))
    ).strip()


def _prompt_api_key_value(*, key_env: str = "") -> str | None:
    env_hint = f" for {key_env}" if key_env else ""
    input_mode = _choose_from_list("API key input", ["hidden", "visible"], default="hidden")
    if input_mode == "visible":
        typer.echo("Paste the new API key, then press Enter. Input will be visible.")
        api_key = _prompt_text(f"New API key{env_hint} (optional)")
    else:
        typer.echo("Paste the new API key, then press Enter. Input is hidden when the terminal supports it.")
        api_key = _prompt_text(f"New API key{env_hint} (optional)", hide_input=True)
    return api_key or None


def _prompt_api_key_update(current: dict[str, Any], *, key_env: str = "") -> tuple[str | None, bool]:
    status = _api_key_status(current.get("api_key"))
    typer.echo(f"Current api_key: {status}")
    if _api_key_present(current.get("api_key")):
        action = _choose_from_list(
            "API key",
            ["reuse existing", "enter new key", "clear existing"],
            default="reuse existing",
        )
        if action == "reuse existing":
            return None, False
        if action == "clear existing":
            return None, True
    return _prompt_api_key_value(key_env=key_env), False


def _matching_preset_name(config_data: dict[str, Any], presets: dict[str, ProviderPreset]) -> str | None:
    provider, model_name = _split_model(str(config_data.get("model") or ""))
    api_base = config_data.get("api_base")
    api_format = config_data.get("api_format")
    for name, preset in presets.items():
        if provider != preset.get("model_provider"):
            continue
        preset_base = preset.get("api_base") or None
        if preset_base and preset_base != (api_base or None):
            continue
        if _api_format_from_api(str(preset["api"])) != (api_format or "completion"):
            continue
        models = _preset_models(preset)
        if models and model_name not in models:
            continue
        return name
    return None


def _print_provider_status(config_data: dict[str, Any], presets: dict[str, ProviderPreset]) -> None:
    provider, model_name = _split_model(str(config_data.get("model") or ""))
    preset_name = _matching_preset_name(config_data, presets) or "custom"
    preset = presets.get(preset_name, {})
    api_format = str(config_data.get("api_format") or "completion")
    typer.echo(f"provider_preset: {preset_name}")
    typer.echo(f"model: {config_data.get('model') or '(unset)'}")
    typer.echo(f"provider: {provider or '(unset)'}")
    typer.echo(f"model_name: {model_name or '(unset)'}")
    typer.echo(f"api: {FORMAT_TO_API_NAME.get(api_format, api_format)}")
    typer.echo(f"api_base: {config_data.get('api_base') or '(unset)'}")
    typer.echo(f"api_format: {api_format}")
    typer.echo(f"api_key_env: {_first_key_env(preset) or '(unset)'}")
    typer.echo(f"api_key: {_api_key_status(config_data.get('api_key'))}")
    typer.echo(f"fallback_models: {config_data.get('fallback_models') or '(unset)'}")
    if config_data.get("client_args") is None:
        typer.echo("client_args: (unset)")
    else:
        typer.echo("client_args: set")


def _provider_config_from_preset(
    preset: ProviderPreset,
    current: dict[str, Any],
    *,
    model: str | None,
    api_base: str | None,
    api: str | None,
    api_format: str | None,
    api_key: str | None,
    clear_api_base: bool,
    clear_api_key: bool,
    keep_client_args: bool,
    clear_client_args: bool,
) -> ProviderProfile:
    api_name = (
        _normalize_api_name(api, api_format=api_format)
        if api or api_format
        else _normalize_api_name(preset.get("api"))
    )
    profile: ProviderProfile = {
        "model": _full_model_id(preset, model),
        "api_format": _api_format_from_api(api_name),
    }

    if clear_api_base:
        profile["api_base"] = None
    else:
        selected_api_base = api_base if api_base is not None else preset.get("api_base")
        if (selected_api_base in (None, "")) and preset.get("requires_api_base"):
            raise typer.BadParameter("this provider preset requires --api-base.")
        profile["api_base"] = selected_api_base or None

    if clear_api_key:
        profile["api_key"] = None
    elif api_key is not None:
        profile["api_key"] = api_key
    elif preset.get("api_key") not in (None, "") and not _api_key_present(current.get("api_key")):
        profile["api_key"] = preset["api_key"]

    if clear_client_args:
        profile["client_args"] = None
    elif keep_client_args:
        pass
    else:
        profile["client_args"] = preset.get("client_args")

    for field in ("fallback_models", "max_tokens", "model_timeout_seconds"):
        if field in preset:
            profile[field] = preset[field]
        elif field in CLEARABLE_CONFIG_PROVIDER_FIELDS:
            profile[field] = None
    return profile


def _custom_preset_from_args(
    *,
    model: str | None,
    api_base: str | None,
    api: str | None,
    api_format: str | None,
) -> ProviderPreset:
    if not model:
        raise typer.BadParameter("provide a provider preset name or --model.")
    provider, model_name = _split_model(model)
    api_name = _normalize_api_name(api, api_format=api_format or "completion")
    return {
        "name": "Custom",
        "description": "Custom one-off provider settings.",
        "category": "custom",
        "api": api_name,
        "model_provider": provider or _default_model_provider(api_name),
        "api_base": api_base or "",
        "default_model": model_name or model,
        "models": [{"id": model_name or model}],
        "api_key_env": [],
        "requires_api_base": False,
    }


def _resolve_preset(name: str, presets: dict[str, ProviderPreset]) -> ProviderPreset:
    preset_name = _provider_aliases(presets).get(name, name)
    try:
        return copy.deepcopy(presets[preset_name])
    except KeyError as exc:
        choices = ", ".join([*presets, "custom"])
        raise typer.BadParameter(f"unknown provider preset '{name}'. Available: {choices}") from exc


def _provider_config_from_current(config_data: dict[str, Any], *, include_api_key: bool) -> ProviderPreset:
    provider, model_name = _split_model(str(config_data.get("model") or ""))
    if not provider or not model_name:
        raise typer.BadParameter("current config does not define a provider-prefixed field 'model'.")
    api_format = str(config_data.get("api_format") or "completion")
    if api_format not in API_FORMAT_VALUES:
        choices = ", ".join(API_FORMAT_VALUES)
        raise typer.BadParameter(f"current api_format must be one of: {choices}")
    preset: ProviderPreset = {
        "name": provider,
        "description": "Saved from current Bub provider config.",
        "category": "custom",
        "api": FORMAT_TO_API_NAME[api_format],
        "model_provider": provider,
        "api_base": config_data.get("api_base") or "",
        "default_model": model_name,
        "models": [{"id": model_name}],
        "api_key_env": [],
        "requires_api_base": False,
    }
    for field in ("client_args", "fallback_models", "max_tokens", "model_timeout_seconds"):
        if field in config_data:
            preset[field] = config_data[field]
    if include_api_key and config_data.get("api_key"):
        preset["api_key"] = config_data["api_key"]
    return preset


def _apply_provider_profile(config_data: dict[str, Any], profile: ProviderProfile) -> dict[str, Any]:
    updated = dict(config_data)
    for field in CONFIG_PROVIDER_FIELDS:
        if field not in profile:
            continue
        value = profile[field]
        if field in CLEARABLE_CONFIG_PROVIDER_FIELDS and value in (None, ""):
            updated.pop(field, None)
        else:
            updated[field] = value
    return updated


def _write_provider_update(
    target: Path,
    current: dict[str, Any],
    profile: ProviderProfile,
    *,
    backup: bool,
    no_backup: bool,
    restart_docker: bool,
    docker_container: str,
) -> None:
    _warn_if_reusing_api_key(current, profile)
    updated = _apply_provider_profile(current, profile)
    configure.validate(updated)
    backup_path = _backup_file(target) if backup and not no_backup else None
    _write_yaml_file(target, updated)

    typer.echo(f"updated: {target}")
    if backup_path:
        typer.echo(f"backup: {backup_path}")
    _print_provider_status(updated, _all_provider_presets(target))
    if restart_docker:
        _note_docker_hot_load(docker_container)


def _warn_if_reusing_api_key(current: dict[str, Any], profile: ProviderProfile) -> None:
    if "api_key" in profile or not _api_key_present(current.get("api_key")):
        return
    changed = any(
        profile.get(field, current.get(field)) != current.get(field)
        for field in ("model", "api_base", "api_format")
        if field in profile
    )
    if changed:
        typer.secho(
            "api_key was not changed; pass --api-key to replace it or --clear-api-key to remove it.",
            fg="yellow",
        )


def _note_docker_hot_load(name: str) -> None:
    typer.secho(
        f"provider config is hot-loaded by new bub runs; docker restart skipped for '{name}'.",
        fg="yellow",
    )


@provider_app.command("status")
def provider_status(
    ctx: typer.Context,
    config_file: Path | None = typer.Option(None, "--config", help="Path to Bub config.yml"),
) -> None:
    """Show the active provider configuration without printing secrets."""
    framework = ctx.find_root().ensure_object(BubFramework)
    target = (config_file or framework.config_file).expanduser().resolve()
    typer.echo(f"config: {target}")
    _print_provider_status(_load_yaml_file(target), _all_provider_presets(target))


@provider_app.command("list")
def provider_list(
    ctx: typer.Context,
    config_file: Path | None = typer.Option(None, "--config", help="Path to Bub config.yml"),
    templates: bool = typer.Option(False, "--templates", help="Show custom interface templates instead of provider presets."),
) -> None:
    """List built-in and local provider interface presets."""
    framework = ctx.find_root().ensure_object(BubFramework)
    target = (config_file or framework.config_file).expanduser().resolve()
    sources = _provider_preset_sources(target)
    presets = _all_provider_presets(target)
    sorted_items = sorted(
        ((name, preset) for name, preset in presets.items() if _is_custom_interface_template(preset) == templates),
        key=lambda item: (
            PROVIDER_CATEGORY_ORDER.get(str(item[1].get("category") or "custom"), len(PROVIDER_CATEGORY_ORDER)),
            sources.get(item[0], "local"),
            str(item[1].get("name") or item[0]).casefold(),
        ),
    )
    if not sorted_items:
        typer.echo("No custom interface templates found." if templates else "No provider presets found.")
        return
    for name, preset in sorted_items:
        models = _preset_models(preset)
        key_env = _first_key_env(preset)
        aliases = ", ".join(preset.get("aliases") or [])
        typer.echo(_provider_list_header(name, preset, source=sources.get(name, "local")))
        if preset.get("description"):
            typer.echo(f"  {preset.get('description', '')}")
        if aliases:
            typer.echo(f"  aliases: {aliases}")
        typer.echo(f"  api: {preset.get('api')}")
        typer.echo(f"  provider: {preset.get('model_provider')}")
        typer.echo(f"  api_base: {preset.get('api_base') or '(set with --api-base)'}")
        if preset.get("is_full_url"):
            typer.echo("  full_url: true")
        typer.echo(f"  api_key_env: {key_env or '(unset)'}")
        if preset.get("api_key_url"):
            typer.echo(f"  api_key_url: {preset.get('api_key_url')}")
        if preset.get("models_url"):
            typer.echo(f"  models_url: {preset.get('models_url')}")
        typer.echo(f"  default_model: {preset.get('default_model')}")
        typer.echo(f"  models: {', '.join(models[:6])}{' ...' if len(models) > 6 else ''}")


@provider_app.command("show")
def provider_show(
    ctx: typer.Context,
    provider_name: str = typer.Argument(..., help="Provider preset name or alias, for example kimi"),
    config_file: Path | None = typer.Option(None, "--config", help="Path to Bub config.yml"),
) -> None:
    """Show one provider interface preset without printing secrets."""
    framework = ctx.find_root().ensure_object(BubFramework)
    target = (config_file or framework.config_file).expanduser().resolve()
    preset = _resolve_preset(provider_name, _all_provider_presets(target))
    typer.echo(yaml.safe_dump(_redacted_preset_for_print(preset), sort_keys=False, allow_unicode=True).rstrip())


@provider_app.command("models")
def provider_models(
    ctx: typer.Context,
    provider_name: str = typer.Argument(..., help="Provider preset name, for example kimi-coding"),
    config_file: Path | None = typer.Option(None, "--config", help="Path to Bub config.yml"),
) -> None:
    """List models offered by a provider preset."""
    framework = ctx.find_root().ensure_object(BubFramework)
    target = (config_file or framework.config_file).expanduser().resolve()
    preset = _resolve_preset(provider_name, _all_provider_presets(target))
    for entry in _preset_model_entries(preset):
        marker = " (default)" if entry["id"] == preset.get("default_model") else ""
        typer.echo(f"{_format_model_entry(entry)}{marker}")


@provider_app.command("use")
def provider_use(
    ctx: typer.Context,
    provider_name: str | None = typer.Argument(None, help="Provider preset name, for example kimi-coding"),
    model: str | None = typer.Option(None, "--model", help="Model name inside the preset. Full provider:model is also accepted."),
    api_base: str | None = typer.Option(None, "--api-base", help="API base URL"),
    api: str | None = typer.Option(None, "--api", help="openai-completions, openai-responses, or anthropic-messages"),
    api_format: str | None = typer.Option(None, "--api-format", help="completion, responses, or messages"),
    api_key: str | None = typer.Option(None, "--api-key", help="Provider API key. Omit to keep existing key."),
    clear_api_base: bool = typer.Option(False, "--clear-api-base", help="Remove api_base from config."),
    clear_api_key: bool = typer.Option(False, "--clear-api-key", help="Remove api_key from config."),
    keep_client_args: bool = typer.Option(False, "--keep-client-args", help="Keep current client_args unchanged."),
    clear_client_args: bool = typer.Option(False, "--clear-client-args", help="Remove client_args from config."),
    config_file: Path | None = typer.Option(None, "--config", help="Path to Bub config.yml"),
    backup: bool = typer.Option(False, "--backup", help="Back up config before writing."),
    no_backup: bool = typer.Option(False, "--no-backup", help="Deprecated; backups are opt-in by default."),
    restart_docker: bool = typer.Option(False, "--restart-docker", help="Compatibility option; provider changes hot-load without restart."),
    docker_container: str = typer.Option("bub", "--docker-container", help="Docker container name to restart."),
) -> None:
    """Switch provider settings from a provider interface preset."""
    framework = ctx.find_root().ensure_object(BubFramework)
    target = (config_file or framework.config_file).expanduser().resolve()
    current = _load_yaml_file(target)
    presets = _all_provider_presets(target)
    preset = (
        _custom_preset_from_args(model=model, api_base=api_base, api=api, api_format=api_format)
        if provider_name in (None, "custom")
        else _resolve_preset(provider_name, presets)
    )
    profile = _provider_config_from_preset(
        preset,
        current,
        model=model,
        api_base=api_base,
        api=api,
        api_format=api_format,
        api_key=api_key,
        clear_api_base=clear_api_base,
        clear_api_key=clear_api_key,
        keep_client_args=keep_client_args,
        clear_client_args=clear_client_args,
    )
    _write_provider_update(
        target,
        current,
        profile,
        backup=backup,
        no_backup=no_backup,
        restart_docker=restart_docker,
        docker_container=docker_container,
    )


@provider_app.command("choose")
def provider_choose(
    ctx: typer.Context,
    config_file: Path | None = typer.Option(None, "--config", help="Path to Bub config.yml"),
    backup: bool = typer.Option(False, "--backup", help="Back up config before writing."),
    no_backup: bool = typer.Option(False, "--no-backup", help="Deprecated; backups are opt-in by default."),
    restart_docker: bool = typer.Option(False, "--restart-docker", help="Compatibility option; provider changes hot-load without restart."),
    docker_container: str = typer.Option("bub", "--docker-container", help="Docker container name to restart."),
) -> None:
    """Interactively choose provider, API key, and model."""
    framework = ctx.find_root().ensure_object(BubFramework)
    target = (config_file or framework.config_file).expanduser().resolve()
    current = _load_yaml_file(target)
    presets = _all_provider_presets(target)
    current_match = _matching_preset_name(current, presets)
    provider_items = sorted(
        [(name, preset) for name, preset in presets.items() if not _is_custom_interface_template(preset)],
        key=_provider_sort_key,
    )
    template_items = sorted(
        [(name, preset) for name, preset in presets.items() if _is_custom_interface_template(preset)],
        key=_provider_sort_key,
    )
    current_is_template = bool(current_match and current_match in presets and _is_custom_interface_template(presets[current_match]))
    mode_choices = ["Provider"]
    if template_items:
        mode_choices.append("Interface template")
    mode_choices.append("Manual custom")
    default_mode = "Interface template" if current_is_template else "Provider"
    selected_mode = _choose_from_list("Mode", mode_choices, default=default_mode)

    if selected_mode == "Manual custom":
        selected = "custom"
    elif selected_mode == "Interface template":
        selected = _choose_preset_from_list(
            "Template",
            template_items,
            default=current_match if current_is_template else None,
        )
    else:
        selected = _choose_preset_from_list(
            "Provider",
            provider_items,
            default=current_match if current_match and not current_is_template else None,
        )

    if selected == "custom":
        current_model = str(current.get("model") or "")
        model = _prompt_text("Model", default=current_model)
        if not model:
            raise typer.BadParameter("model is required.")
        current_api_base = str(current.get("api_base") or "")
        api_base = _prompt_text("API base (optional)", default=current_api_base)
        current_api_format = str(current.get("api_format") or "completion")
        api_name = _choose_from_list(
            "API",
            list(CANONICAL_API_BY_FORMAT.values()),
            default=FORMAT_TO_API_NAME.get(current_api_format, "openai-completions"),
        )
        api_key, clear_api_key = _prompt_api_key_update(current)
        preset = _custom_preset_from_args(model=model, api_base=api_base or None, api=api_name, api_format=None)
        profile = _provider_config_from_preset(
            preset,
            current,
            model=model,
            api_base=api_base or None,
            api=api_name,
            api_format=None,
            api_key=api_key if api_key else None,
            clear_api_base=False,
            clear_api_key=clear_api_key,
            keep_client_args=True,
            clear_client_args=False,
        )
    else:
        preset = copy.deepcopy(presets[selected])
        models = _preset_models(preset)
        _, current_model_name = _split_model(str(current.get("model") or ""))
        default_model = current_model_name if current_match == selected and current_model_name in models else preset["default_model"]
        model_choices = models + ["custom"]
        selected_model = _choose_from_list("Model", model_choices, default=default_model)
        model = (
            _prompt_text("Custom model", default=str(preset["default_model"]))
            if selected_model == "custom"
            else selected_model
        )
        api_base = None
        if preset.get("requires_api_base"):
            api_base_default = str(
                (current.get("api_base") if current_match == selected else None) or preset.get("api_base") or ""
            )
            api_base = _prompt_text("API base", default=api_base_default)
            if not api_base:
                raise typer.BadParameter("api_base is required for this provider preset.")
        key_env = _first_key_env(preset)
        api_key, clear_api_key = _prompt_api_key_update(current, key_env=key_env)
        profile = _provider_config_from_preset(
            preset,
            current,
            model=model,
            api_base=api_base,
            api=None,
            api_format=None,
            api_key=api_key if api_key else None,
            clear_api_base=False,
            clear_api_key=clear_api_key,
            keep_client_args=False,
            clear_client_args=False,
        )

    _write_provider_update(
        target,
        current,
        profile,
        backup=backup,
        no_backup=no_backup,
        restart_docker=restart_docker,
        docker_container=docker_container,
    )


@provider_app.command("save")
def provider_save(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Local provider preset name, for example my-provider"),
    description: str | None = typer.Option(None, "--description", help="Optional preset description."),
    include_api_key: bool = typer.Option(False, "--include-api-key", help="Store the current api_key in this local preset."),
    api_key_env: str | None = typer.Option(None, "--api-key-env", help="API key env hint to store in this preset."),
    config_file: Path | None = typer.Option(None, "--config", help="Path to Bub config.yml"),
) -> None:
    """Save the current provider interface as a local preset without secrets by default."""
    framework = ctx.find_root().ensure_object(BubFramework)
    target = (config_file or framework.config_file).expanduser().resolve()
    current = _load_yaml_file(target)
    preset_name = _sanitize_preset_name(name)
    preset = _provider_config_from_current(current, include_api_key=include_api_key)
    if description:
        preset["description"] = description
    if api_key_env:
        preset["api_key_env"] = [api_key_env]
    preset_path = _provider_presets_dir(target) / f"{preset_name}.yml"
    _write_yaml_file(preset_path, preset)
    typer.echo(f"saved: {preset_path}")
    _print_provider_status(current, {preset_name: preset})


@lru_cache(maxsize=1)
def _find_uv() -> str:
    import shutil
    import sysconfig

    default_path = Path.home() / ".local" / "bin"

    bin_path = sysconfig.get_path("scripts")
    uv_path = shutil.which("uv", path=os.pathsep.join([bin_path, str(default_path), os.getenv("PATH", "")]))
    if uv_path is None:
        raise FileNotFoundError("uv executable not found in PATH or scripts directory.")
    return uv_path


@lru_cache(maxsize=1)
def _default_project() -> Path:
    import bub

    project = bub.home / "bub-project"
    project.mkdir(exist_ok=True, parents=True)
    return project


def _is_in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


project_opt = typer.Option(
    default_factory=_default_project,
    help="Path to the project directory (default: ~/.bub/bub-project)",
    envvar="BUB_PROJECT",
)


def _uv(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    uv_executable = _find_uv()
    if not _is_in_venv():
        typer.secho("Please install Bub in a virtual environment to use this command.", err=True, fg="red")
        raise typer.Exit(1)
    env = {**os.environ, "VIRTUAL_ENV": sys.prefix}
    try:
        return subprocess.run([uv_executable, *args], env=env, check=True, cwd=cwd)
    except subprocess.CalledProcessError as e:
        typer.secho(f"Command 'uv {' '.join(args)}' failed with exit code {e.returncode}.", err=True, fg="red")
        raise typer.Exit(e.returncode) from e


BUB_CONTRIB_REPO = "https://github.com/bubbuild/bub-contrib.git"


def _build_requirement(spec: str) -> str:
    if spec.startswith(("git@", "https://")):
        # Git URL
        return f"git+{spec}"
    elif "/" in spec:
        # owner/repo format
        repo, *rest = spec.partition("@")
        ref = "".join(rest)
        return f"git+https://github.com/{repo}.git{ref}"
    else:
        # Assume it's a package name in bub-contrib
        name, has_ref, ref = spec.partition("@")
        if has_ref:
            ref = f"@{ref}"
            return f"git+{BUB_CONTRIB_REPO}{ref}#subdirectory=packages/{name}"
        else:  # PyPI package name
            return name


def _build_local_requirement_path(url: str, subdirectory: str | None = None) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "file":
        return None

    path = parsed.path
    if parsed.netloc and parsed.netloc != "localhost":
        path = f"//{parsed.netloc}{path}"
    local_path = Path(url2pathname(unquote(path)))
    if subdirectory:
        local_path /= subdirectory
    return os.fspath(local_path)


def _build_bub_requirement() -> list[str]:
    dist = metadata.distribution("bub")
    dist_name = dist.name
    direct_url_text = dist.read_text("direct_url.json")
    if not direct_url_text:
        return [dist_name]

    direct_url = json.loads(direct_url_text)
    requirement_url = str(direct_url["url"])
    subdirectory = direct_url.get("subdirectory")
    normalized_subdirectory = subdirectory if isinstance(subdirectory, str) and subdirectory else None

    local_path = _build_local_requirement_path(requirement_url, normalized_subdirectory)
    if local_path is not None:
        dir_info = direct_url.get("dir_info")
        editable = isinstance(dir_info, dict) and bool(dir_info.get("editable"))
        return ["--editable", local_path] if editable else [local_path]

    vcs_info = direct_url.get("vcs_info")
    if isinstance(vcs_info, dict):
        vcs = vcs_info.get("vcs")
        requested_revision = vcs_info.get("requested_revision")
        if isinstance(vcs, str) and vcs:
            requirement_url = f"{vcs}+{requirement_url}"
        if isinstance(requested_revision, str) and requested_revision:
            requirement_url = f"{requirement_url}@{requested_revision}"

    if normalized_subdirectory:
        requirement_url = f"{requirement_url}#subdirectory={normalized_subdirectory}"

    return [requirement_url]


def _ensure_project(project: Path) -> None:
    if (project / "pyproject.toml").is_file():
        return
    _uv("init", "--bare", "--name", "bub-project", "--app", cwd=project)
    bub_requirement = _build_bub_requirement()
    _uv("add", "--active", "--no-sync", *bub_requirement, cwd=project)


def install(
    specs: list[str] = typer.Argument(
        default_factory=list,
        help="Package specification to install, can be a git URL, owner/repo, or package name in bub-contrib.",
    ),
    project: Path = project_opt,
) -> None:
    """Install a plugin into Bub's environment, or sync the environment if no specifications are provided."""
    _ensure_project(project)
    if not specs:
        _uv("sync", "--active", "--inexact", cwd=project)
    else:
        _uv("add", "--active", *map(_build_requirement, specs), cwd=project)


def uninstall(
    packages: list[str] = typer.Argument(..., help="Package name to uninstall (must match the name in pyproject.toml)"),
    project: Path = project_opt,
) -> None:
    """Uninstall a plugin from Bub's environment."""
    _ensure_project(project)
    _uv("remove", "--active", *packages, cwd=project)


def update(
    packages: list[str] = typer.Argument(
        default_factory=list, help="Optional package name to update (must match the name in pyproject.toml)"
    ),
    project: Path = project_opt,
) -> None:
    """Update selected package or all packages in Bub's environment."""
    _ensure_project(project)
    if not packages:
        _uv("sync", "--active", "--upgrade", "--inexact", cwd=project)
    else:
        package_args: list[str] = []
        for pkg in packages:
            package_args.extend(["--upgrade-package", pkg])
        _uv("sync", "--active", "--inexact", *package_args, cwd=project)
