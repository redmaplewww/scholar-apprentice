#!/usr/bin/env python3
"""提案队列：草稿 → 审查 → 正式，附变更日志与回滚。

正式结构的每一次变化都经过这里，所以任何一次结构改动都能撤销。
"""
from __future__ import annotations

import json
import uuid

from .db import Store, now, rows

KINDS = {
    "new_axis", "redefine_axis", "split_axis", "merge_axis", "promote_axis", "demote_axis",
    "add_edge", "add_clause", "revise_clause", "retire_clause", "new_tech", "new_paradigm",
    "dispute",
}


def add_proposal(store: Store, kind: str, payload: dict, evidence: str = "",
                 impact: str = "{}", risk: str = "agent") -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown proposal kind {kind}; allowed {sorted(KINDS)}")
    pid = "pr-" + uuid.uuid4().hex[:10]
    with store.structure() as s:
        s.execute("""INSERT INTO proposals(proposal_id,kind,payload,evidence,impact,risk,status,
                     agent_checks,reason,created_at) VALUES(?,?,?,?,?,?,'draft','','',?)""",
                  (pid, kind, json.dumps(payload, ensure_ascii=False), evidence, impact, risk, now()))
    return pid


def get(store: Store, proposal_id: str) -> dict | None:
    with store.structure() as s:
        r = s.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
    return dict(r) if r else None


def pending(store: Store, statuses=("draft", "escalated", "needs_source")) -> list[dict]:
    qs = ",".join("?" * len(statuses))
    with store.structure() as s:
        return [dict(r) for r in rows(
            s, f"SELECT * FROM proposals WHERE status IN ({qs}) ORDER BY created_at", statuses)]


def auto_committed(store: Store, limit: int = 50) -> list[dict]:
    with store.structure() as s:
        return [dict(r) for r in rows(
            s, "SELECT * FROM proposals WHERE status='agent_approved' ORDER BY decided_at DESC LIMIT ?",
            (limit,))]


# ------------------------------------------------------------------ apply

def apply(store: Store, proposal_id: str, by: str = "agent", status: str = "agent_approved") -> dict:
    p = get(store, proposal_id)
    if not p:
        return {"ok": False, "reason": "no such proposal"}
    if p["status"] in ("rejected", "reverted"):
        return {"ok": False, "reason": f"proposal is {p['status']}"}
    kind, payload = p["kind"], json.loads(p["payload"] or "{}")
    result = _apply_one(store, proposal_id, kind, payload)
    with store.structure() as s:
        s.execute("UPDATE proposals SET status=?,decided_at=?,decided_by=? WHERE proposal_id=?",
                  (status, now(), by, proposal_id))
    return {"ok": True, "applied": kind, "result": result, "status": status}


def _apply_one(store: Store, proposal_id: str, kind: str, payload: dict) -> dict:
    if kind == "add_clause":
        cid = payload.get("clause_id") or ("cl-" + uuid.uuid4().hex[:8])
        with store.structure() as s:
            before = s.execute("SELECT * FROM clauses WHERE clause_id=?", (cid,)).fetchone()
            s.execute("""INSERT INTO clauses(clause_id,kind,axis_id,text,status,normative,support,
                          counter,evidence,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,'',?,?,?)
                         ON CONFLICT(clause_id) DO UPDATE SET text=excluded.text,status=excluded.status,
                           updated_at=excluded.updated_at""",
                      (cid, payload.get("clause_kind") or payload.get("kind_of_clause") or "baseline",
                       payload.get("axis_id", ""), payload["text"], "active",
                       1 if payload.get("normative") else 0, payload.get("support", 0.0),
                       payload.get("evidence", ""), now(), now()))
        store.log_change("add_clause", cid, dict(before) if before else "", payload, proposal_id)
        return {"clause_id": cid}

    if kind == "retire_clause":
        cid = payload["clause_id"]
        with store.structure() as s:
            before = s.execute("SELECT status FROM clauses WHERE clause_id=?", (cid,)).fetchone()
            s.execute("UPDATE clauses SET status='retired',updated_at=? WHERE clause_id=?", (now(), cid))
        store.log_change("retire_clause", cid, before["status"] if before else "", "retired", proposal_id)
        return {"clause_id": cid}

    if kind == "revise_clause":
        cid = payload["clause_id"]
        with store.structure() as s:
            before = s.execute("SELECT text,support FROM clauses WHERE clause_id=?", (cid,)).fetchone()
            s.execute("UPDATE clauses SET text=?,support=?,updated_at=? WHERE clause_id=?",
                      (payload["text"], payload.get("support", 0.0), now(), cid))
        store.log_change("revise_clause", cid, dict(before) if before else "", payload, proposal_id)
        return {"clause_id": cid}

    if kind == "add_edge":
        with store.structure() as s:
            cur = s.execute("""INSERT INTO edges(src,dst,rel,problem,evidence,axis_id,status,created_at)
                               VALUES(?,?,?,?,?,?,'active',?)""",
                            (payload["src"], payload["dst"], payload.get("rel", "resolve"),
                             payload.get("problem", ""), payload.get("evidence", ""),
                             payload.get("axis_id", ""), now()))
            eid = cur.lastrowid
        store.log_change("add_edge", str(eid), "", payload, proposal_id)
        return {"edge_id": eid}

    if kind in ("promote_axis", "demote_axis", "redefine_axis"):
        aid = payload["axis_id"]
        with store.structure() as s:
            before = s.execute("SELECT * FROM axes WHERE axis_id=?", (aid,)).fetchone()
            if not before:
                return {"error": "no such axis"}
            if kind == "promote_axis":
                s.execute("UPDATE axes SET status='stable',last_change=? WHERE axis_id=?", (now(), aid))
            elif kind == "demote_axis":
                s.execute("UPDATE axes SET status='declining',last_change=? WHERE axis_id=?", (now(), aid))
            else:
                s.execute("""UPDATE axes SET name=?,definition=?,last_change=?,
                             pinned=CASE WHEN ?=1 THEN 1 ELSE pinned END WHERE axis_id=?""",
                          (payload.get("name", before["name"]),
                           payload.get("definition", before["definition"]), now(),
                           1 if payload.get("user_authored") else 0, aid))
        store.log_change(kind, aid, dict(before), payload, proposal_id)
        return {"axis_id": aid}

    if kind == "split_axis":
        parent = payload["axis_id"]
        children = payload.get("children") or []
        with store.structure() as s:
            before = s.execute("SELECT * FROM axes WHERE axis_id=?", (parent,)).fetchone()
            if not before:
                return {"error": "no such axis"}
            s.execute("UPDATE axes SET status='deprecated',last_change=? WHERE axis_id=?", (now(), parent))
            n = s.execute("SELECT COUNT(*) FROM axes").fetchone()[0]
            made = []
            for i, child in enumerate(children):
                cid = f"{parent}{chr(97 + i)}"
                s.execute("""INSERT OR REPLACE INTO axes(axis_id,name,definition,status,parent_id,lineage,
                              evidence_count,group_count,first_seen,last_change)
                             VALUES(?,?,?,'candidate',?,?,0,0,?,?)""",
                          (cid, child.get("name", f"{parent} 子轴{i+1}"),
                           child.get("definition", ""), parent, f"{parent} -> {cid}", now(), now()))
                made.append(cid)
        store.log_change("split_axis", parent, dict(before), {"children": made}, proposal_id)
        return {"parent": parent, "children": made}

    if kind == "merge_axis":
        into, absorbed = payload["into"], payload["absorbed"]
        with store.structure() as s:
            b1 = s.execute("SELECT * FROM axes WHERE axis_id=?", (into,)).fetchone()
            b2 = s.execute("SELECT * FROM axes WHERE axis_id=?", (absorbed,)).fetchone()
            s.execute("UPDATE axes SET status='stable',lineage=coalesce(lineage,'')||?,last_change=? WHERE axis_id=?",
                      (f" ← {absorbed}", now(), into))
            s.execute("UPDATE axes SET status='deprecated',last_change=? WHERE axis_id=?", (now(), absorbed))
            s.execute("UPDATE contributions SET axis_id=? WHERE axis_id=?", (into, absorbed))
            s.execute("INSERT OR REPLACE INTO axis_members(axis_id,paper_id,target_key,weight) "
                      "SELECT ?,paper_id,target_key,weight FROM axis_members WHERE axis_id=?",
                      (into, absorbed))
        store.log_change("merge_axis", f"{absorbed}->{into}",
                         {"into": dict(b1) if b1 else None, "absorbed": dict(b2) if b2 else None},
                         {"result": into}, proposal_id)
        return {"into": into, "absorbed": absorbed}

    if kind == "new_axis":
        aid = payload.get("axis_id") or ("axis-" + uuid.uuid4().hex[:6])
        with store.structure() as s:
            s.execute("""INSERT OR IGNORE INTO axes(axis_id,name,definition,status,evidence_count,
                          group_count,first_seen,last_change) VALUES(?,?,?,'candidate',?,0,?,?)""",
                      (aid, payload.get("name", "待命名"), payload.get("definition", ""),
                       payload.get("evidence_count", 0), now(), now()))
        store.log_change("new_axis", aid, "", payload, proposal_id)
        return {"axis_id": aid}

    if kind == "new_paradigm":
        aid = payload.get("axis_id") or ("para-" + uuid.uuid4().hex[:6])
        with store.structure() as s:
            s.execute("""INSERT OR IGNORE INTO axes(axis_id,name,definition,status,evidence_count,
                          group_count,first_seen,last_change) VALUES(?,?,?,'candidate',?,0,?,?)""",
                      (aid, "范式：" + payload.get("name", "待命名"),
                       payload.get("definition", ""), payload.get("evidence_count", 0), now(), now()))
        store.log_change("new_paradigm", aid, "", payload, proposal_id)
        return {"axis_id": aid}

    if kind == "new_tech":
        # 技术本身记在贡献记录的 technique 字段；这里只记一次结构事实
        store.log_change("new_tech", payload.get("technique", ""), "", payload, proposal_id)
        return {"technique": payload.get("technique", "")}

    if kind == "dispute":
        with store.structure() as s:
            s.execute("INSERT INTO claims(statement,status,support,counter,axis_id,created_at) "
                      "VALUES(?, 'contested', ?, ?, ?, ?)",
                      (payload.get("statement", ""), payload.get("support", ""),
                       payload.get("counter", ""), payload.get("axis_id", ""), now()))
        store.log_change("dispute", payload.get("statement", ""), "", payload, proposal_id)
        return {"ok": True}

    return {"error": f"unhandled kind {kind}"}


# ----------------------------------------------------------------- revert

def revert(store: Store, proposal_id: str) -> dict:
    p = get(store, proposal_id)
    if not p:
        return {"ok": False, "reason": "no such proposal"}
    if p["status"] not in ("agent_approved", "human_approved"):
        return {"ok": False, "reason": f"only approved proposals can be reverted (is {p['status']})"}
    kind, payload = p["kind"], json.loads(p["payload"] or "{}")
    undone = _revert_one(store, kind, payload, p["evidence"])
    with store.structure() as s:
        s.execute("UPDATE proposals SET status='reverted',decided_at=?,decided_by='revert' WHERE proposal_id=?",
                  (now(), proposal_id))
    store.log_change("revert", proposal_id, kind, "", proposal_id)
    return {"ok": True, "reverted": kind, "undo": undone}


def _revert_one(store: Store, kind: str, payload: dict, evidence: str) -> dict:
    if kind == "add_clause":
        cid = payload.get("clause_id") or ""
        with store.structure() as s:
            if not cid:
                r = s.execute("SELECT clause_id FROM clauses WHERE text=? ORDER BY rowid DESC LIMIT 1",
                              (payload.get("text", ""),)).fetchone()
                cid = r["clause_id"] if r else ""
            s.execute("DELETE FROM clauses WHERE clause_id=?", (cid,))
        return {"removed_clause": cid}
    if kind == "add_edge":
        with store.structure() as s:
            s.execute("DELETE FROM edges WHERE src=? AND dst=? AND coalesce(problem,'')=?",
                      (payload.get("src", ""), payload.get("dst", ""), payload.get("problem", "")))
        return {"removed_edge": [payload.get("src"), payload.get("dst")]}
    if kind in ("promote_axis", "demote_axis"):
        aid = payload.get("axis_id", "")
        with store.structure() as s:
            s.execute("UPDATE axes SET status='candidate',last_change=? WHERE axis_id=?", (now(), aid))
        return {"axis": aid, "status": "candidate"}
    if kind == "redefine_axis":
        aid = payload.get("axis_id", "")
        with store.structure() as s:
            s.execute("UPDATE axes SET pinned=0,last_change=? WHERE axis_id=?", (now(), aid))
        return {"axis": aid, "pinned": 0}
    if kind == "split_axis":
        parent = payload.get("axis_id", "")
        with store.structure() as s:
            s.execute("UPDATE axes SET status='stable',last_change=? WHERE axis_id=?", (now(), parent))
            s.execute("UPDATE axes SET status='deprecated' WHERE parent_id=?", (parent,))
        return {"parent": parent, "restored": True}
    if kind == "merge_axis":
        absorbed = payload.get("absorbed", "")
        with store.structure() as s:
            s.execute("UPDATE axes SET status='candidate',last_change=? WHERE axis_id=?", (now(), absorbed))
        return {"restored": absorbed}
    if kind == "dispute":
        with store.structure() as s:
            s.execute("DELETE FROM claims WHERE statement=? AND status='contested'",
                      (payload.get("statement", ""),))
        return {"removed_claim": payload.get("statement", "")}
    return {"note": f"no undo for {kind}"}


def reject(store: Store, proposal_id: str, reason: str = "", by: str = "agent") -> dict:
    with store.structure() as s:
        s.execute("UPDATE proposals SET status='rejected',reason=?,decided_at=?,decided_by=? WHERE proposal_id=?",
                  (reason, now(), by, proposal_id))
    return {"ok": True, "rejected": proposal_id}


def queue_stats(store: Store) -> dict:
    with store.structure() as s:
        by_status = {r["status"]: r["n"] for r in rows(
            s, "SELECT status, COUNT(*) n FROM proposals GROUP BY status")}
        decided = s.execute("SELECT COUNT(*) FROM proposals WHERE status IN "
                            "('agent_approved','human_approved')").fetchone()[0]
        escalated = by_status.get("escalated", 0)
        total = sum(by_status.values()) or 1
    return {"by_status": by_status, "approved": decided,
            "escalation_rate": round(escalated / total, 3)}
