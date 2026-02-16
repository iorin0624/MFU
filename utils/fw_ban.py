from __future__ import annotations

import os
import subprocess
from ipaddress import ip_address, ip_network
from typing import Any, Dict


def normalize_ipv4_target(*, cidr: str = "", ip: str = "") -> str:
    """IPv4 CIDRに正規化。IP単体は /24 に丸める。"""
    cidr_raw = (cidr or "").strip()
    ip_raw = (ip or "").strip()

    if cidr_raw:
        net = ip_network(cidr_raw, strict=False)
        if net.version != 4:
            raise ValueError("IPv4のみ対応")
        return net.with_prefixlen

    if ip_raw:
        ipobj = ip_address(ip_raw)
        if ipobj.version != 4:
            raise ValueError("IPv4のみ対応")
        return ip_network(f"{ipobj}/24", strict=False).with_prefixlen

    raise ValueError("cidr または ip が必要です")


def ban_ipv4_cidr_via_ssh(target_cidr: str) -> Dict[str, Any]:
    """
    103.15 側へ SSH して badhosts に CIDR を追加し永続化する。
    戻り値は /admin/fw/ban 互換に合わせる。
    """
    target = normalize_ipv4_target(cidr=target_cidr)

    host = os.getenv("FW_BAN_HOST", "192.168.103.15")
    user = os.getenv("FW_BAN_USER", "root")
    ssh_connect_timeout = int(os.getenv("FW_BAN_SSH_CONNECT_TIMEOUT", "5"))
    ssh_exec_timeout = int(os.getenv("FW_BAN_SSH_EXEC_TIMEOUT", "12"))

    remote_cmd = (
        "PATH=/usr/sbin:/sbin:/usr/bin:/bin; "
        f"if ipset -q test badhosts {target}; then "
        "  echo ALREADY; "
        "else "
        f"  ipset add badhosts {target} -exist && echo ADDED; "
        "fi; "
        "netfilter-persistent save >/dev/null 2>&1 || true"
    )

    ssh_cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={ssh_connect_timeout}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{user}@{host}",
        remote_cmd,
    ]

    try:
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=ssh_exec_timeout)
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "status": "timeout",
            "target": target,
            "message": str(e),
            "stdout": "",
            "stderr": "",
        }

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode == 0:
        if "ADDED" in stdout:
            status = "added"
        elif "ALREADY" in stdout:
            status = "already"
        else:
            status = "ok"
        return {
            "ok": True,
            "status": status,
            "target": target,
            "stdout": stdout,
            "stderr": stderr,
        }

    return {
        "ok": False,
        "status": "error",
        "rc": proc.returncode,
        "target": target,
        "stdout": stdout,
        "stderr": stderr,
    }
