#!/usr/bin/env python3
"""scholar.py —— 领域归纳与学术 SOP 的命令行入口。

设计约定：脚本做确定性的事（入库校验、聚类、门槛、审查、渲染、回放），
语言理解由 Agent 按工单完成。所以你会看到 workorder / commit 成对出现，
以及 reflect 只接受 Agent 写好的 JSON。

常用：
  scholar.py init --ws 领域/weno --domain weno
  scholar.py ingest zotero --ws 领域/weno --collection WENO --limit 300
  scholar.py workorder extract --ws 领域/weno --limit 10 > order.json
  scholar.py commit extract --ws 领域/weno --file filled.json
  scholar.py induce --ws 领域/weno
  scholar.py reflect --ws 领域/weno --unit paper:doi:xxx --file refl.json
  scholar.py review --ws 领域/weno
  scholar.py queue list|approve|reject|revert
  scholar.py healthcheck --ws 领域/weno
  scholar.py render --ws 领域/weno
  scholar.py replay build --src 领域/weno --dest 领域/weno-replay --cutoff 2012
  scholar.py eval --ws 领域/weno --hypothesis domain-packs/weno/hypothesis.yaml
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from summarize import evidence, health, induce, queue, reflect, render, replay  # noqa: E402
from summarize import review_agent, textutil, zotero_bridge  # noqa: E402
from summarize.db import Store, now, rows  # noqa: E402


def _store(a) -> Store:
    return Store(a.ws)


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _read_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ commands

def cmd_init(a) -> dict:
    st = _store(a)
    st.init()
    st.set_meta("domain", a.domain or st.root.name)
    st.set_meta("skeleton_version", "0.1")
    st.set_meta("sop_version", "0.1")
    for k, v in (("core_question", a.core_question or ""), ("shared_principles", a.principles or ""),
                 ("paradigm_scope", a.scope or "")):
        if v:
            st.set_meta(k, v)
    z = zotero_bridge.status()
    return {"ok": True, "ws": str(st.root), "domain": st.meta("domain"),
            "zotero": {"ok": bool(z.get("ok")), "hint": zotero_bridge.health_hint()},
            "db": {"papers": str(st.papers_path), "structure": str(st.structure_path)},
            "vault": str(st.vault)}


def cmd_status(a) -> dict:
    st = _store(a)
    ev = evidence.stats(st)
    with st.structure() as s:
        axes = {r["status"]: r["n"] for r in rows(s, "SELECT status,COUNT(*) n FROM axes GROUP BY status")}
        props = {r["status"]: r["n"] for r in rows(s, "SELECT status,COUNT(*) n FROM proposals GROUP BY status")}
        cl = {r["status"]: r["n"] for r in rows(s, "SELECT status,COUNT(*) n FROM clauses GROUP BY status")}
        holes = {r["status"]: r["n"] for r in rows(s, "SELECT status,COUNT(*) n FROM holes GROUP BY status")}
    return {"ok": True, "evidence": ev, "axes": axes, "proposals": props, "clauses": cl, "holes": holes}


def cmd_ingest(a) -> dict:
    st = _store(a)
    if a.what == "zotero":
        return evidence.ingest_zotero(st, collection=a.collection, since=a.since, limit=a.limit)
    if a.what == "json":
        data = _read_json(a.file)
        n = 0
        for rec in data.get("papers", data if isinstance(data, list) else []):
            evidence.upsert_paper(st, rec)
            n += 1
        return {"ok": True, "imported": n}
    return {"ok": False, "reason": "use zotero|json"}


def cmd_workorder(a) -> dict:
    st = _store(a)
    if a.what == "extract":
        return evidence.workorder_extract(st, limit=a.limit, only_missing=not a.all,
                                          include_fulltext=a.fulltext)
    if a.what == "reflect":
        return reflect.template(a.unit)
    if a.what == "naming":
        return _naming_workorder(st)
    return {"ok": False, "reason": "use extract|reflect|naming"}


def _naming_workorder(st: Store) -> dict:
    with st.structure() as s:
        axes = [dict(r) for r in rows(s, "SELECT * FROM axes WHERE status IN "
                                         "('candidate','stable','declining')")]
    with st.papers() as p:
        out = []
        for a in axes:
            texts = [r["target_text"] for r in rows(
                p, "SELECT target_text FROM contributions WHERE axis_id=? LIMIT 25", (a["axis_id"],))]
            out.append({"axis_id": a["axis_id"], "current_name": a["name"],
                        "definition": a["definition"], "targets": texts})
    return {"instruction": "给每个候选轴起一个领域内的部件名（2–6 字），并写一句定义：它解决什么、"
                           "不解决什么。只依据给出的 target_text，不要引入先验。输出 "
                           '{"axes":[{"axis_id":...,"name":...,"definition":...}]}',
            "axes": out}


def cmd_commit(a) -> dict:
    st = _store(a)
    data = _read_json(a.file)
    if a.what == "extract":
        return evidence.commit_extraction(st, data)
    if a.what == "naming":
        renamed, logs = 0, []
        with st.structure() as s:
            for ax in data.get("axes", []):
                before = s.execute("SELECT * FROM axes WHERE axis_id=?", (ax["axis_id"],)).fetchone()
                if not before:
                    continue
                s.execute("UPDATE axes SET name=?,definition=?,last_change=? WHERE axis_id=?",
                          (ax["name"], ax["definition"], now(), ax["axis_id"]))
                logs.append((ax["axis_id"], dict(before), ax))
                renamed += 1
        for aid, before, after in logs:
            st.log_change("agent_rename", aid, before, after, "")
        return {"ok": True, "renamed": renamed}
    if a.what == "definition":
        st.set_meta(data.get("key", ""), data.get("value", ""))
        return {"ok": True}
    return {"ok": False, "reason": "use extract|naming|definition"}


def cmd_induce(a) -> dict:
    st = _store(a)
    return induce.run_all(st, threshold=a.threshold, min_size=a.min_size)


def cmd_reflect(a) -> dict:
    st = _store(a)
    if a.file:
        return reflect.reflect(st, a.unit, _read_json(a.file))
    return {"ok": False, "reason": "need --file (or use workorder reflect for a template)"}


def cmd_review(a) -> dict:
    st = _store(a)
    return review_agent.review_pending(st, limit=a.limit)


def cmd_queue(a) -> dict:
    st = _store(a)
    if a.action == "list":
        return {"pending": queue.pending(st), "auto": queue.auto_committed(st),
                "stats": queue.queue_stats(st)}
    if a.action == "approve":
        r = queue.apply(st, a.proposal_id, by=a.by, status="human_approved")
        return r
    if a.action == "reject":
        return queue.reject(st, a.proposal_id, a.reason or "", by=a.by)
    if a.action == "revert":
        return queue.revert(st, a.proposal_id)
    if a.action == "snapshot":
        return {"ok": True, "path": st.snapshot(a.reason or "")}
    return {"ok": False, "reason": "use list|approve|reject|revert|snapshot"}


def cmd_health(a) -> dict:
    st = _store(a)
    sig = health.signals(st)
    if a.act:
        return health.act(st, sig, create=True)
    if a.close:
        return health.close_hole(st, a.close, status=a.status or "closed", note=a.note or "",
                                 queries=a.queries or 0)
    if a.escalate:
        return health.escalate_hole(st, a.escalate)
    if a.holes:
        return {"holes": health.holes_list(st, a.status or "open")}
    return sig


def cmd_render(a) -> dict:
    st = _store(a)
    return render.render_all(st)


def cmd_replay(a) -> dict:
    if a.action == "build":
        return replay.run_replay(a.src, a.dest, cutoff=a.cutoff, min_size=a.min_size,
                                 cluster_threshold=a.threshold, report_path=a.report)
    if a.action == "metrics":
        return replay.metrics(_store(a))
    if a.action == "delete-recover":
        return replay.delete_recover(_store(a), axis_id=a.axis_id or "", n=a.n)
    return {"ok": False, "reason": "use build|metrics|delete-recover"}


def cmd_eval(a) -> dict:
    st = _store(a)
    out = {"ws": str(st.root), "evidence": evidence.stats(st), "metrics": replay.metrics(st)}
    if a.hypothesis and Path(a.hypothesis).exists():
        out["Q1_hypothesis"] = replay.compare_to_hypothesis(st, a.hypothesis)
    out["zero"] = reflect.zero_stats(st)
    return out


def cmd_zotero(a) -> dict:
    if a.action == "status":
        return zotero_bridge.status()
    if a.action == "search":
        return zotero_bridge.search(a.query or "", limit=a.limit, scope=a.scope)
    if a.action == "recent":
        return zotero_bridge.recent(days=a.days, limit=a.limit)
    if a.action == "fulltext":
        return zotero_bridge.fulltext(a.key)
    return {"ok": False, "reason": "use status|search|recent|fulltext"}


# ----------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scholar.py", description="领域归纳与学术 SOP")
    ap.add_argument("--ws", default="", help="工作区根目录")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化工作区")
    p.add_argument("--domain", default="")
    p.add_argument("--core-question", dest="core_question", default="")
    p.add_argument("--principles", default="")
    p.add_argument("--scope", default="")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="总览")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("ingest", help="入库")
    p.add_argument("what", choices=["zotero", "json"])
    p.add_argument("--collection", default="")
    p.add_argument("--since", default="")
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--file", default="")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("workorder", help="生成给 Agent 的工单")
    p.add_argument("what", choices=["extract", "reflect", "naming"])
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--all", action="store_true", help="包括已抽取过的论文")
    p.add_argument("--fulltext", action="store_true", help="工单里附全文节选")
    p.add_argument("--unit", default="paper:x")
    p.set_defaults(func=cmd_workorder)

    p = sub.add_parser("commit", help="提交 Agent 填写的结果")
    p.add_argument("what", choices=["extract", "naming", "definition"])
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("induce", help="归纳：聚类成轴、成链、挖条款")
    p.add_argument("--threshold", type=float, default=0.28)
    p.add_argument("--min-size", dest="min_size", type=int, default=3)
    p.set_defaults(func=cmd_induce)

    p = sub.add_parser("reflect", help="提交一次工作单元的复盘（含沉淀门 G1–G4）")
    p.add_argument("--unit", required=True, help="paper:<id> / session:<id> / explore:<id> / ...")
    p.add_argument("--file", default="")
    p.set_defaults(func=cmd_reflect)

    p = sub.add_parser("review", help="审查 Agent：A1–A5")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("queue", help="提案队列")
    p.add_argument("action", choices=["list", "approve", "reject", "revert", "snapshot"])
    p.add_argument("proposal_id", nargs="?", default="")
    p.add_argument("--reason", default="")
    p.add_argument("--by", default="user")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("healthcheck", help="体检 / 缺口单")
    p.add_argument("--act", action="store_true", help="真的生成提案与缺口单")
    p.add_argument("--holes", action="store_true")
    p.add_argument("--close", default="")
    p.add_argument("--escalate", default="")
    p.add_argument("--status", default="")
    p.add_argument("--note", default="")
    p.add_argument("--queries", type=int, default=0)
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("render", help="渲染页面")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("replay", help="时间切分回放")
    p.add_argument("action", choices=["build", "metrics", "delete-recover"])
    p.add_argument("--src", default="")
    p.add_argument("--dest", default="")
    p.add_argument("--cutoff", type=int, default=2012)
    p.add_argument("--min-size", dest="min_size", type=int, default=3)
    p.add_argument("--threshold", type=float, default=0.28)
    p.add_argument("--report", default="")
    p.add_argument("--axis-id", dest="axis_id", default="")
    p.add_argument("--n", type=int, default=2)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("eval", help="验收题 Q1–Q8")
    p.add_argument("--hypothesis", default="")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("zotero", help="Zotero 连通性")
    p.add_argument("action", choices=["status", "search", "recent", "fulltext"])
    p.add_argument("--query", default="")
    p.add_argument("--key", default="")
    p.add_argument("--scope", default="metadata")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_zotero)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.ws:
        print("需要 --ws <工作区>", file=sys.stderr)
        return 2
    try:
        out = a.func(a)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    _out(out)
    return 0 if (not isinstance(out, dict) or out.get("ok", True)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
