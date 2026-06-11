from __future__ import annotations

from pathlib import Path

from bub.tape_sync import TapeSyncConfig


def test_tape_sync_config_defaults_to_bub_home(tmp_path: Path) -> None:
    config = TapeSyncConfig.from_env(
        {
            "BUB_HOME": str(tmp_path),
            "BUB_TAPE_SYNC_BUCKET": "bucket",
            "BUB_TAPE_SYNC_ENDPOINT": "https://example.invalid",
            "BUB_TAPE_SYNC_ACCESS_KEY_ID": "key",
            "BUB_TAPE_SYNC_SECRET_ACCESS_KEY": "secret",
        }
    )

    assert config is not None
    assert config.local_path == tmp_path / "tapes.db"
    assert config.key == "tapes.db"


def test_tape_sync_config_returns_none_without_credentials() -> None:
    assert TapeSyncConfig.from_env({}) is None
