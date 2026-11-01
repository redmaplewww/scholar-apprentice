#!/usr/bin/env python3
"""复盘与沉淀门。

每个工作单元结束后，Agent 写一份复盘（候选 0–3 条），本模块只做机械判定：
G1 新（语义去重）、G2 据（指针可解析）、G3 影响（声明会影响什么判断）、
G4 稳（来源数或标单例）。没过的进未采纳日志，不进队列。

用法（Agent 侧）：
  scholar.py reflect --unit paper:doi:xxx --file reflection.json
  → 返回每条候选的过门结果与 proposal_id
"""
from __future__ import annotations

import json
from pathlib import Path

from . import textutil as tu
from .db import Store, now, rows
from .evidence import load_fulltext
from .queue import add_proposal

AFFECTS = {"redteam", "control_group", "assignment", "teaching", "clause", "route", "hole"}
UNIT_KINDS = ("paper", "session", "explore", "experiment", "redteam", "dedup", "manual")


def _existing_texts(store: Store) -> list[tuple[str, str]]:
    """已有结构元素的文本，用于 G1 去重。"""
    out = []
    with store.structure() as s:
        for r in rows(s, "SELECT axis_id, name, definition FROM axes"):
            out.append((r["axis_id"], f"{r['name']} {r['definition'] or ''}"))
        for r in rows(s, "SELECT clause_id, text FROM clauses"):
            out.append((r["clause_id"], r["text"]))
        for r in rows(s, "SELECT edge_id, src, dst, coalesce(problem,'') p FROM edges"):
            out.append((f"edge-{r['edge_id']}", f"{r['src']} {r['dst']} {r['p']}"))
    return out


def _resolve_pointer(store: Store, paper_id: str, evidence: str, quote: str) -> tuple[bool, str]:
    """G2：指针必须能落到这篇论文上；有引文就同时做一次逐字检查。"""
    with store.papers() as p:
        row = p.execute("SELECT meta_level FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
    if not row:
        return False, "unknown paper"
    if quote:
        text = load_fulltext(store, paper_id)
        if not text:
            return False, "no fulltext/abstract to verify quote"
        if not tu.quote_in(quote, text):
            return False, "quote not found in source"
    if not evidence.strip():
        return False, "empty evidence pointer"
    return True, "ok"


def reflect(store: Store, unit: str, payload: dict) -> dict:
    """payload = {"candidates":[{kind,axis_id,text,evidence,quote,paper_id,affects,sources,payload}],
                  "zero_reason": "..."}"""
    if ":" not in unit:
        unit = "manual:" + unit
    kind = unit.split(":", 1)[0]
    if kind not in UNIT_KINDS:
        return {"ok": False, "reason": f"unknown unit kind {kind}; use {UNIT_KINDS}"}

    candidates = payload.get("candidates") or []
    if not candidates:
        with store.structure() as s:
            s.execute("INSERT INTO reflections(unit,candidates,zero_reason,payload,created_at) VALUES(?,?,?,?,?)",
                      (unit, 0, payload.get("zero_reason", "无新内容"), "{}", now()))
        return {"ok": True, "candidates": 0, "note": "零沉淀已记录"}

    existing = _existing_texts(store)
    vz = tu.Vectorizer().fit([t for _, t in existing] + [c.get("text", "") for c in candidates])

    results, accepted = [], 0
    for c in candidates:
        gates = {}
        reasons = []

        # G2
        ok2, why2 = _resolve_pointer(store, c.get("paper_id", ""), c.get("evidence", ""),
                                     c.get("quote", ""))
        gates["G2"] = ok2
        if not ok2:
            reasons.append("G2:" + why2)

        # G1 新
        cv = vz.vec(c.get("text", ""))
        best, score = None, 0.0
        for key, t in existing:
            sc = tu.cosine(cv, vz.vec(t))
            if sc > score:
                best, score = key, sc
        gates["G1"] = score < 0.82
        if not gates["G1"]:
            reasons.append(f"G1:与 {best} 重复 (cos={score:.2f})")

        # G3 影响
        affects = c.get("affects") or ""
        gates["G3"] = affects in AFFECTS
        if not gates["G3"]:
            reasons.append("G3:未声明影响面")

        # G4 稳
        sources = int(c.get("sources") or 0)
        single = bool(c.get("single_observation"))
        gates["G4"] = sources >= 2 or single
        if not gates["G4"]:
            reasons.append("G4:单一来源且未标单例")

        passed = all(gates.values())
        rec = {"kind": c.get("kind", "add_clause"), "gates": gates, "text": c.get("text", "")}
        if passed:
            pid = add_proposal(
                store,
                kind=c.get("kind", "add_clause"),
                payload={"text": c.get("text", ""), "axis_id": c.get("axis_id", ""),
                         "paper_id": c.get("paper_id", ""), "evidence": c.get("evidence", ""),
                         "quote": c.get("quote", ""), "affects": affects,
                         "single_observation": single, **c.get("payload", {})},
                evidence=c.get("evidence", ""),
                impact=json.dumps({"affects": affects, "unit": unit}, ensure_ascii=False),
                risk="agent",
            )
            rec["proposal_id"] = pid
            accepted += 1
        else:
            with store.structure() as s:
                s.execute("""INSERT INTO unadopted(unit,payload,failed_gate,reason,at)
                             VALUES(?,?,?,?,?)""",
                          (unit, json.dumps(c, ensure_ascii=False),
                           ",".join(k for k, v in gates.items() if not v), "；".join(reasons), now()))
        results.append(rec)

    with store.structure() as s:
        s.execute("INSERT INTO reflections(unit,candidates,zero_reason,payload,created_at) VALUES(?,?,?,?,?)",
                  (unit, len(candidates), "", json.dumps(results, ensure_ascii=False), now()))
    return {"ok": True, "candidates": len(candidates), "accepted": accepted, "results": results}


def zero_stats(store: Store) -> dict:
    with store.structure() as s:
        total = s.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        zero = s.execute("SELECT COUNT(*) FROM reflections WHERE candidates=0").fetchone()[0]
        withc = s.execute("SELECT COUNT(*) FROM reflections WHERE candidates>0").fetchone()[0]
        avg = s.execute("SELECT AVG(candidates) FROM reflections").fetchone()[0] or 0
        blocked = s.execute("SELECT COUNT(*) FROM unadopted").fetchone()[0]
    return {"units": total, "zero": zero, "with_candidates": withc,
            "zero_ratio": round(zero / total, 3) if total else 0.0,
            "avg_candidates": round(avg, 2), "blocked_by_gates": blocked}


def template(unit: str) -> dict:
    """给 Agent 的复盘模板，避免它自由发挥。"""
    return {
        "unit": unit,
        "instruction": "只写这一步工作里真正新增的东西；没有就返回空 candidates 并写 zero_reason。"
                       "kind ∈ add_edge|add_clause|revise_clause|retire_clause|new_tech|new_axis|dispute；"
                       "affects ∈ redteam|control_group|assignment|teaching|clause|route|hole；"
                       "quote 必须是原文片段，用于 A1 逐字校验。",
        "candidates": [
            {
                "kind": "add_clause",
                "text": "",
                "axis_id": "",
                "paper_id": "",
                "evidence": "p.? §?",
                "quote": "",
                "affects": "control_group",
                "sources": 1,
                "single_observation": True,
                "payload": {},
            }
        ],
    }
