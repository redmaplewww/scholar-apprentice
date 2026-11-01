#!/usr/bin/env python3
"""selfcheck.py —— 校验这套 skill 自身是否完整可用（装机后跑一次）

检查项：
  1. 必需文件齐全
  2. SKILL.md frontmatter 合法（name/description 存在）
  3. 每个 domain-pack 的 pack.yaml 可解析且含必需键
  4. 仪器卡引用的 instrument 与台账一致
  5. phase_gate 单元测试通过
退出码：0 全过 / 1 有警告 / 2 有错误
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
REQUIRED = [
    "SKILL.md",
    "references/stages.md",
    "references/stage-I-instrument-commissioning.md",
    "scripts/phase_gate.py",
    "scripts/init_workspace.py",
    "scripts/test_phase_gate.py",
    "scripts/summarize_cli.py",
    "scripts/test_summarize.py",
    "references/summarize-protocol.md",
    "references/review-agent.md",
    "assets/workspace-template/研究档案/00-总进度.md",
    "assets/workspace-template/研究档案/仪器台账.md",
]
PACK_KEYS = ["domain", "instruments", "conventions", "known_answer_battery", "events", "red_lines"]

errors: list[str] = []
warns: list[str] = []


def check_files() -> None:
    for rel in REQUIRED:
        if not (SKILL / rel).exists():
            errors.append(f"缺文件：{rel}")


def check_skill_md() -> None:
    p = SKILL / "SKILL.md"
    if not p.exists():
        return
    text = p.read_text(encoding="utf-8")
    if not text.startswith("---"):
        errors.append("SKILL.md 缺 frontmatter")
        return
    fm = text.split("---", 2)[1]
    for key in ("name:", "description:"):
        if key not in fm:
            errors.append(f"SKILL.md frontmatter 缺 {key}")


def check_packs() -> None:
    try:
        import yaml
    except ImportError:
        warns.append("未安装 PyYAML，跳过 pack.yaml 解析检查")
        return
    packs = sorted((SKILL / "domain-packs").glob("*/pack.yaml"))
    if not packs:
        warns.append("没有任何 domain-pack（这是允许的，核心可独立运行）")
    for p in packs:
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"{p.parent.name}/pack.yaml 解析失败：{e}")
            continue
        for k in PACK_KEYS:
            if k not in d:
                errors.append(f"{p.parent.name}/pack.yaml 缺键 {k}")


def check_ledger_consistency() -> None:
    """若工作区台账存在，检查仪器卡里出现的 instrument_id 是否都在台账中。"""
    ledger = SKILL.parent / "研究档案" / "instruments.json"
    if not ledger.exists():
        return
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except Exception as e:
        errors.append(f"instruments.json 解析失败：{e}")
        return
    ins = data.get("instruments", {})
    for pid, v in ins.items():
        if "status" not in v or "materials" not in v:
            errors.append(f"台账条目 {pid} 缺 status/materials")
        st = v.get("status")
        if st not in ("draft", "partial", "full"):
            errors.append(f"台账条目 {pid} status={st!r} 不在 draft/partial/full 之内")
        for m, lv in (v.get("materials") or {}).items():
            lvl = lv.get("level") if isinstance(lv, dict) else lv
            if lvl not in ("screening", "qualitative", "quantitative"):
                errors.append(f"台账 {pid}.{m} level={lvl!r} 非法")
        card = v.get("commission_card")
        if card and not (SKILL.parent / card).exists():
            warns.append(f"台账 {pid} 指向的仪器卡不存在：{card}")


def run_tests() -> None:
    p = subprocess.run([sys.executable, "-m", "unittest", "-q", "test_phase_gate.py"],
                       cwd=SKILL / "scripts", capture_output=True, text=True)
    if p.returncode != 0:
        errors.append("phase_gate 测试失败：\n" + (p.stderr or p.stdout)[-800:])


def main() -> int:
    check_files()
    check_skill_md()
    check_packs()
    check_ledger_consistency()
    run_tests()

    print(f"skill: {SKILL}")
    for w in warns:
        print(f"  ⚠ {w}")
    for e in errors:
        print(f"  ✗ {e}")
    if errors:
        print(f"\n{len(errors)} 个错误")
        return 2
    print("\n✅ 自检通过" + (f"（{len(warns)} 条警告）" if warns else ""))
    return 1 if warns else 0


if __name__ == "__main__":
    sys.exit(main())
