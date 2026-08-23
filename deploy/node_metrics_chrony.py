#!/usr/bin/env python3
"""Chrony status endpoint for the MFU node metrics agent.

Only the commands listed in ``CHRONY_COMMANDS`` can be executed.  Request
parameters are deliberately ignored so this endpoint cannot become a remote
shell.
"""

from datetime import datetime, timezone
import os
import shutil
import subprocess

from flask import jsonify


CHRONY_COMMANDS = {
    "tracking": ["chronyc", "tracking"],
    "sources": ["chronyc", "sources", "-v"],
    "selectdata": ["chronyc", "selectdata", "-v"],
    "clients": ["chronyc", "clients"],
    "clients_numeric": ["chronyc", "-n", "clients"],
    "serverstats": ["chronyc", "serverstats"],
}


def _run_fixed(command, timeout=5):
    env = os.environ.copy()
    env.update({"LC_ALL": "C", "LANG": "C"})
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": "chronyc command timed out",
        }
    except Exception as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
        }


def register_chrony_endpoint(app):
    @app.get("/chrony")
    def chrony_status():
        if shutil.which("chronyc") is None:
            return jsonify({
                "ok": False,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "error": "chronyc is not installed",
                "commands": {},
            }), 503

        commands = {
            name: _run_fixed(command)
            for name, command in CHRONY_COMMANDS.items()
        }
        required = ("tracking", "sources", "selectdata", "clients", "serverstats")
        ok = all(commands[name]["ok"] for name in required)
        return jsonify({
            "ok": ok,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "host": os.uname().nodename,
            "commands": commands,
        }), 200 if ok else 503

