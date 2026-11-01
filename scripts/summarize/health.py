#!/usr/bin/env python3
"""体检：把结构健康状况算成信号，信号生成提案或缺口单。

信号阈值集中在本文件顶部，回放（replay.py）调阈值时只改这里。
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from . import textutil as tu
from .db import Store, js, now, rows
from .queue import add_proposal

# ---- 阈值（初始值，回放后调整） ----
UNASSIGNED_RATE = 0.15
AXIS_MIN_PAPERS = 3
AXIS_MIN_GROUPS = 2
SPLIT_CLUSTER_GAP = 0.18
MERGE_OVERLAP = 0.60
COUPLE_MIN = 0.30
THIN_GROUP = 2
DORMANT_YEARS = 3
BASE_DRIFT = 0.30


def _unassigned(store: Store) -> tuple[int, int]:
    with store.papers() as p:
        total = p.execute("SELECT COUNT(*) FROM contributions").fetchone()[0]
        un = p.execute("SELECT COUNT(*) FROM contributions WHERE coalesce(axis_id,'')=''").fetchone()[0]
    return un, total


def _axis_papers(store: Store) -> dict[str, set]:
    with store.papers() as p:
        out: dict[str, set] = defaultdict(set)
        for r in rows(p, "SELECT axis_id,paper_id FROM contributions WHERE coalesce(axis_id,'')!=''"):
            out[r["axis_id"]].add(r["paper_id"])
        return out


def signals(store: Store) -> dict:
    un, total = _unassigned(store)
    with store.structure() as s:
        axes = [dict(r) for r in rows(s, "SELECT * FROM axes WHERE status!='deprecated'")]
        clauses = [dict(r) for r in rows(s, "SELECT * FROM clauses")]
        holes = [dict(r) for r in rows(s, "SELECT * FROM holes WHERE status='open'")]
    papers_by_axis = _axis_papers(store)
    sig = {"unassigned_rate": round(un / total, 3) if total else 0.0,
           "unassigned": un, "total_contribs": total, "items": []}

    # 1 未归类堆积
    if total and un / total > UNASSIGNED_RATE:
        sig["items"].append({"signal": "unassigned_pileup", "value": round(un / total, 3),
                             "action": "cluster_and_propose_axis"})

    # 2/6 轴成熟 / 证据单薄
    for a in axes:
        pid = papers_by_axis.get(a["axis_id"], set())
        if a["status"] == "candidate" and len(pid) >= AXIS_MIN_PAPERS and (a["group_count"] or 0) >= AXIS_MIN_GROUPS:
            sig["items"].append({"signal": "axis_mature", "axis_id": a["axis_id"],
                                 "papers": len(pid), "groups": a["group_count"],
                                 "action": "propose_promote"})
        if a["status"] in ("candidate", "stable") and 0 < len(pid) and (a["group_count"] or 0) < THIN_GROUP:
            sig["items"].append({"signal": "thin_evidence", "axis_id": a["axis_id"],
                                 "groups": a["group_count"], "action": "open_hole"})

    # 3 轴内分化 & 4 轴间重叠
    with store.papers() as p:
        contribs = rows(p, "SELECT target_text,axis_id FROM contributions WHERE coalesce(axis_id,'')!=''")
    by_axis: dict[str, list[str]] = defaultdict(list)
    for c in contribs:
        by_axis[c["axis_id"]].append(c["target_text"])
    vz = tu.Vectorizer().fit([c["target_text"] for c in contribs] or [""])
    for aid, texts in by_axis.items():
        if len(texts) < 6:
            continue
        vecs = [vz.vec(t) for t in texts]
        split = _two_means(vecs)
        if not split:
            continue
        c1, c2, g1, g2 = split
        inter = tu.cosine(c1, c2)
        intra1 = sum(tu.cosine(v, c1) for v in g1) / len(g1)
        intra2 = sum(tu.cosine(v, c2) for v in g2) / len(g2)
        # 只有"簇内紧、簇间松"才算真分化，否则只是同一个轴里话题散
        if (len(g1) >= 3 and len(g2) >= 3 and inter < (1 - SPLIT_CLUSTER_GAP)
                and min(intra1, intra2) > inter + 0.10):
            sig["items"].append({"signal": "axis_split", "axis_id": aid,
                                 "inter": round(inter, 3), "intra": [round(intra1, 3), round(intra2, 3)],
                                 "sizes": [len(g1), len(g2)], "action": "propose_split"})

    with store.structure() as s:
        for r in rows(s, "SELECT a,b,ratio FROM couplings"):
            if r["ratio"] >= MERGE_OVERLAP:
                sig["items"].append({"signal": "axis_overlap", "a": r["a"], "b": r["b"],
                                     "ratio": r["ratio"], "action": "propose_merge"})

    # 5 冷热
    with store.papers() as p:
        years = {r["paper_id"]: r["year"] for r in rows(p, "SELECT paper_id,year FROM papers")}
    cur = max(years.values() or [0])
    for aid, pids in papers_by_axis.items():
        ys = [years.get(x, 0) for x in pids]
        if cur and ys and max(ys) and cur - max(ys) >= DORMANT_YEARS:
            sig["items"].append({"signal": "dormant", "axis_id": aid,
                                 "last_year": max(ys), "action": "propose_demote"})

    # 7 断链：有技术的论文，没有入边
    with store.structure() as s:
        have_in = {r["dst"].lower() for r in rows(s, "SELECT dst FROM edges")}
    with store.papers() as p:
        techs = Counter((r["technique"] or "").strip() for r in rows(
            p, "SELECT technique FROM contributions WHERE coalesce(technique,'')!=''"))
    for tech, n in techs.items():
        if n >= 2 and tech.lower() not in have_in:
            sig["items"].append({"signal": "broken_chain", "technique": tech,
                                 "papers": n, "action": "open_hole"})

    # 8 技术画像空白（副作用未知）
    with store.papers() as p:
        crit_targets = {(r["target_technique"] or "").strip().lower()
                        for r in rows(p, "SELECT target_technique FROM critiques")}
    for tech, n in techs.items():
        if n >= 3 and tech.lower() not in crit_targets:
            sig["items"].append({"signal": "unknown_side_effect", "technique": tech,
                                 "papers": n, "action": "open_hole"})

    # 9 基线漂移
    drift = _baseline_drift(store)
    for d in drift:
        sig["items"].append({"signal": "baseline_drift", **d, "action": "propose_revise_clause"})

    # 10 共识动摇：contested 观点
    with store.structure() as s:
        for r in rows(s, "SELECT id,statement FROM claims WHERE status='contested'"):
            sig["items"].append({"signal": "consensus_shaken", "claim_id": r["id"],
                                 "statement": r["statement"], "action": "notify"})

    # 11 条款反例
    with store.structure() as s:
        for r in rows(s, "SELECT clause_id,text FROM clauses WHERE status='active' AND coalesce(counter,'')!=''"):
            sig["items"].append({"signal": "clause_counterexample", "clause_id": r["clause_id"],
                                 "action": "propose_revise_clause"})
    sig["open_holes"] = len(holes)
    return sig


def _two_means(vecs: list[dict]) -> tuple | None:
    """确定性 2-means：以最远的一对做初始种子，迭代 8 次。

    不用随机种子，保证同一份数据每次得到同样的拆分（回放可复现）。
    """
    n = len(vecs)
    if n < 6:
        return None
    bi, bj, best = 0, 1, 2.0
    for i in range(n):
        for j in range(i + 1, n):
            d = 1 - tu.cosine(vecs[i], vecs[j])
            if d < best:
                bi, bj, best = i, j, d
    c1, c2 = vecs[bi], vecs[bj]
    g1: list[int] = []
    g2: list[int] = []
    for _ in range(8):
        g1, g2 = [], []
        for k, v in enumerate(vecs):
            (g1 if tu.cosine(v, c1) >= tu.cosine(v, c2) else g2).append(k)
        if not g1 or not g2:
            return None
        c1 = tu.centroid([vecs[k] for k in g1])
        c2 = tu.centroid([vecs[k] for k in g2])
    return c1, c2, [vecs[k] for k in g1], [vecs[k] for k in g2]


def _baseline_drift(store: Store) -> list[dict]:
    """比较近 2 年与之前的基线使用率。"""
    with store.papers() as p:
        years = {r["paper_id"]: r["year"] for r in rows(p, "SELECT paper_id,year FROM papers")}
        prs = rows(p, "SELECT paper_id,axis_id,baselines FROM practices")
    if not prs:
        return []
    cur = max(years.values() or [0])
    recent = Counter()
    older = Counter()
    n_recent = n_older = 0
    for pr in prs:
        y = years.get(pr["paper_id"], 0)
        bl = js(pr["baselines"])
        if y >= cur - 2:
            n_recent += 1
            recent.update(bl)
        else:
            n_older += 1
            older.update(bl)
    out = []
    if n_recent < 2 or n_older < 2:
        return out
    for name, c in older.items():
        before = c / n_older
        after = recent.get(name, 0) / n_recent
        if before - after > BASE_DRIFT:
            out.append({"baseline": name, "before": round(before, 3), "after": round(after, 3)})
    return out


def act(store: Store, sig: dict | None = None, create: bool = True) -> dict:
    """把信号变成提案/缺口单，并去重（同一目标只开一张）。"""
    sig = sig or signals(store)
    made = {"proposals": [], "holes": []}
    if not create:
        return {"signals": sig, "created": made}

    with store.structure() as s:
        open_holes = {(h["kind"], h["axis_id"] or h["desc"]) for h in
                      rows(s, "SELECT * FROM holes WHERE status IN ('open','running')")}
        existing_props = rows(s, "SELECT kind,payload FROM proposals WHERE status IN ('draft','escalated')")

    def has_prop(kind, **match):
        for p in existing_props:
            if p["kind"] != kind:
                continue
            pl = json.loads(p["payload"] or "{}")
            if all(pl.get(k) == v for k, v in match.items()):
                return True
        return False

    for it in sig["items"]:
        a = it["action"]
        if a == "propose_promote" and not has_prop("promote_axis", axis_id=it["axis_id"]):
            pid = add_proposal(store, "promote_axis", {"axis_id": it["axis_id"]},
                               evidence=f"{it['papers']} 篇 / {it['groups']} 组",
                               impact=json.dumps({"affects": ["assignment", "route"]}, ensure_ascii=False),
                               risk="human")
            made["proposals"].append({"kind": "promote_axis", "proposal_id": pid})
        elif a == "propose_demote" and not has_prop("demote_axis", axis_id=it["axis_id"]):
            pid = add_proposal(store, "demote_axis", {"axis_id": it["axis_id"]},
                               evidence=f"最后活跃 {it['last_year']}", risk="agent")
            made["proposals"].append({"kind": "demote_axis", "proposal_id": pid})
        elif a == "propose_split" and not has_prop("split_axis", axis_id=it["axis_id"]):
            pid = add_proposal(store, "split_axis",
                               {"axis_id": it["axis_id"], "children": [],
                                "note": "需 Agent 依据双簇成员命名"},
                               evidence=f"簇间余弦 {it['centroid_cos']}", risk="human")
            made["proposals"].append({"kind": "split_axis", "proposal_id": pid})
        elif a == "propose_merge" and not has_prop("merge_axis", absorbed=it["b"]):
            pid = add_proposal(store, "merge_axis", {"into": it["a"], "absorbed": it["b"]},
                               evidence=f"共现率 {it['ratio']}", risk="human")
            made["proposals"].append({"kind": "merge_axis", "proposal_id": pid})
        elif a == "propose_revise_clause" and it.get("clause_id"):
            if not has_prop("revise_clause", clause_id=it["clause_id"]):
                pid = add_proposal(store, "revise_clause",
                                   {"clause_id": it["clause_id"], "text": "", "support": 0.0},
                                   evidence="出现反例或基线漂移", risk="agent")
                made["proposals"].append({"kind": "revise_clause", "proposal_id": pid})
        elif a == "open_hole":
            key = (it["signal"], it.get("axis_id") or it.get("technique", ""))
            if key in open_holes:
                continue
            hid = open_hole(store, it)
            made["holes"].append({"hole_id": hid, "signal": it["signal"]})

    # 挖出来的条款候选本身也要进队列，否则它们永远停在 candidate，SOP 页为空。
    with store.structure() as s:
        cands = [dict(r) for r in rows(s, "SELECT * FROM clauses WHERE status='candidate'")]
    for c in cands:
        if has_prop("add_clause", clause_id=c["clause_id"]):
            continue
        pid = add_proposal(store, "add_clause",
                           {"clause_id": c["clause_id"], "text": c["text"],
                            "axis_id": c["axis_id"], "clause_kind": c["kind"],
                            "support": c["support"], "normative": bool(c["normative"]),
                            "evidence": c["evidence"] or "统计挖掘"},
                           evidence=c["evidence"] or f"支持率 {c['support']}",
                           impact=json.dumps({"affects": ["control_group", "redteam"]},
                                             ensure_ascii=False),
                           risk="human" if c["normative"] else "agent")
        made["proposals"].append({"kind": "add_clause", "proposal_id": pid,
                                  "clause": c["clause_id"]})
    return {"signals": sig, "created": made}


HOLE_RECIPES = {
    "thin_evidence": ("证据单薄", "对成员论文做前向/后向引用检索 + 术语别名扩展",
                      "≥2 个独立课题组，或检索穷尽"),
    "broken_chain": ("演化链断点", "读该技术首篇论文的引言，找它批评的前人工作", "补上入边并附页码，或标无明确前身"),
    "unknown_side_effect": ("副作用未知", "前向引用中检索批评性表述", "≥1 条批评记录，或检索穷尽"),
    "evaluation_gap": ("评测空白", "检索技术×算例组合；找不到则提议实验", "找到已有结果，或实验完成"),
    "dispute_open": ("争议未决", "检索争议之后明确回应的论文", "找到裁决证据，或保持争议并附进展"),
    "coverage_gap": ("覆盖缺口", "与综述目录/教材目录/Zotero 标签对照", "映射成功，或开新轴候选"),
    "frontier_drift": ("前沿漂移", "围绕萌芽簇代表论文扩展检索", "达到新轴门槛，或 3 次体检无增长"),
    "axis_gap": ("轴空缺", "针对该轴做定向检索补成员", "成员达到 3 篇 / 2 组，或检索穷尽"),
}


def open_hole(store: Store, item: dict) -> str:
    kind = item["signal"]
    name, action, close = HOLE_RECIPES.get(kind, ("结构缺口", "定向检索", "补上或放弃"))
    hid = f"h-{kind}-{abs(hash((item.get('axis_id'), item.get('technique'), name))) % 100000:05d}"
    with store.structure() as s:
        s.execute("""INSERT INTO holes(hole_id,kind,axis_id,desc,action,close_condition,status,
                      attempts,queries,note,created_at) VALUES(?,?,?,?,?,?,'open',0,0,'',?)""",
                  (hid, kind, item.get("axis_id", ""),
                   item.get("technique") or item.get("desc") or name,
                   action, close, now()))
    return hid


def close_hole(store: Store, hole_id: str, status: str = "closed", note: str = "",
               queries: int = 0) -> dict:
    if status not in ("closed", "exhausted", "escalated"):
        raise ValueError("status must be closed|exhausted|escalated")
    with store.structure() as s:
        r = s.execute("SELECT * FROM holes WHERE hole_id=?", (hole_id,)).fetchone()
        if not r:
            return {"ok": False, "reason": "no such hole"}
        s.execute("UPDATE holes SET status=?,note=?,closed_at=?,queries=queries+? WHERE hole_id=?",
                  (status, note, now(), queries, hole_id))
    return {"ok": True, "hole_id": hole_id, "status": status}


def escalate_hole(store: Store, hole_id: str) -> dict:
    """检索穷尽仍补不上 → 疑似研究空白，交探索引擎。"""
    r = close_hole(store, hole_id, "escalated", "检索穷尽仍无证据，转疑似研究空白")
    if r.get("ok"):
        with store.structure() as s:
            s.execute("INSERT INTO proposals(proposal_id,kind,payload,evidence,impact,risk,status,"
                      "agent_checks,reason,created_at) VALUES(?,?,?,?,?,'human','escalated','','',?)",
                      ("pr-gap-" + hole_id, "dispute",
                       json.dumps({"statement": f"疑似研究空白：{hole_id}",
                                   "axis_id": ""}, ensure_ascii=False),
                       hole_id, "{}", now()))
    return r


def holes_list(store: Store, status: str = "open") -> list[dict]:
    with store.structure() as s:
        return [dict(r) for r in rows(s, "SELECT * FROM holes WHERE status=? ORDER BY created_at", (status,))]
