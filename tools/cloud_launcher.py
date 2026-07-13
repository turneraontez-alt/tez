#!/usr/bin/env python3
"""Load Q15 env files with local-runner semantics, then exec a command."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re


ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"missing Q15 environment file: {path}")
    values: dict[str, str] = {}
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8-sig", errors="strict").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid environment line {path}:{line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not ENV_KEY.fullmatch(key):
            raise ValueError(f"invalid environment key {path}:{line_number}")
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {'"', "'"}
        ):
            value = value[1:-1]
        values[key] = value
    return values


def load_environment(paths: list[Path]) -> dict[str, str]:
    environment = dict(os.environ)
    for path in paths:
        environment.update(read_env_file(path))
    return environment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", action="append", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")
    os.execvpe(command[0], command, load_environment(args.config))
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
