from __future__ import annotations

from pathlib import Path

import pytest

from bub import configure


def test_load_yaml_mapping_rejects_duplicate_keys(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        "model: openai:kimi-for-coding\n"
        "api_key: first\n"
        "api_key: second\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate YAML key 'api_key'"):
        configure.load_yaml_mapping(config_file)


def test_load_yaml_mapping_reads_mapping(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        "model: openai:kimi-for-coding\n"
        "api_key: test-key\n",
        encoding="utf-8",
    )

    assert configure.load_yaml_mapping(config_file) == {
        "model": "openai:kimi-for-coding",
        "api_key": "test-key",
    }
