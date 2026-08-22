from __future__ import annotations

import os
import shlex
import subprocess
from ipaddress import ip_address, ip_network
from typing import Any, Dict


def run_ssh_command(cmd: str, meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """FW_BAN_HOST へ SSH でコマンドを実行し、互換フォーマットで返す。"""
    host = os.getenv("FW_BAN_HOST", "192.168.103.15")
    user = os.getenv("FW_BAN_USER", "root")
    identity_file = os.getenv("FW_BAN_IDENTITY_FILE", "/mnt/mfu/ssh/fw_ban_ed25519")
    known_hosts_file = os.getenv("FW_BAN_KNOWN_HOSTS", "/mnt/mfu/ssh/known_hosts")
    ssh_home = os.getenv("FW_BAN_SSH_HOME", "/mnt/mfu/tmp")
    ssh_connect_timeout = int(os.getenv("FW_BAN_SSH_CONNECT_TIMEOUT", "5"))
    ssh_exec_timeout = int(os.getenv("FW_BAN_SSH_EXEC_TIMEOUT", "12"))

    ssh_cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={ssh_connect_timeout}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        identity_file,
        f"{user}@{host}",
        cmd,
    ]

    meta = meta or {}
    target = meta.get("target", "")
    executor = f"{user}@{host}"
    merged_meta = {"executor": executor, "via": "ssh", **meta}
    process_env = os.environ.copy()
    process_env["HOME"] = ssh_home

    try:
        proc = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=ssh_exec_timeout,
            env=process_env,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "status": "timeout",
            "target": target,
            "message": str(e),
            "stdout": "",
            "stderr": "",
            **merged_meta,
        }

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode == 0:
        status = "ok"
        if "ADDED" in stdout:
            status = "added"
        elif "ALREADY" in stdout:
            status = "already"
        elif "REMOVED" in stdout:
            status = "removed"
        elif "MISSING" in stdout:
            status = "missing"
        return {
            "ok": True,
            "status": status,
            "target": target,
            "stdout": stdout,
            "stderr": stderr,
            **merged_meta,
        }

    return {
        "ok": False,
        "status": "error",
        "rc": proc.returncode,
        "target": target,
        "stdout": stdout,
        "stderr": stderr,
        **merged_meta,
    }


def normalize_ip_target(*, cidr: str = "", ip: str = "") -> Dict[str, Any]:
    """IPv4/IPv6 を自動判定して正規化する。"""
    cidr_raw = (cidr or "").strip()
    ip_raw = (ip or "").strip()

    if not cidr_raw and not ip_raw:
        raise ValueError("cidr または ip が必要です")

    s = cidr_raw or ip_raw
    if "/" in s:
        net = ip_network(s, strict=False)
    else:
        ipobj = ip_address(s)
        suffix = 32 if ipobj.version == 4 else 128
        net = ip_network(f"{ipobj}/{suffix}", strict=False)

    if net.version == 4 and ("/" not in s or net.prefixlen > 24):
        net = ip_network(f"{net.network_address}/24", strict=False)

    return {"version": net.version, "target": str(net)}


def normalize_ipv4_target(*, cidr: str = "", ip: str = "") -> str:
    """IPv4 CIDRに正規化。IP単体は /24 に丸める。"""
    normalized = normalize_ip_target(cidr=cidr, ip=ip)
    if normalized["version"] != 4:
        raise ValueError("IPv4のみ対応")
    return str(normalized["target"])


def ban_ip_cidr_via_ssh(target: Dict[str, Any]) -> Dict[str, Any]:
    """制限付きSSH鍵を使い、IPバージョン別のipsetへBANを追加する。"""
    version = int(target.get("version", 0))
    net = str(target.get("target", "")).strip()

    if version == 4:
        setname = "badhosts4"
        family = "inet"
    elif version == 6:
        setname = "badhosts6"
        family = "inet6"
    else:
        raise ValueError("version は 4 または 6 を指定してください")

    cmd = f"ban {version} {shlex.quote(net)}"
    return run_ssh_command(cmd, meta={"setname": setname, "target": net})


def temporarily_ban_ip_cidr_via_ssh(
    target: Dict[str, Any],
    *,
    timeout_sec: int,
) -> Dict[str, Any]:
    """Add an automatically detected address to the expiring firewall set."""
    version = int(target.get("version", 0))
    net = str(target.get("target", "")).strip()
    timeout = max(60, min(604800, int(timeout_sec)))

    if version == 4:
        setname = "badhosts4_auto"
    elif version == 6:
        setname = "badhosts6_auto"
    else:
        raise ValueError("version は 4 または 6 を指定してください")

    cmd = f"autoban {version} {shlex.quote(net)} {timeout}"
    return run_ssh_command(
        cmd,
        meta={"setname": setname, "target": net, "timeout_sec": timeout},
    )


def permanently_ban_ip_cidr_via_ssh(target: Dict[str, Any]) -> Dict[str, Any]:
    """Add an automatically escalated address to its dedicated persistent set."""
    version = int(target.get("version", 0))
    net = str(target.get("target", "")).strip()
    if version == 4:
        setname = "badhosts4_auto_permanent"
    elif version == 6:
        setname = "badhosts6_auto_permanent"
    else:
        raise ValueError("version は 4 または 6 を指定してください")
    cmd = f"autopermaban {version} {shlex.quote(net)}"
    return run_ssh_command(cmd, meta={"setname": setname, "target": net})


def unban_auto_permanent_ip_cidr_via_ssh(target: Dict[str, Any]) -> Dict[str, Any]:
    """Remove an address only from the dedicated automatic permanent set."""
    version = int(target.get("version", 0))
    net = str(target.get("target", "")).strip()
    if version == 4:
        setname = "badhosts4_auto_permanent"
    elif version == 6:
        setname = "badhosts6_auto_permanent"
    else:
        raise ValueError("version は 4 または 6 を指定してください")
    cmd = f"autopermunban {version} {shlex.quote(net)}"
    return run_ssh_command(cmd, meta={"setname": setname, "target": net})


def ban_ipv4_cidr_via_ssh(target_cidr: str) -> Dict[str, Any]:
    """
    103.15 側へ SSH して badhosts に CIDR を追加し永続化する。
    戻り値は /admin/fw/ban 互換に合わせる。
    """
    target = normalize_ipv4_target(cidr=target_cidr)
    return ban_ip_cidr_via_ssh({"version": 4, "target": target})
