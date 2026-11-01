#!/usr/bin/env python3
"""时间切分回放与验收题。

回放的原则：不写"模拟"逻辑，而是真的用同一套代码跑。做法是把证据按年份
分批灌进一个**独立的工作区**，每灌一年就跑一次归纳与审查，看结构自己长成
什么样。这样回放结果与真实运行同源，不存在"回放通过但实跑失败"。
"""
from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

from . import health, induce, review_agent, textutil as tu
from .db import Store, now, rows


def _copy_papers_up_to(src: Store, dst: Store, max_year: int) -> dict:
    """把源库中 year<=max_year 的论文与证据复制到目标库（幂等）。"""
    with src.papers() as sp:
        papers = [dict(r) for r in rows(sp, "SELECT * FROM papers WHERE year<=? ORDER BY year", (max_year,))]
        pids = [p["paper_id"] for p in papers]
        if not pids:
            return {"papers": 0}
        qs = ",".join("?" * len(pids))
        contribs = [dict(r) for r in rows(sp, f"SELECT * FROM contributions WHERE paper_id IN ({qs})", pids)]
        practices = [dict(r) for r in rows(sp, f"SELECT * FROM practices WHERE paper_id IN ({qs})", pids)]
        critiques = [dict(r) for r in rows(sp, f"SELECT * FROM critiques WHERE paper_id IN ({qs})", pids)]
        first_author = {}
        for p in papers:
            au = json.loads(p["authors"] or "[]")
            first_author[p["paper_id"]] = str(au[0]).split(",")[0].strip().lower() if au else ""

    with dst.papers() as dp:
        for p in papers:
            cols = ("paper_id,doi,title,year,venue,authors,abstract,source,zotero_key,pdf_path,"
                    "fulltext_path,fulltext_chars,meta_level,added_at")
            dp.execute(f"INSERT OR IGNORE INTO papers({cols}) VALUES({','.join('?'*14)})",
                       tuple(p[c] for c in cols.split(",")))
        for c in contribs:
            dp.execute("""INSERT OR IGNORE INTO contributions(paper_id,target_text,technique,method,claim,
                          addr_technique,addr_problem,evidence,level,source,axis_id,assign_conf,
                          assign_method,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (c["paper_id"], c["target_text"], c.get("technique"), c.get("method"),
                        c.get("claim"), c.get("addr_technique"), c.get("addr_problem"),
                        c.get("evidence"), c.get("level"), c.get("source"),
                        c.get("axis_id"), c.get("assign_conf"), c.get("assign_method"),
                        c.get("created_at") or now()))
        for pr in practices:
            dp.execute("""INSERT OR IGNORE INTO practices(paper_id,axis_id,baselines,benchmarks,params,
                          metrics,evidence,level,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                       (pr["paper_id"], pr.get("axis_id"), pr.get("baselines"), pr.get("benchmarks"),
                        pr.get("params"), pr.get("metrics"), pr.get("evidence"), pr.get("level"),
                        pr.get("created_at") or now()))
        for cr in critiques:
            dp.execute("""INSERT OR IGNORE INTO critiques(paper_id,target_technique,target_text,problem,kind,
                          evidence,source,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                       (cr["paper_id"], cr.get("target_technique"), cr.get("target_text"),
                        cr.get("problem"), cr.get("kind"), cr.get("evidence"), cr.get("source"),
                        cr.get("created_at") or now()))
    with dst.papers() as dp:
        total = dp.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    return {"papers": total, "copied": len(papers), "contributions": len(contribs),
            "practices": len(practices), "critiques": len(critiques)}


def run_replay(src_root: str | Path, dest_root: str | Path, cutoff: int,
               min_size: int = 3, cluster_threshold: float = 0.28,
               report_path: str | Path | None = None) -> dict:
    """从 cutoff 起逐年回放。dest 会被重建。"""
    src = Store(src_root)
    dest = Path(dest_root)
    if dest.exists():
        shutil.rmtree(dest)
    dst = Store(dest)
    dst.init()

    with src.papers() as sp:
        years = sorted({r["year"] for r in rows(sp, "SELECT DISTINCT year FROM papers WHERE year>0")})
    if not years:
        return {"ok": False, "reason": "源库没有带年份的论文"}

    _copy_papers_up_to(src, dst, cutoff)
    hist = []
    base = induce.run_all(dst, cluster_threshold, min_size)
    boot = health.act(dst, create=True)
    rev = review_agent.review_pending(dst)
    hist.append({"year": cutoff, "phase": "bootstrap",
                 "axes": len(rows(dst.structure(), "SELECT 1 FROM axes")),
                 "created": len(base["axes"].get("created", [])),
                 "signals": len(boot["signals"]["items"]),
                 "review": rev["summary"]})

    for y in [x for x in years if x > cutoff]:
        before = _copy_papers_up_to(src, dst, y - 1)["papers"]
        added = _copy_papers_up_to(src, dst, y)
        new_n = added["papers"] - before
        if new_n <= 0:
            continue
        # 只让今年的证据参与本轮归纳：清空未归轴状态前先记录
        before_axes = {r["axis_id"] for r in rows(dst.structure(), "SELECT axis_id FROM axes")}
        ind = induce.run_all(dst, cluster_threshold, min_size)
        hb = health.act(dst, create=True)
        rev = review_agent.review_pending(dst)
        after_axes = {r["axis_id"] for r in rows(dst.structure(), "SELECT axis_id FROM axes")}
        with dst.structure() as s:
            stable = s.execute("SELECT COUNT(*) FROM axes WHERE status='stable'").fetchone()[0]
            cand = s.execute("SELECT COUNT(*) FROM axes WHERE status='candidate'").fetchone()[0]
        hist.append({"year": y, "new_papers": new_n, "total_papers": added["papers"],
                     "new_axes": sorted(after_axes - before_axes),
                     "created": len(ind["axes"].get("created", [])),
                     "stable": stable, "candidate": cand,
                     "signals": len(hb["signals"]["items"]),
                     "review": rev["summary"]})

    # 回放最容易暴露的两个毛病：轴数少于全量归纳（过度合并），或明显多于全量
    # （过度切分）。这里把两边都记下来，不做美化。
    with src.structure() as ss:
        full_axes = ss.execute("SELECT COUNT(*) FROM axes WHERE status!='deprecated'").fetchone()[0]
    with dst.structure() as ds:
        replay_axes = ds.execute("SELECT COUNT(*) FROM axes WHERE status!='deprecated'").fetchone()[0]
    rep = {"cutoff": cutoff, "years": hist, "history_len": len(hist),
           "axes_full": full_axes, "axes_replay": replay_axes,
           "over_merged": replay_axes < full_axes,
           "over_split": replay_axes > full_axes,
           "final": metrics(dst), "generated": now()}
    if report_path:
        Path(report_path).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    return rep


# ---------------------------------------------------------------- metrics

def metrics(store: Store) -> dict:
    """Q2 / Q5 / Q7 / Q8 可直接算；Q1 需要假设文件；Q3/Q4/Q6 见 report。"""
    from .health import signals
    from .reflect import zero_stats
    from .review_agent import quality

    with store.papers() as p:
        total = p.execute("SELECT COUNT(*) FROM contributions").fetchone()[0]
        un = p.execute("SELECT COUNT(*) FROM contributions WHERE coalesce(axis_id,'')=''").fetchone()[0]
    with store.structure() as s:
        logs = [dict(r) for r in rows(s, "SELECT op,target FROM changelog")]
        axes = {r["axis_id"]: r["status"] for r in rows(s, "SELECT axis_id,status FROM axes")}
    promoted = {l["target"] for l in logs if l["op"] == "promote_axis"}
    demoted = {l["target"] for l in logs if l["op"] == "demote_axis"}
    flips = sorted(promoted & demoted)
    zs = zero_stats(store)
    q = quality(store)
    sig = signals(store)
    return {
        "Q2_unassigned_rate": round(un / total, 3) if total else 0.0,
        "Q5_flips": flips,
        "Q5_flip_count": len(flips),
        "axes": axes,
        "Q7_zero_ratio": zs["zero_ratio"],
        "Q7_avg_candidates": zs["avg_candidates"],
        "Q8": q,
        "unassigned_pileup": sig["unassigned_rate"],
    }


def compare_to_hypothesis(store: Store, hypothesis_path: str | Path) -> dict:
    """Q1：盲归纳对用户假设的找回率。"""
    import yaml

    hyp = yaml.safe_load(Path(hypothesis_path).read_text(encoding="utf-8"))
    return induce.compare_hypothesis(store, hyp or {})


def delete_recover(store: Store, axis_id: str = "", n: int = 2) -> dict:
    """Q6 的机械代理：抽走某轴若干论文的贡献，看体检是否开出对应缺口。"""
    from .health import act, signals

    with store.papers() as p:
        if not axis_id:
            r = p.execute("""SELECT axis_id, COUNT(DISTINCT paper_id) c FROM contributions
                             WHERE coalesce(axis_id,'')!='' GROUP BY axis_id
                             ORDER BY c DESC LIMIT 1""").fetchone()
            if not r:
                return {"ok": False, "reason": "没有可用的轴"}
            axis_id = r["axis_id"]
        pids = [x["paper_id"] for x in rows(
            p, "SELECT DISTINCT paper_id FROM contributions WHERE axis_id=? LIMIT ?", (axis_id, n))]
        for pid in pids:
            p.execute("UPDATE contributions SET axis_id=NULL WHERE paper_id=? AND axis_id=?", (pid, axis_id))
    sig = signals(store)
    made = act(store, sig, create=True)
    holes = [h for h in made["created"]["holes"]]
    reopened = bool(holes)
    return {"ok": True, "axis_id": axis_id, "removed_papers": pids,
            "holes_opened": holes, "recovered_signal": reopened,
            "note": "只验证缺口能否被识别；真正的检索补回需联网跑补缺"}
