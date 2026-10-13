#!/usr/bin/env python3
"""Add newly provisioned au PAY source tokens to the Raspberry Pi bridge."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


FEATURES = ("au_pay_usage", "au_pay_otp")


def install(config_path: Path, token_path: Path) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    incoming = json.loads(token_path.read_text(encoding="utf-8"))
    if not isinstance(config.get("tokens"), dict) or set(incoming) != set(FEATURES):
        raise ValueError("unexpected bridge config or au PAY token file")
    for feature in FEATURES:
        token = str(incoming[feature])
        if not token.startswith("mfu_ns_"):
            raise ValueError(f"invalid token for {feature}")
        existing = config["tokens"].get(feature)
        if existing and existing != token:
            raise ValueError(f"different token already configured for {feature}")
        config["tokens"][feature] = token
    temporary = config_path.with_name(config_path.name + ".au-pay-tmp")
    if temporary.exists():
        raise FileExistsError(temporary)
    original_stat = config_path.stat()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chown(temporary, original_stat.st_uid, original_stat.st_gid)
    os.chmod(temporary, original_stat.st_mode & 0o777)
    os.replace(temporary, config_path)
    token_path.unlink()
    print("installed au PAY bridge tokens")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--tokens", required=True, type=Path)
    args = parser.parse_args()
    install(args.config, args.tokens)
