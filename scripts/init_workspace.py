#!/usr/bin/env python3
"""init_workspace.py —— 给一个新领域铺运行工作区（幂等，不覆盖已有文件）

用法：
  python init_workspace.py <目标目录> [--domain 名称]

产物：
  <目标>/研究档案/{00-总进度.md, 仪器台账.md, 缺口登记簿.md, 能力画像.md}
  <目标>/研究档案/instruments.json     # 空台账（phase_gate 的真相源）
  <目标>/结果/_模板-结果档案.md
  <目标>/{探索,仪器,用户}/            # 空目录
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
TPL = SKILL / "assets" / "workspace-template"

DIRS = ["研究档案", "结果", "探索", "仪器", "用户"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--domain", default="")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的模板文件")
    a = ap.parse_args()

    root = Path(a.target).resolve()
    if not TPL.exists():
        print(f"模板目录缺失：{TPL}")
        return 2
    root.mkdir(parents=True, exist_ok=True)
    for d in DIRS:
        (root / d).mkdir(exist_ok=True)

    created, skipped = [], []
    for src in TPL.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(TPL)
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and not a.force:
            skipped.append(str(rel))
            continue
        text = src.read_text(encoding="utf-8")
        if a.domain and rel.name == "00-总进度.md":
            text = text.replace("| 领域 | |", f"| 领域 | {a.domain} |")
        dst.write_text(text, encoding="utf-8")
        created.append(str(rel))

    ledger = root / "研究档案" / "instruments.json"
    if not ledger.exists():
        ledger.write_text(json.dumps({"instruments": {}}, ensure_ascii=False, indent=1), encoding="utf-8")
        created.append("研究档案/instruments.json")

    print(f"工作区：{root}")
    print(f"新建 {len(created)} 个文件：{', '.join(created) if created else '-'}")
    if skipped:
        print(f"已存在跳过 {len(skipped)} 个：{', '.join(skipped)}")
    print("\n下一步：在 00-总进度.md 填定位，然后判断是否触发 I 阶段：")
    print(f"  python {SKILL / 'scripts' / 'phase_gate.py'} should-I <命中的判据>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
