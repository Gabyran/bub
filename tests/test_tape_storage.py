from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from republic.tape import InMemoryTapeStore

from bub.builtin.store import ForkTapeStore
from bub.builtin.tape import TapeService


class _FakeTape:
    def __init__(self, name: str) -> None:
        self.name = name
        self.context = SimpleNamespace(state={})


class _FakeLLM:
    def tape(self, name: str) -> _FakeTape:
        return _FakeTape(name)


def test_session_tape_uses_shared_scope_across_workspaces(tmp_path: Path) -> None:
    service = TapeService(_FakeLLM(), tmp_path / "archive", ForkTapeStore(InMemoryTapeStore()), shared_scope="shared")

    local_tape = service.session_tape("cli_session", tmp_path / "local")
    server_tape = service.session_tape("cli_session", tmp_path / "server")

    assert local_tape.name == server_tape.name
