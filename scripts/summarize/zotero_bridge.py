#!/usr/bin/env python3
"""Zotero 桥：直接复用本机 zotero-mcp 的工具函数（不经过 MCP 协议）。

找不到服务文件时返回 ok=False，调用方按"文献入口不可用"降级，不抛异常。
路径可用环境变量 ZOTERO_MCP_SERVER 覆盖。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

DEFAULT_SERVER = r"D:\kb-services\zotero-mcp\server.py"
_MOD = None


def _server():
    global _MOD
    if _MOD is not None:
        return _MOD
    path = Path(os.environ.get("ZOTERO_MCP_SERVER", DEFAULT_SERVER))
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("zotero_mcp_server", str(path))
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        _MOD = mod
        return mod
    except Exception:  # noqa: BLE001
        return None


def _call(name: str, **kwargs) -> dict:
    mod = _server()
    if mod is None:
        return {"ok": False, "reason": f"zotero-mcp not found (ZOTERO_MCP_SERVER={DEFAULT_SERVER})"}
    fn = getattr(mod, name, None)
    if fn is None:
        return {"ok": False, "reason": f"tool {name} missing in zotero-mcp"}
    try:
        out = fn(**kwargs)
        data = json.loads(out) if isinstance(out, str) else out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"ok": False, "reason": f"unexpected reply: {type(data).__name__}"}
    if data.get("error"):
        return {"ok": False, "reason": data["error"]}
    data.setdefault("ok", True)   # 底层说不可用就是不可用，不要覆盖
    return data


def status() -> dict:
    return _call("zotero_status")


def bulk_export(collection: str = "", since: str = "", limit: int = 300) -> dict:
    return _call("bulk_export", collection=collection, since=since, limit=limit)


def search(query: str, limit: int = 20, scope: str = "metadata") -> dict:
    return _call("search_items", query=query, limit=limit, scope=scope)


def fulltext(item_key: str, max_chars: int = 400000) -> dict:
    return _call("get_fulltext", item_key=item_key, max_chars=max_chars)


def recent(days: int = 30, limit: int = 50) -> dict:
    return _call("recent_items", days=days, limit=limit)


def health_hint() -> str:
    data = status()
    if data.get("ok"):
        return f"Zotero 可用：{data.get('items', 0)} 条记录，数据目录 {data.get('data_dir')}"
    steps = data.get("how_to_fix") or [data.get("reason", "unknown")]
    return "Zotero 不可用：" + "；".join(steps)


if __name__ == "__main__":
    print(health_hint())
    sys.exit(0)
