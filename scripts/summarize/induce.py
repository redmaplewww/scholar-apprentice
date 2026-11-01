#!/usr/bin/env python3
"""归纳：从证据层派生出结构层与规范层。

所有产出都是"候选"（status=candidate），只在工作台可见；转正式要经审查 Agent
与人。这里不调用任何模型：聚类只用 hashed TF-IDF，命名给出高频术语，最终
命名与定义由 Agent 在工单里润色。
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from . import textutil as tu
from .db import Store, js, now, rows

DEFAULT_CLUSTER_THRESHOLD = 0.28   # 回放在真实库上调这个值
DEFAULT_ASSIGN_THRESHOLD = 0.15


# ------------------------------------------------------------ assignment

def _axes(store: Store, statuses=("candidate", "stable", "declining")) -> list[dict]:
    qs = ",".join("?" * len(statuses))
    with store.structure() as s:
        out = []
        for r in rows(s, f"SELECT * FROM axes WHERE status IN ({qs})", statuses):
            out.append({**dict(r), "centroid": json.loads(r["centroid"]) if r["centroid"] else {}})
        return out


def _contrib_text(store: Store) -> list[dict]:
    with store.papers() as p:
        return [dict(r) for r in rows(p, "SELECT id,paper_id,target_text,technique,axis_id FROM contributions")]


def assign_unassigned(store: Store, threshold: float = DEFAULT_ASSIGN_THRESHOLD,
                      margin: float = 0.06, strong: float = 0.45) -> dict:
    """把未归轴的贡献记录分到最近的轴；低于阈值留空（进未归类池）。

    轴向量优先用成员记录算质心（成员文本是归轴的直接依据），成员为空时才
    退回用名称与定义，避免候选轴刚建立、名字还是占位符时把记录错分。
    """
    contribs = _contrib_text(store)
    axes = _axes(store)
    if not axes:
        return {"assigned": 0, "unassigned": len(contribs), "reason": "no axes yet"}
    vz = tu.Vectorizer().fit([c["target_text"] for c in contribs] +
                             [a["name"] + " " + (a["definition"] or "") for a in axes])
    members: dict[str, list[str]] = {}
    for c in contribs:
        if c["axis_id"]:
            members.setdefault(c["axis_id"], []).append(c["target_text"])
    cand = {}
    for a in axes:
        texts = members.get(a["axis_id"]) or []
        if texts:
            cand[a["axis_id"]] = tu.centroid([vz.vec(t) for t in texts])
        else:
            cand[a["axis_id"]] = vz.vec(a["name"] + " " + (a["definition"] or ""))
    assigned = 0
    with store.papers() as p:
        for c in contribs:
            if c["axis_id"]:
                continue
            v = vz.vec(c["target_text"])
            scores = sorted(((tu.cosine(v, av), aid) for aid, av in cand.items()), reverse=True)
            if not scores:
                continue
            score, best = scores[0]
            second = scores[1][0] if len(scores) > 1 else 0.0
            # 只有一个轴时，任何够阈值的都会被吸收，导致过度合并。要求"明显领先
            # 第二名"或"绝对相似度足够高"，否则留在未归类池，由聚类开新轴。
            if best and score >= threshold and (score - second >= margin or score >= strong):
                p.execute("UPDATE contributions SET axis_id=?,assign_conf=?,assign_method='centroid' WHERE id=?",
                          (best, round(score, 4), c["id"]))
                with store.structure() as s:
                    s.execute("INSERT OR REPLACE INTO axis_members(axis_id,paper_id,target_key,weight) VALUES(?,?,?,?)",
                              (best, c["paper_id"], c["target_text"][:200], round(score, 4)))
                assigned += 1
    remaining = sum(1 for c in _contrib_text(store) if not c["axis_id"])
    return {"assigned": assigned, "unassigned": remaining}


# --------------------------------------------------------------- cluster

def form_candidate_axes(store: Store, threshold: float = DEFAULT_CLUSTER_THRESHOLD,
                        min_size: int = 3) -> dict:
    """对未归轴的目标文本做凝聚聚类，够规模的成为候选轴。"""
    pool = [c for c in _contrib_text(store) if not c["axis_id"]]
    if len(pool) < min_size:
        return {"created": [], "reason": "pool too small", "pool": len(pool)}

    texts = [c["target_text"] for c in pool]
    vz = tu.Vectorizer().fit(texts)
    vecs = [vz.vec(t) for t in texts]

    clusters: list[list[int]] = []
    for i, v in enumerate(vecs):
        best, score = None, 0.0
        for ci, members in enumerate(clusters):
            sc = tu.cosine(v, tu.centroid([vecs[j] for j in members]))
            if sc > score:
                best, score = ci, sc
        if best is not None and score >= threshold:
            clusters[best].append(i)
        else:
            clusters.append([i])

    # 合并趟：贪心聚类对顺序敏感（同义文本先到先得），按质心相似度反复合并
    merged = True
    while merged and len(clusters) > 1:
        merged = False
        cents = [tu.centroid([vecs[j] for j in m]) for m in clusters]
        best = (0.0, -1, -1)
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                sc = tu.cosine(cents[i], cents[j])
                if sc > best[0]:
                    best = (sc, i, j)
        score, i, j = best
        if score >= threshold:
            clusters[i] = clusters[i] + clusters[j]
            clusters.pop(j)
            merged = True

    created = []
    with store.structure() as s:
        existing = rows(s, "SELECT axis_id FROM axes")
        n = len(existing)
    for members in clusters:
        if len(members) < min_size:
            continue
        texts_c = [texts[j] for j in members]
        terms = tu.top_terms(texts_c, 4)
        name = " / ".join(terms) if terms else "待命名部件"
        axis_id = f"axis-{n + 1:02d}"
        n += 1
        definition = "（待 Agent 命名与定义）由 %d 条贡献记录聚类得到；高频术语：%s" % (
            len(members), "、".join(terms))
        with store.structure() as s:
            s.execute(
                """INSERT OR IGNORE INTO axes(axis_id,name,definition,status,evidence_count,
                     group_count,centroid,first_seen,last_change)
                   VALUES(?,?,?,'candidate',?,?,?,?,?)""",
                (axis_id, name, definition, len(members), 0,
                 json.dumps(tu.centroid([vecs[j] for j in members])), now(), now()),
            )
        paper_ids = {pool[j]["paper_id"] for j in members}
        with store.papers() as p:
            for j in members:
                p.execute("UPDATE contributions SET axis_id=?,assign_conf=?,assign_method='cluster' WHERE id=?",
                          (axis_id, 0.5, pool[j]["id"]))
        with store.structure() as s:
            for j in members:
                s.execute("INSERT OR REPLACE INTO axis_members(axis_id,paper_id,target_key,weight) VALUES(?,?,?,?)",
                          (axis_id, pool[j]["paper_id"], texts[j][:200], 0.5))
        created.append({"axis_id": axis_id, "name": name, "members": len(members),
                        "papers": len(paper_ids), "terms": terms})
    return {"created": created, "clusters": len(clusters), "pool": len(pool)}


def recompute_axis_stats(store: Store) -> dict:
    """重算每个轴的证据数与独立课题组数（按第一作者姓氏近似）。"""
    with store.papers() as p:
        papers = {r["paper_id"]: r for r in rows(p, "SELECT paper_id, authors FROM papers")}
        contribs = rows(p, "SELECT axis_id, paper_id FROM contributions WHERE axis_id IS NOT NULL")
    by_axis: dict[str, set] = defaultdict(set)
    for c in contribs:
        by_axis[c["axis_id"]].add(c["paper_id"])
    out = {}
    with store.structure() as s:
        for aid, pids in by_axis.items():
            groups = set()
            for pid in pids:
                authors = json.loads(papers[pid]["authors"] or "[]") if pid in papers else []
                if authors:
                    first = str(authors[0])
                    groups.add(first.split(",")[0].strip().lower())
            s.execute("UPDATE axes SET evidence_count=?, group_count=?, last_change=? WHERE axis_id=?",
                      (len(pids), len(groups), now(), aid))
            out[aid] = {"papers": len(pids), "groups": len(groups)}
    return out


# ----------------------------------------------------------------- edges

def build_edges(store: Store) -> dict:
    """演化边：B 声明解决了 A 的某问题 → A→B。"""
    with store.papers() as p:
        cs = rows(p, """SELECT id,paper_id,technique,addr_technique,addr_problem,evidence,axis_id
                        FROM contributions
                        WHERE coalesce(addr_technique,'')!='' AND coalesce(technique,'')!=''""")
    made = 0
    with store.structure() as s:
        for c in cs:
            src, dst = c["addr_technique"].strip(), c["technique"].strip()
            if not src or not dst or src.lower() == dst.lower():
                continue
            dup = s.execute("SELECT 1 FROM edges WHERE src=? AND dst=? AND coalesce(problem,'')=?",
                            (src, dst, c["addr_problem"] or "")).fetchone()
            if dup:
                continue
            s.execute("""INSERT INTO edges(src,dst,rel,problem,evidence,axis_id,status,created_at)
                         VALUES(?,?,'resolve',?,?,?,'candidate',?)""",
                      (src, dst, c["addr_problem"] or "", c["evidence"] or "", c["axis_id"] or "", now()))
            made += 1
    return {"edges_created": made}


# --------------------------------------------------- combos / couplings

def update_combos(store: Store) -> dict:
    with store.papers() as p:
        cs = rows(p, "SELECT paper_id, axis_id FROM contributions WHERE axis_id IS NOT NULL")
    per_paper: dict[str, set] = defaultdict(set)
    for c in cs:
        per_paper[c["paper_id"]].add(c["axis_id"])
    n = 0
    with store.structure() as s:
        for pid, aids in per_paper.items():
            for aid in aids:
                s.execute("INSERT OR IGNORE INTO combos(paper_id,axis_id,created_at) VALUES(?,?,?)",
                          (pid, aid, now()))
                n += 1
    return {"combos": n}


def update_couplings(store: Store) -> dict:
    with store.papers() as p:
        cs = rows(p, "SELECT paper_id, axis_id FROM contributions WHERE axis_id IS NOT NULL")
    per_paper: dict[str, set] = defaultdict(set)
    axis_papers: dict[str, set] = defaultdict(set)
    for c in cs:
        per_paper[c["paper_id"]].add(c["axis_id"])
        axis_papers[c["axis_id"]].add(c["paper_id"])
    pairs: Counter = Counter()
    pair_papers: dict[tuple, set] = defaultdict(set)
    for pid, aids in per_paper.items():
        al = sorted(aids)
        for i in range(len(al)):
            for j in range(i + 1, len(al)):
                pairs[(al[i], al[j])] += 1
                pair_papers[(al[i], al[j])].add(pid)
    out = []
    with store.structure() as s:
        for (a, b), n in pairs.items():
            ratio = n / max(1, min(len(axis_papers[a]), len(axis_papers[b])))
            if ratio < 0.30:
                continue
            s.execute("""INSERT INTO couplings(a,b,ratio,papers,updated_at) VALUES(?,?,?,?,?)
                         ON CONFLICT(a,b) DO UPDATE SET ratio=excluded.ratio,
                           papers=excluded.papers, updated_at=excluded.updated_at""",
                      (a, b, round(ratio, 3), json.dumps(sorted(pair_papers[(a, b)]), ensure_ascii=False), now()))
            out.append({"a": a, "b": b, "ratio": round(ratio, 3)})
    return {"couplings": out}


# ---------------------------------------------------------------- clauses

def mine_clauses(store: Store, min_support: float = 0.6, min_bench: float = 0.5,
                 since_year: int = 0) -> dict:
    """从实践记录挖描述性条款，从批评记录挖规范性条款。"""
    with store.papers() as p:
        years = {r["paper_id"]: r["year"] for r in rows(p, "SELECT paper_id, year FROM papers")}
        practices = rows(p, "SELECT * FROM practices")
        critiques = rows(p, "SELECT * FROM critiques")
    axis_of_paper = {}
    with store.papers() as p:
        for r in rows(p, """SELECT paper_id, axis_id FROM contributions
                            WHERE axis_id IS NOT NULL GROUP BY paper_id"""):
            axis_of_paper[r["paper_id"]] = r["axis_id"]

    per_axis_n: Counter = Counter()
    base: dict[str, Counter] = defaultdict(Counter)
    bench: dict[str, Counter] = defaultdict(Counter)
    params: Counter = Counter()
    for pr in practices:
        if since_year and (years.get(pr["paper_id"], 0) < since_year):
            continue
        aid = pr["axis_id"] or axis_of_paper.get(pr["paper_id"], "")
        if not aid:
            continue
        per_axis_n[aid] += 1
        for b in js(pr["baselines"]):
            base[aid][b] += 1
        for b in js(pr["benchmarks"]):
            bench[aid][b] += 1
        for x in js(pr["params"]):
            params[x] += 1

    created = []
    with store.structure() as s:
        for aid, n in per_axis_n.items():
            if n < 2:
                continue
            for name, c in base[aid].items():
                sup = c / n
                if sup >= min_support:
                    created.append(_clause(s, aid, "baseline", f"新工作须对比 {name}", sup, 0))
            for name, c in bench[aid].items():
                sup = c / n
                if sup >= min_bench:
                    created.append(_clause(s, aid, "benchmark", f"必测算例：{name}", sup, 0))
        for name, c in params.items():
            if c >= 3:
                created.append(_clause(s, "", "param", f"必报参数：{name}", c / max(1, sum(params.values())), 0))

        # 全局条款：跨轴统计，供领域地图的"全局评测"用
        n_all = max(1, sum(per_axis_n.values()))
        gbench: Counter = Counter()
        gbase: Counter = Counter()
        for aid in per_axis_n:
            gbench.update(bench[aid])
            gbase.update(base[aid])
        for name, c in gbench.items():
            sup = c / n_all
            if sup >= min_bench:
                created.append(_clause(s, "", "benchmark", f"全局必测算例：{name}", sup, 0))
        for name, c in gbase.items():
            sup = c / n_all
            if sup >= min_support:
                created.append(_clause(s, "", "baseline", f"全局基线：{name}", sup, 0))

        # 规范性：同一技术被 ≥2 个独立来源批评同一问题
        crit: dict[tuple, list] = defaultdict(list)
        for cr in critiques:
            key = ((cr["target_technique"] or "").strip().lower(), (cr["problem"] or "").strip())
            if key[0] and key[1]:
                crit[key].append(cr)
        for (tech, problem), items in crit.items():
            pids = {i["paper_id"] for i in items}
            kinds = {i["kind"] for i in items}
            if len(pids) >= 2 or kinds & {"comment", "erratum"}:
                created.append(_clause(s, "", "redline",
                                       f"{tech} 不得在 {problem} 未排除的情况下作为结论依据",
                                       len(pids) / max(1, len(pids) + 1), 1,
                                       evidence=json.dumps([dict(i) for i in items], ensure_ascii=False)))
    return {"clauses": created}


def _clause(s, axis_id: str, kind: str, text: str, support: float, normative: int,
            evidence: str = "") -> dict:
    cid = f"cl-{kind}-{abs(hash((axis_id, text))) % 100000:05d}"
    s.execute("""INSERT INTO clauses(clause_id,kind,axis_id,text,status,normative,support,
                  counter,evidence,created_at,updated_at)
                 VALUES(?,?,?,?,'candidate',?,?,'',?,?,?)
                 ON CONFLICT(clause_id) DO UPDATE SET support=excluded.support,
                   updated_at=excluded.updated_at""",
              (cid, kind, axis_id, text, normative, round(support, 3), evidence, now(), now()))
    return {"clause_id": cid, "kind": kind, "text": text, "support": round(support, 3)}


# -------------------------------------------------------------- hypothesis

def compare_hypothesis(store: Store, hypothesis: dict) -> dict:
    """C6/Q1：盲归纳得到的轴 vs 用户假设，给出找回情况。

    比较的是"轴的成员内容"，不只看名字：用户假设往往用中文（如"非线性权重"），
    盲归纳出的轴名可能是英文术语或占位符，只比名字会低估找回率。
    """
    induced = _axes(store, ("candidate", "stable", "declining"))
    with store.papers() as p:
        member_texts: dict[str, list[str]] = defaultdict(list)
        for r in rows(p, "SELECT axis_id,target_text FROM contributions WHERE coalesce(axis_id,'')!=''"):
            member_texts[r["axis_id"]].append(r["target_text"])

    def axis_text(a: dict) -> str:
        texts = member_texts.get(a["axis_id"], [])[:8]
        return a["name"] + " " + (a["definition"] or "") + " " + " ".join(texts)

    def hyp_text(h: dict) -> str:
        return (h.get("name", "") + " " + h.get("definition", "") + " "
                + " ".join(h.get("targets", [])))

    docs = [axis_text(a) for a in induced] + [hyp_text(h) for h in hypothesis.get("axes", [])]
    vz = tu.Vectorizer().fit(docs)
    # 第二通道：只比拉丁词。用户假设常用中文命名、配英文关键词，混排会让中文
    # 二元组变成"稀有但无法匹配"的维度，稀释余弦相似度。
    lat = tu.Vectorizer().fit([" ".join(tu.tokens_latin(d)) for d in docs])
    lat_axis = {a["axis_id"]: lat.vec(" ".join(tu.tokens_latin(axis_text(a)))) for a in induced}

    out = {"recalled": [], "missed": [], "extra": [], "recall_rate": 0.0}
    used = set()
    for h in hypothesis.get("axes", []):
        ht = hyp_text(h)
        hv = vz.vec(ht)
        hv_lat = lat.vec(" ".join(tu.tokens_latin(ht)))
        best, score = None, 0.0
        for a in induced:
            if a["axis_id"] in used:
                continue
            sc = max(tu.cosine(hv, vz.vec(axis_text(a))),
                     tu.cosine(hv_lat, lat_axis[a["axis_id"]]))
            if sc > score:
                best, score = a, sc
        if best is not None and score >= 0.25:
            out["recalled"].append({"hypothesis": h.get("name"), "axis_id": best["axis_id"],
                                    "induced": best["name"], "score": round(score, 3)})
            used.add(best["axis_id"])
        else:
            out["missed"].append({"hypothesis": h.get("name"), "best_score": round(score, 3)})
    for a in induced:
        if a["axis_id"] not in used:
            out["extra"].append({"axis_id": a["axis_id"], "name": a["name"],
                                 "papers": a["evidence_count"]})
    total = max(1, len(hypothesis.get("axes", [])))
    out["recall_rate"] = round(len(out["recalled"]) / total, 3)
    # 词面匹配会给出高分但语义错的配对（例如把"候选模板"配到"光滑指示器"）。
    # 把"强匹配"与"弱匹配"分开报，弱匹配必须人工核。
    STRONG = 0.35
    strong = [r for r in out["recalled"] if r["score"] >= STRONG]
    out["strong_recall_rate"] = round(len(strong) / total, 3)
    out["weak_mappings"] = [r for r in out["recalled"] if r["score"] < STRONG]
    out["note"] = "recall_rate 是词面匹配的查全率；strong_recall_rate 才是可信召回，weak_mappings 需人工判定。"
    return out


# ------------------------------------------------------------------- run

def run_all(store: Store, threshold: float = DEFAULT_CLUSTER_THRESHOLD,
            min_size: int = 3) -> dict:
    # 顺序很关键：先把新记录归到已有轴，剩下的才聚类成新轴。反过来会让每年新增
    # 的论文各自成簇，回放时表现为"每年冒出一个新轴"。
    r0 = assign_unassigned(store)
    r1 = form_candidate_axes(store, threshold, min_size)
    r2 = assign_unassigned(store)
    r3 = recompute_axis_stats(store)
    r4 = build_edges(store)
    r5 = update_combos(store)
    r6 = update_couplings(store)
    r7 = mine_clauses(store)
    return {"axes": r1, "assign": {**r2, "assigned_before_cluster": r0["assigned"]},
            "stats": r3, "edges": r4, "combos": r5, "couplings": r6, "clauses": r7}
