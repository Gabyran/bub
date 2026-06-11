from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class TapeSyncConfig:
    local_path: Path
    bucket: str
    endpoint: str
    access_key_id: str
    secret_access_key: str
    key: str = "tapes.db"
    region: str = "auto"
    root: str = ""
    service: str = "s3"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TapeSyncConfig | None:
        source = env or os.environ
        bucket = source.get("BUB_TAPE_SYNC_BUCKET")
        endpoint = source.get("BUB_TAPE_SYNC_ENDPOINT")
        access_key_id = source.get("BUB_TAPE_SYNC_ACCESS_KEY_ID")
        secret_access_key = source.get("BUB_TAPE_SYNC_SECRET_ACCESS_KEY")

        if not bucket or not endpoint or not access_key_id or not secret_access_key:
            return None

        home = Path(source.get("BUB_HOME", str(Path.home() / ".bub"))).expanduser()
        local_path = Path(source.get("BUB_TAPE_SYNC_LOCAL_PATH", str(home / "tapes.db"))).expanduser()

        return cls(
            local_path=local_path,
            bucket=bucket,
            endpoint=endpoint,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            key=source.get("BUB_TAPE_SYNC_KEY", "tapes.db"),
            region=source.get("BUB_TAPE_SYNC_REGION", "auto"),
            root=source.get("BUB_TAPE_SYNC_ROOT", ""),
            service=source.get("BUB_TAPE_SYNC_PROVIDER", "s3"),
        )


def _build_operator(config: TapeSyncConfig):
    try:
        import opendal
    except ImportError as exc:  # pragma: no cover - depends on runtime install
        raise RuntimeError("OpenDAL is not installed in this environment.") from exc

    options = {
        "bucket": config.bucket,
        "endpoint": config.endpoint,
        "access_key_id": config.access_key_id,
        "secret_access_key": config.secret_access_key,
        "region": config.region,
    }
    if config.root:
        options["root"] = config.root

    return opendal.Operator(config.service, **options)


def _remote_key(config: TapeSyncConfig) -> str:
    return config.key.lstrip("/")


def pull(config: TapeSyncConfig | None = None) -> int:
    config = config or TapeSyncConfig.from_env()
    if config is None:
        return 0

    operator = _build_operator(config)
    remote_key = _remote_key(config)

    try:
        content = operator.read(remote_key)
    except Exception as exc:
        from opendal.exceptions import NotFound  # type: ignore

        if isinstance(exc, NotFound) or "not found" in str(exc).lower():
            return 0
        print(f"tape sync pull failed: {exc}", file=sys.stderr)
        return 1

    config.local_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(config.local_path.parent)) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.replace(config.local_path)
    return 0


def push(config: TapeSyncConfig | None = None) -> int:
    config = config or TapeSyncConfig.from_env()
    if config is None:
        return 0

    if not config.local_path.is_file():
        return 0

    operator = _build_operator(config)
    remote_key = _remote_key(config)

    try:
        operator.write(remote_key, config.local_path.read_bytes())
    except Exception as exc:
        print(f"tape sync push failed: {exc}", file=sys.stderr)
        return 1
    return 0


def sync(config: TapeSyncConfig | None = None) -> int:
    result = pull(config)
    if result != 0:
        return result
    return push(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bub.tape_sync")
    parser.add_argument("direction", choices=("pull", "push", "sync"))
    args = parser.parse_args(argv)

    if args.direction == "pull":
        return pull()
    if args.direction == "push":
        return push()
    return sync()


if __name__ == "__main__":
    raise SystemExit(main())
