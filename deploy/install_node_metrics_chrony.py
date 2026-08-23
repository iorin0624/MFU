#!/usr/bin/env python3
"""Idempotently enable the Chrony endpoint in node_metrics_agent.py."""

from pathlib import Path
import shutil
from datetime import datetime


AGENT = Path("/opt/node-metrics/node_metrics_agent.py")
MODULE = Path("/opt/node-metrics/node_metrics_chrony.py")
MARKER = "register_chrony_endpoint(app)"
ANCHOR = "app = Flask(__name__)\n"
INSERT = (
    "app = Flask(__name__)\n"
    "\n"
    "# Fixed-command Chrony monitoring endpoint (managed by MFU).\n"
    "from node_metrics_chrony import register_chrony_endpoint\n"
    "register_chrony_endpoint(app)\n"
)


def main():
    if not AGENT.exists():
        raise SystemExit(f"agent not found: {AGENT}")
    if not MODULE.exists():
        raise SystemExit(f"module not found: {MODULE}")

    text = AGENT.read_text(encoding="utf-8")
    if MARKER in text:
        print("Chrony endpoint is already installed")
        return
    if ANCHOR not in text:
        raise SystemExit("Flask app anchor was not found")

    backup = AGENT.with_name(
        AGENT.name + ".bak_chrony_" + datetime.now().strftime("%Y%m%d%H%M%S")
    )
    shutil.copy2(AGENT, backup)
    AGENT.write_text(text.replace(ANCHOR, INSERT, 1), encoding="utf-8")
    print(f"Installed Chrony endpoint; backup={backup}")


if __name__ == "__main__":
    main()

