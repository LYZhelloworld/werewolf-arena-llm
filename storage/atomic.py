"""原子写：先写同目录临时文件再 ``os.replace``，避免读到半截文件。

``events.json`` / ``meta.json`` / ``index.json`` 都是整体覆盖写，
崩溃时留下一个语法损坏的 JSON 会直接毁掉复盘能力，所以必须原子替换。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any) -> None:
    """把 ``data`` 以 UTF-8 JSON 原子写入 ``path``。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
