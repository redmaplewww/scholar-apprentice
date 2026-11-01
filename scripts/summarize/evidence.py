#!/usr/bin/env python3
"""证据层：入库、抽取工单、抽取结果提交。

分工：脚本负责取全文、校验字段、去重、落库；语言理解由 Agent 按工单完成，
再原样提交。提交时只做结构校验，不在这一层判断"说得对不对"——那是审查
Agent 的 A1–A5。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import textutil as tu
from .db import Store, js, now, rows

REQUIRED_RECORD = ("paper_id", "target_text", "evidence")


# --------------------------------------------------------------- papers

def _paper_id(rec: dict) -> str:
    if rec.get("doi"):
        return "doi:" + str(rec["doi"]).strip().lower()
    if rec.get("zotero_key"):
        return "zotero:" + str(rec["zotero_key"])
    return "manual:" + re.sub(r"\W+", "-", (rec.get("title") or "untitled"))[:80].lower()


def upsert_paper(store: Store, rec: dict) -> str:
    pid = rec.get("paper_id") or _paper_id(rec)
    with store.papers() as p:
        p.execute(
            """INSERT INTO papers(paper_id,doi,title,year,venue,authors,abstract,source,
                                  zotero_key,pdf_path,fulltext_path,fulltext_chars,meta_level,added_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(paper_id) DO UPDATE SET
                 title=coalesce(nullif(excluded.title,''),papers.title),
                 abstract=coalesce(nullif(excluded.abstract,''),papers.abstract),
                 fulltext_path=coalesce(nullif(excluded.fulltext_path,''),papers.fulltext_path),
                 fulltext_chars=max(papers.fulltext_chars, excluded.fulltext_chars),
                 meta_level=CASE WHEN excluded.meta_level='fulltext' THEN 'fulltext'
                                 ELSE papers.meta_level END""",
            (
                pid, (rec.get("doi") or "").lower(), rec.get("title", ""), rec.get("year") or 0,
                rec.get("venue", ""), json.dumps(rec.get("authors") or [], ensure_ascii=False),
                rec.get("abstract", ""), rec.get("source", "manual"),
                rec.get("zotero_key", ""), rec.get("pdf_path", ""),
                rec.get("fulltext_path", ""), int(rec.get("fulltext_chars") or 0),
                rec.get("meta_level") or ("fulltext" if rec.get("fulltext_path") else "abstract"),
                now(),
            ),
        )
        p.execute("INSERT INTO ingest_log(paper_id,action,detail,at) VALUES(?,?,?,?)",
                  (pid, "upsert", rec.get("source", ""), now()))
    return pid


def ingest_zotero(store: Store, collection: str = "", since: str = "", limit: int = 300) -> dict:
    """从 Zotero 批量导入元数据与全文路径。"""
    from .zotero_bridge import bulk_export

    data = bulk_export(collection=collection, since=since, limit=limit)
    if not data.get("ok"):
        return {"ok": False, "reason": data.get("reason", "zotero unavailable")}
    added, seen = 0, 0
    for it in data.get("items", []):
        pid = upsert_paper(
            store,
            {
                "doi": it.get("doi", ""),
                "zotero_key": it.get("key", ""),
                "title": it.get("title", ""),
                "year": _year(it.get("year")),
                "venue": it.get("venue", ""),
                "authors": it.get("authors", []),
                "abstract": it.get("abstract", ""),
                "source": "zotero",
                "pdf_path": it.get("pdf_path", ""),
                "fulltext_path": it.get("fulltext_path", ""),
                "fulltext_chars": it.get("fulltext_chars", 0),
                "meta_level": "fulltext" if it.get("fulltext_path") else "abstract",
            },
        )
        added += 1
        if it.get("doi"):
            seen += 1
    return {"ok": True, "imported": added, "with_doi": seen, "total_in_library": data.get("count", 0)}


def _year(v) -> int:
    m = re.search(r"\d{4}", str(v or ""))
    return int(m.group(0)) if m else 0


def load_fulltext(store: Store, paper_id: str, limit: int = 400000) -> str:
    with store.papers() as p:
        r = p.execute("SELECT fulltext_path, abstract FROM papers WHERE paper_id=?",
                      (paper_id,)).fetchone()
    if not r:
        return ""
    path = r["fulltext_path"] or ""
    if path and Path(path).exists():
        try:
            txt = Path(path).read_text(encoding="utf-8", errors="ignore")
            return txt[:limit]
        except OSError:
            pass
    return r["abstract"] or ""


# ------------------------------------------------------------ work order

EXTRACT_INSTRUCTION = """你在为「领域归纳」抽取证据。逐篇阅读下列论文，只写论文自己说过的话，不要补充你的先验。
每篇输出三类记录（可以只输出其中一类，宁少勿滥）：

1. contributions：这篇改动了哪个部件（target_text 用自由文本写，例如 "nonlinear weights near critical points"，
   不要从任何现成轴列表里挑）、用了什么做法 method、声称什么效果 claim、
   针对谁的什么问题 addresses{technique,problem}、出处 evidence（页码/公式/表格，给到能复查的粒度）。
2. practices：它对比了哪些基线 baselines、用了哪些算例 benchmarks、报告了哪些参数 params、指标 metrics。
3. critiques：它对前人的批评或自陈局限 target_technique / problem / evidence / kind
   （kind ∈ critique|limitation|comment|erratum|user_annotation）。

硬要求：
- evidence 必须能定位（"p.5 Eq.(12)" 这类），没把握就写 "p.? 摘要"。abstract_only 的论文不要写"解决了什么"。
- 一条记录只写一件事；不确定的字段留空字符串，不要猜。
- 输出严格 JSON：{"records":[{"paper_id":..., "contributions":[...], "practices":[...], "critiques":[...]}]}
"""


def workorder_extract(store: Store, limit: int = 10, only_missing: bool = True,
                      include_fulltext: bool = False) -> dict:
    with store.papers() as p:
        if only_missing:
            q = """SELECT * FROM papers p WHERE NOT EXISTS(
                     SELECT 1 FROM contributions c WHERE c.paper_id=p.paper_id)
                   ORDER BY p.year DESC, p.paper_id LIMIT ?"""
        else:
            q = "SELECT * FROM papers p ORDER BY p.year DESC, p.paper_id LIMIT ?"
        recs = rows(p, q, (limit,))
    papers = []
    for r in recs:
        item = {
            "paper_id": r["paper_id"],
            "title": r["title"],
            "year": r["year"],
            "venue": r["venue"],
            "level": r["meta_level"],
            "abstract": r["abstract"],
            "fulltext_path": r["fulltext_path"] if (r["fulltext_path"] and Path(r["fulltext_path"]).exists()) else "",
            "pdf_path": r["pdf_path"],
        }
        if include_fulltext:
            item["fulltext_excerpt"] = load_fulltext(store, r["paper_id"], 12000)
        papers.append(item)
    return {"instruction": EXTRACT_INSTRUCTION, "count": len(papers), "papers": papers}


def commit_extraction(store: Store, payload: dict) -> dict:
    """校验并写入 Agent 提交的抽取结果；不合格的进 unadopted。"""
    records = payload.get("records") or []
    ok = {"contributions": 0, "practices": 0, "critiques": 0}
    rejected = []
    with store.papers() as p:
        known = {r["paper_id"] for r in rows(p, "SELECT paper_id FROM papers")}
    for rec in records:
        pid = rec.get("paper_id", "")
        if pid not in known:
            rejected.append({"paper_id": pid, "why": "unknown paper_id"})
            continue
        for c in rec.get("contributions", []):
            miss = [k for k in ("target_text", "evidence") if not c.get(k)]
            if miss:
                _unadopt(store, "extract", c, "G2", "missing " + ",".join(miss))
                continue
            with store.papers() as p:
                dup = p.execute(
                    "SELECT 1 FROM contributions WHERE paper_id=? AND target_text=? AND coalesce(method,'')=?",
                    (pid, c["target_text"], c.get("method", "")),
                ).fetchone()
                if dup:
                    continue
                p.execute(
                    """INSERT INTO contributions(paper_id,target_text,technique,method,claim,
                        addr_technique,addr_problem,evidence,level,source,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, c["target_text"], c.get("technique", ""), c.get("method", ""),
                     c.get("claim", ""), c.get("addr_technique", ""), c.get("addr_problem", ""),
                     c["evidence"], c.get("level", ""), c.get("source", "agent"), now()),
                )
            ok["contributions"] += 1
        for pr in rec.get("practices", []):
            if not pr.get("evidence"):
                _unadopt(store, "extract", pr, "G2", "missing evidence")
                continue
            with store.papers() as p:
                p.execute(
                    """INSERT INTO practices(paper_id,axis_id,baselines,benchmarks,params,metrics,
                        evidence,level,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (pid, pr.get("axis_id", ""), json.dumps(js(pr.get("baselines")), ensure_ascii=False),
                     json.dumps(js(pr.get("benchmarks")), ensure_ascii=False),
                     json.dumps(js(pr.get("params")), ensure_ascii=False),
                     json.dumps(js(pr.get("metrics")), ensure_ascii=False),
                     pr["evidence"], pr.get("level", ""), now()),
                )
            ok["practices"] += 1
        for cr in rec.get("critiques", []):
            if not cr.get("problem") or not cr.get("evidence"):
                _unadopt(store, "extract", cr, "G2", "missing problem/evidence")
                continue
            with store.papers() as p:
                p.execute(
                    """INSERT INTO critiques(paper_id,target_technique,target_text,problem,kind,
                        evidence,source,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (pid, cr.get("target_technique", ""), cr.get("target_text", ""),
                     cr["problem"], cr.get("kind", "critique"), cr["evidence"],
                     cr.get("source", "agent"), now()),
                )
            ok["critiques"] += 1
    return {"ok": True, **ok, "rejected": rejected}


def _unadopt(store: Store, unit: str, payload: dict, gate: str, reason: str) -> None:
    with store.structure() as s:
        s.execute("INSERT INTO unadopted(unit,payload,failed_gate,reason,at) VALUES(?,?,?,?,?)",
                  (unit, json.dumps(payload, ensure_ascii=False), gate, reason, now()))


def stats(store: Store) -> dict:
    with store.papers() as p:
        def c(sql):
            r = p.execute(sql).fetchone()
            return r[0] if r else 0

        return {
            "papers": c("SELECT COUNT(*) FROM papers"),
            "fulltext": c("SELECT COUNT(*) FROM papers WHERE meta_level='fulltext'"),
            "contributions": c("SELECT COUNT(*) FROM contributions"),
            "unassigned": c("SELECT COUNT(*) FROM contributions WHERE axis_id IS NULL OR axis_id=''"),
            "practices": c("SELECT COUNT(*) FROM practices"),
            "critiques": c("SELECT COUNT(*) FROM critiques"),
        }
