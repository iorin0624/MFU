#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import os
import shlex
import subprocess
import sys


IPSET = "/usr/sbin/ipset"
NETFILTER_PERSISTENT = "/usr/sbin/netfilter-persistent"


def parse_original_command(raw_command: str) -> tuple[int, str, str, str]:
    parts = shlex.split(raw_command or "")
    if len(parts) != 3 or parts[0] != "ban":
        raise ValueError("only 'ban <4|6> <CIDR>' is allowed")

    try:
        requested_version = int(parts[1])
    except ValueError as exc:
        raise ValueError("IP version must be 4 or 6") from exc

    if requested_version not in (4, 6):
        raise ValueError("IP version must be 4 or 6")

    network = ipaddress.ip_network(parts[2], strict=False)
    if network.version != requested_version:
        raise ValueError("IP version does not match CIDR")

    if network.version == 4:
        return 4, str(network), "badhosts4", "inet"
    return 6, str(network), "badhosts6", "inet6"


def apply_ban(version: int, target: str, set_name: str, family: str) -> str:
    subprocess.run(
        [IPSET, "create", set_name, "hash:net", "family", family, "-exist"],
        check=True,
    )
    tested = subprocess.run(
        [IPSET, "test", set_name, target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if tested.returncode == 0:
        status = "ALREADY"
    elif tested.returncode == 1:
        subprocess.run([IPSET, "add", set_name, target, "-exist"], check=True)
        status = "ADDED"
    else:
        raise RuntimeError(f"ipset test failed with rc={tested.returncode}")

    subprocess.run([NETFILTER_PERSISTENT, "save"], check=True)
    return f"{status} {target} IPv{version}"


def main() -> int:
    try:
        parsed = parse_original_command(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        print(apply_ban(*parsed))
        return 0
    except Exception as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
