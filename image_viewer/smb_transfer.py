"""Copy a selected local image to a pre-mounted, authenticated SMB share."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path


class TransferError(RuntimeError):
    pass


def copy_image(source: Path, display_name: str, mount_root: Path) -> str:
    root = Path(mount_root)
    if not root.is_dir() or not os.path.ismount(root):
        raise TransferError("ラズパイのSMB共有に接続されていません。")
    if not source.is_file():
        raise TransferError("画像が見つかりません。")
    if not display_name or Path(display_name).name != display_name or display_name in {".", ".."}:
        raise TransferError("ファイル名を確認してください。")
    temporary = root / f".mfu-transfer-{uuid.uuid4().hex}.part"
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        stem = Path(display_name).stem
        suffix = Path(display_name).suffix
        for number in range(1, 10001):
            name = display_name if number == 1 else f"{stem} ({number - 1}){suffix}"
            destination = root / name
            try:
                # Exclusive reservation prevents a simultaneous transfer from
                # silently replacing an existing image. The staged file is
                # swapped into place immediately after reservation.
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
            except FileExistsError:
                continue
            else:
                os.close(fd)
                try:
                    os.replace(temporary, destination)
                except Exception:
                    destination.unlink(missing_ok=True)
                    raise
                return name
        raise TransferError("同名ファイルが多すぎるため保存できません。")
    finally:
        temporary.unlink(missing_ok=True)
