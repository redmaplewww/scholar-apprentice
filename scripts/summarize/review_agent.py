#!/usr/bin/env python3
"""审查 Agent：对每条提案做 A1–A5，并按风险决定去向。

它跑在独立上下文里，只看三样：提案、证据原文、当前正式结构快照。本文件实现
的是它的机械部分——A1 是确定性逐字比对，不依赖模型判断，所以编造无法蒙混。
"""
from __future__ import annotations

import json
import re

from . import textutil as tu
from .db import Store, now, rows
from .evidence import load_fulltext
from .queue import apply, get, pending, reject

HUMAN_KINDS = {"promote_axis", "split_axis", "merge_axis", "new_paradigm", "dispute"}


def _active_structures(store: Store) -> dict:
    with store.structure() as s:
        axes = [dict(r) for r in rows(s, "SELECT axis_id,name,definition,status FROM axes "
                                         "WHERE status IN ('candidate','stable','declining')")]
        clauses = [dict(r) for r in rows(s, "SELECT clause_id,kind,axis_id,text,normative,status "
                                            "FROM clauses WHERE status='active'")]
        edges = [dict(r) for r in rows(s, "SELECT src,dst,problem FROM edges WHERE status='active'")]
    return {"axes": axes, "clauses": clauses, "edges": edges}


def _a1_quote(store: Store, p: dict, payload: dict) -> tuple[str, str]:
    """返回 (状态, 说明)。状态 ∈ pass|fail|not_applicable|no_source。"""
    quote = (payload.get("quote") or "").strip()
    pid = payload.get("paper_id") or ""
    # 统计挖掘出来的条款没有单篇引文，它的"证据"是支持率与可复算的统计口径。
    # 这类不做逐字校验，但要确认条款确实存在于库里（防止凭空写一条）。
    if not quote and p["kind"] in ("add_clause", "revise_clause") and payload.get("clause_id"):
        with store.structure() as s:
            row = s.execute("SELECT 1 FROM clauses WHERE clause_id=?",
                            (payload["clause_id"],)).fetchone()
        if row:
            return "not_applicable", "统计条款：来源为实践/批评统计，支持率可复算"
        return "fail", f"条款 {payload['clause_id']} 不存在"
    if not quote:
        # 没有引文的提案（如晋升轴、改定义）不适用逐字校验
        if p["kind"] in {"promote_axis", "demote_axis", "redefine_axis", "split_axis",
                         "merge_axis", "new_paradigm", "dispute", "new_axis"}:
            return "not_applicable", "结构类提案无引文"
        return "fail", "缺少 quote，无法逐字校验"
    if not pid:
        return "fail", "缺少 paper_id"
    text = load_fulltext(store, pid)
    if not text:
        return "no_source", f"论文 {pid} 没有全文/摘要"
    if tu.quote_in(quote, text):
        return "pass", "引文逐字命中"
    return "fail", "引文未在原文中逐字命中（疑似改写或编造）"


def _a2_strength(p: dict, payload: dict) -> tuple[str, str, dict]:
    text = payload.get("text") or payload.get("statement") or ""
    if not text:
        return "pass", "", {}
    if not tu.has_strong_claim(text):
        return "pass", "", {}
    support = int(payload.get("sources") or payload.get("evidence_count") or 0)
    if support >= 3:
        return "pass", "强断言有 ≥3 来源支撑", {}
    new_text = tu.hedge(text)
    return "downgraded", "强断言但来源不足，已加限定语", {"text": new_text}


def _a3_consistency(store: Store, p: dict, payload: dict) -> tuple[str, str, dict | None]:
    st = _active_structures(store)
    text = payload.get("text") or ""
    if not text:
        return "pass", "", None
    vz = tu.Vectorizer().fit([text] + [c["text"] for c in st["clauses"]])
    v = vz.vec(text)
    for c in st["clauses"]:
        if payload.get("clause_id") and c["clause_id"] == payload.get("clause_id"):
            continue
        same_axis = (c["axis_id"] or "") == (payload.get("axis_id") or "")
        sc = tu.cosine(v, vz.vec(c["text"]))
        if same_axis and c["kind"] == payload.get("clause_kind", "baseline") and sc > 0.6:
            if _contradicts(text, c["text"]):
                return "conflict", f"与生效条款 {c['clause_id']} 冲突", {
                    "statement": text, "counter": c["text"], "axis_id": c["axis_id"]}
    return "pass", "", None


def _contradicts(a: str, b: str) -> bool:
    """粗粒度否定冲突：一方含禁止语，另一方断言应当。"""
    neg = ("不得", "禁止", "不应", "must not", "should not")
    return any(x in a for x in neg) != any(x in b for x in neg)


def _a4_necessity(store: Store, p: dict, payload: dict, others: list[dict]) -> tuple[str, str]:
    text = payload.get("text") or ""
    if not text:
        return "pass", ""
    docs = [text] + [json.loads(o["payload"] or "{}").get("text", "") for o in others
                     if o["proposal_id"] != p["proposal_id"]]
    vz = tu.Vectorizer().fit([d for d in docs if d])
    v = vz.vec(text)
    for o in others:
        if o["proposal_id"] == p["proposal_id"]:
            continue
        ot = json.loads(o["payload"] or "{}").get("text", "")
        if ot and tu.cosine(v, vz.vec(ot)) > 0.9:
            return "duplicate", f"与待审提案 {o['proposal_id']} 重复"
    return "pass", ""


def _a5_risk(p: dict, payload: dict) -> tuple[str, str]:
    if p["kind"] in HUMAN_KINDS:
        return "human", f"{p['kind']} 属于判断题"
    if p["kind"] in ("add_clause",) and payload.get("normative"):
        return "human", "规范性条款（红线）需人审"
    return "agent", "常规结构项"


def review_one(store: Store, p: dict, others: list[dict]) -> dict:
    payload = json.loads(p["payload"] or "{}")
    checks = {}

    a1, a1_note = _a1_quote(store, p, payload)
    checks["A1"] = {"result": a1, "note": a1_note}

    a2, a2_note, a2_fix = _a2_strength(p, payload)
    checks["A2"] = {"result": a2, "note": a2_note}
    if a2_fix:
        payload.update(a2_fix)

    a3, a3_note, dispute = _a3_consistency(store, p, payload)
    checks["A3"] = {"result": a3, "note": a3_note}

    a4, a4_note = _a4_necessity(store, p, payload, others)
    checks["A4"] = {"result": a4, "note": a4_note}

    a5, a5_note = _a5_risk(p, payload)
    checks["A5"] = {"result": a5, "note": a5_note}

    verdict, reason = "agent", ""
    if a1 == "fail" or a4 == "duplicate":
        verdict = "reject"
        reason = a1_note if a1 == "fail" else a4_note
    elif a1 == "no_source":
        verdict = "needs_source"
        reason = a1_note
    elif a3 == "conflict":
        verdict = "dispute"
        reason = a3_note
    elif a5 == "human":
        verdict = "escalate"
        reason = a5_note
    else:
        verdict = "auto"
        reason = "五项检查通过" + ("；A2 已降级措辞" if a2 == "downgraded" else "")

    with store.structure() as s:
        s.execute("UPDATE proposals SET agent_checks=?,payload=? WHERE proposal_id=?",
                  (json.dumps(checks, ensure_ascii=False), json.dumps(payload, ensure_ascii=False),
                   p["proposal_id"]))
        s.execute("INSERT INTO review_log(proposal_id,checks,verdict,reason,at) VALUES(?,?,?,?,?)",
                  (p["proposal_id"], json.dumps(checks, ensure_ascii=False), verdict, reason, now()))

    out = {"proposal_id": p["proposal_id"], "kind": p["kind"], "checks": checks,
           "verdict": verdict, "reason": reason}

    if verdict == "reject":
        reject(store, p["proposal_id"], reason, by="agent")
    elif verdict == "auto":
        r = apply(store, p["proposal_id"], by="agent", status="agent_approved")
        out["applied"] = r
    elif verdict == "escalate":
        with store.structure() as s:
            s.execute("UPDATE proposals SET status='escalated',reason=?,risk='human' WHERE proposal_id=?",
                      (reason, p["proposal_id"]))
    elif verdict == "needs_source":
        with store.structure() as s:
            s.execute("UPDATE proposals SET status='needs_source',reason=? WHERE proposal_id=?",
                      (reason, p["proposal_id"]))
    elif verdict == "dispute":
        from .queue import add_proposal
        dpid = add_proposal(store, "dispute", {**payload, **dispute},
                            evidence=p["evidence"], impact=p["impact"], risk="human")
        with store.structure() as s:
            s.execute("UPDATE proposals SET status='rejected',reason=?,decided_at=?,decided_by='agent' "
                      "WHERE proposal_id=?", (f"转为争议单 {dpid}", now(), p["proposal_id"]))
        out["dispute_proposal"] = dpid
    return out


def review_pending(store: Store, limit: int = 50) -> dict:
    items = [p for p in pending(store, ("draft",))][:limit]
    results = []
    for p in items:
        others = pending(store)
        results.append(review_one(store, p, others))
    summary: dict[str, int] = {}
    for r in results:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    return {"reviewed": len(results), "summary": summary, "results": results}


def quality(store: Store) -> dict:
    with store.structure() as s:
        total = s.execute("SELECT COUNT(*) FROM review_log").fetchone()[0]
        if not total:
            return {"reviewed": 0}
        checks = [json.loads(r["checks"]) for r in rows(s, "SELECT checks FROM review_log")]
        a1_pass = sum(1 for c in checks if c.get("A1", {}).get("result") in ("pass", "not_applicable"))
        verdicts = {r["verdict"]: r["n"] for r in rows(
            s, "SELECT verdict,COUNT(*) n FROM review_log GROUP BY verdict")}
    return {
        "reviewed": total,
        "A1_pass_rate": round(a1_pass / total, 3),
        "verdicts": verdicts,
        "escalation_rate": round(verdicts.get("escalate", 0) / total, 3),
        "reject_rate": round(verdicts.get("reject", 0) / total, 3),
    }
