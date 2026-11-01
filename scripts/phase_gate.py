#!/usr/bin/env python3
"""phase_gate.py —— 学术学徒的阶段门禁（机械执行体，stdlib 单文件）

它不替你做判断，只把"该不该进 I 阶段""这个数字能不能用"变成 exit code。

用法：
  python phase_gate.py should-I --self-generated --new-pipeline --not-covered \
                               --external --conventions --multi-setting
      逐条给出命中判据，>=2 条 exit 0（必须进 I 阶段），<2 条 exit 1

  python phase_gate.py status [--ws <工作区>]
      列出仪器台账里每台仪器的状态与覆盖范围

  python phase_gate.py check --ws <工作区> --instrument <id> --material <体系> [--claim quantitative]
      该体系+该量级是否被允许 → exit 0 允许 / exit 1 需降级 / exit 2 仪器不存在或未落成

  python phase_gate.py birth-cert --ws <工作区> --instrument <id> --version <v> \
                               --status <s> --scope <覆盖范围> --in-scope yes
      五个问题答全 → exit 0；缺项 → exit 1 并列出缺什么

退出码约定（与 da_toolbox 的 gate 一致）：
  0 放行 / 1 降档 / 2 阻塞
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LEVELS = ["forbidden", "screening", "qualitative", "quantitative"]
CRITERIA = {
    "self-generated": "A1 结论依赖 AI 自己新产生的数值",
    "new-pipeline": "A2 需要新建或改造计算/测量/分析管道",
    "not-covered": "A3 该管道没有验收记录，或覆盖不到当前体系/版本",
    "external": "A4 数值要用于对外产出（论文/立项/给用户的判断/SOP）",
    "conventions": "A5 存在坐标/符号/单位/编码约定",
    "multi-setting": "A6 结果依赖多个可调设置",
}


def ledger(ws: Path) -> dict:
    p = ws / "研究档案" / "instruments.json"
    if not p.exists():
        return {"instruments": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_should_i(a) -> int:
    hits = [k for k in CRITERIA if getattr(a, k.replace("-", "_"), False)]
    print("I 阶段启动判据：")
    for k, desc in CRITERIA.items():
        print(f"  [{'x' if k in hits else ' '}] {desc}")
    if len(hits) >= 2:
        print(f"\n命中 {len(hits)} 条 → 必须进入 I 阶段。")
        print("禁止在此之前开跑研究性算例；先按 references/stage-I-instrument-commissioning.md 走 I0–I3。")
        return 0
    print(f"\n命中 {len(hits)} 条 → 不强制。若仍要使用自产数值，按'数字出生证'标注来源。")
    return 1


def cmd_status(a) -> int:
    ws = Path(a.ws).resolve()
    data = ledger(ws)
    ins = data.get("instruments", {})
    if not ins:
        p = ws / "研究档案" / "instruments.json"
        if p.exists():
            print(f"台账是空的：{p}")
        else:
            print(f"台账不存在：{p}")
        print("→ 若本任务要产生自产数值，按 I 阶段冷启动（references/stage-I-instrument-commissioning.md）。")
        return 2
    print(f"{'instrument_id':32s} {'status':10s} coverage")
    for k, v in ins.items():
        cov = ", ".join(f"{m}:{d}" for m, d in v.get("materials", {}).items())
        print(f"{k:32s} {v.get('status', '?'):10s} {cov}")
        for f in v.get("forbidden_claims", []):
            print(f"{'':44s} ⛔ {f}")
    return 0


def cmd_check(a) -> int:
    ws = Path(a.ws).resolve()
    ins = ledger(ws).get("instruments", {})
    v = ins.get(a.instrument)
    if not v:
        print(f"exit2 仪器 {a.instrument} 不在台账里 → 未落成，任何数值只能标〔探索性·未验收〕")
        return 2
    if v.get("status") == "draft":
        print(f"exit2 仪器 {a.instrument} 仍是 draft（未落成）→ 任何数值只能标〔探索性·未验收〕")
        return 2
    mat = v.get("materials", {}).get(a.material)
    if mat is None:
        print(f"exit2 体系 {a.material} 不在覆盖范围内 → 该数值只能标〔探索性·未验收〕")
        return 2
    level = mat.get("level", "screening") if isinstance(mat, dict) else str(mat)
    need = LEVELS.index(a.claim) if a.claim in LEVELS else LEVELS.index("quantitative")
    have = LEVELS.index(level) if level in LEVELS else 0
    if have >= need:
        print(f"exit0 {a.instrument} 对 {a.material} 的 {a.claim} 结论：允许")
        return 0
    print(f"exit1 {a.instrument} 对 {a.material} 最高只到 {level}，不支持 {a.claim}")
    if isinstance(mat, dict) and mat.get("note"):
        print(f"      原因：{mat['note']}")
    return 1


def cmd_birth_cert(a) -> int:
    need = {"instrument": a.instrument, "version": a.version, "status": a.status,
            "scope": a.scope, "in_scope": a.in_scope}
    missing = [k for k, v in need.items() if v in (None, "", "none")]
    if missing:
        print(f"exit1 数字出生证缺项：{', '.join(missing)} → 该数值只能标〔探索性·未验收〕")
        return 1
    if a.in_scope not in ("yes", "true", "1"):
        print("exit1 数值不在仪器覆盖范围内 → 只能标〔探索性·未验收〕")
        return 1
    print("exit0 出生证齐备，且数值在覆盖范围内")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="学术学徒阶段门禁")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("should-I", help="I 阶段是否强制启动")
    for k in CRITERIA:
        s.add_argument("--" + k, action="store_true")
    s.set_defaults(fn=cmd_should_i)

    s = sub.add_parser("status", help="列出仪器台账")
    s.add_argument("--ws", default=".")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("check", help="某数值能否被用作某级结论")
    s.add_argument("--ws", default=".")
    s.add_argument("--instrument", required=True)
    s.add_argument("--material", required=True)
    s.add_argument("--claim", default="quantitative", choices=LEVELS[1:])
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("birth-cert", help="校验数字出生证五问")
    s.add_argument("--ws", default=".")
    # 缺项不交给 argparse 报用法错误，交给门禁自己报"缺什么"（exit 1 而不是 2）
    s.add_argument("--instrument", default=None)
    s.add_argument("--version", default=None)
    s.add_argument("--status", default=None)
    s.add_argument("--scope", default=None)
    s.add_argument("--in-scope", default=None)
    s.set_defaults(fn=cmd_birth_cert)

    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
