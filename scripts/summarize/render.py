#!/usr/bin/env python3
"""渲染：所有页面都是视图，真相在库里。

页面可以直接改。渲染前比对内容哈希，发现手改就：
  1) 备份到 vault/用户编辑备份/
  2) 解析 <!-- user:begin --> ... <!-- user:end --> 区块，原样保留到新页面
  3) 区块里以 "- " 开头的行记为 user 来源的批注（claims.status='user_note'）
  4) 轴页面 frontmatter 的 name/definition 被改 → 直接生效并钉住
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .db import Store, js, now, rows

USER_BEGIN, USER_END = "<!-- user:begin -->", "<!-- user:end -->"
USER_BLOCK = re.compile(re.escape(USER_BEGIN) + r"(.*?)" + re.escape(USER_END), re.S)
FM = re.compile(r"^---\n(.*?)\n---\n", re.S)

USER_HINT = (
    "<!-- 在下面的 user 区块里写什么都行：轴改名/改定义直接生效并钉住，"
    "以 \"- \" 开头的行会记为你的批注（source=user）。区块外的内容是渲染视图，会被覆盖。 -->\n"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _write(store: Store, path: Path, content: str) -> dict:
    """写入前处理手改。返回 {"edited":bool,...}"""
    meta = {}
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    with store.structure() as s:
        rec = s.execute("SELECT hash FROM page_hashes WHERE path=?", (str(path),)).fetchone()
    prev_hash = rec["hash"] if rec else None
    user_text = ""
    edited = False
    if existing and prev_hash and _sha(existing) != prev_hash:
        edited = True
        m = USER_BLOCK.search(existing)
        user_text = m.group(1).strip() if m else ""
        bak = store.vault / "用户编辑备份"
        bak.mkdir(parents=True, exist_ok=True)
        (bak / f"{path.stem}.{time.strftime('%Y%m%d-%H%M%S')}.md").write_text(existing, encoding="utf-8")
        _absorb_user_edit(store, path, existing, user_text)
    elif existing:
        m = USER_BLOCK.search(existing)
        user_text = m.group(1).strip() if m else ""

    body = content
    block = f"{USER_BEGIN}\n{user_text}\n{USER_END}\n"
    if USER_BLOCK.search(body):
        body = USER_BLOCK.sub(lambda _m: block, body)
    else:
        body = body.rstrip() + "\n\n## 我的批注\n\n" + USER_HINT + block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    with store.structure() as s:
        s.execute("INSERT INTO page_hashes(path,hash,rendered_at,edited) VALUES(?,?,?,?) "
                  "ON CONFLICT(path) DO UPDATE SET hash=excluded.hash, rendered_at=excluded.rendered_at, "
                  "edited=CASE WHEN excluded.edited=1 THEN 1 ELSE page_hashes.edited END",
                  (str(path), _sha(body), now(), 1 if edited else 0))
    return {"path": str(path), "edited": edited}


def _absorb_user_edit(store: Store, path: Path, content: str, user_text: str) -> None:
    # 轴页面：frontmatter 改名 / 改定义
    if path.name.startswith("axis-"):
        m = FM.search(content)
        if m:
            fm = dict(
                line.split(":", 1) for line in m.group(1).splitlines() if ":" in line
            )
            aid = path.stem
            name = (fm.get("name") or "").strip()
            definition = (fm.get("definition") or "").strip()
            before = None
            if name or definition:
                with store.structure() as s:
                    row = s.execute("SELECT * FROM axes WHERE axis_id=?", (aid,)).fetchone()
                    if row:
                        before = dict(row)
                        s.execute("UPDATE axes SET name=?,definition=?,pinned=1,last_change=? WHERE axis_id=?",
                                  (name or row["name"], definition or row["definition"], now(), aid))
                if before:
                    store.log_change("user_redefine", aid, before,
                                     {"name": name, "definition": definition}, "")
    for line in (user_text or "").splitlines():
        line = line.strip()
        if line.startswith("- ") and len(line) > 3:
            with store.structure() as s:
                s.execute("INSERT INTO claims(statement,status,support,counter,axis_id,created_at) "
                          "VALUES(?, 'user_note', 'user', '', ?, ?)",
                          (line[2:].strip(), path.stem if path.name.startswith("axis-") else "", now()))


# ------------------------------------------------------------------ pages

def _axis_rows(store: Store) -> list[dict]:
    with store.structure() as s:
        return [dict(r) for r in rows(s, "SELECT * FROM axes WHERE status!='deprecated' ORDER BY axis_id")]


def _clauses(store: Store, kind: str | None = None, axis_id: str | None = None,
             status: str = "active") -> list[dict]:
    q, args = "SELECT * FROM clauses WHERE status=?", [status]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if axis_id is not None:
        q += " AND coalesce(axis_id,'')=?"
        args.append(axis_id)
    with store.structure() as s:
        return [dict(r) for r in rows(s, q, args)]


def domain_map(store: Store) -> str:
    axes = _axis_rows(store)
    with store.structure() as s:
        couplings = [dict(r) for r in rows(s, "SELECT * FROM couplings ORDER BY ratio DESC")]
        edges = [dict(r) for r in rows(s, "SELECT * FROM edges")]
        claims = [dict(r) for r in rows(s, "SELECT * FROM claims WHERE status IN ('consensus','contested')")]
    core = store.meta("core_question", "（待归纳：这个领域要解决的核心矛盾是什么）")
    principle = store.meta("shared_principles", "（待归纳：跨轴共享的设计原则）")
    L = ["---", f"domain: {store.meta('domain', store.root.name)}",
         f"skeleton_version: {store.meta('skeleton_version', '0.1')}",
         f"generated: {now()}", "---", "", "# 领域地图", "", "> 本页由 `structure.db` 渲染，不要在这里写内容；要写请用页面末尾的 user 区块。", "",
         "## 核心问题", "", core, "", "## 核心思路（跨轴共享）", "", principle, "",
         "## 改进轴", "", "| 轴 | 定义 | 论文 | 组数 | 状态 | 路线页 |", "|---|---|---:|---:|---|---|"]
    for a in axes:
        L.append(f"| {a['name']} | {(a['definition'] or '').replace('|', '/')[:90]} | "
                 f"{a['evidence_count'] or 0} | {a['group_count'] or 0} | {a['status']} | "
                 f"[[{a['axis_id']}]] |")
    L += ["", "## 轴间耦合", ""]
    if couplings:
        L += ["| A | B | 共变率 |", "|---|---|---:|"]
        for c in couplings:
            L.append(f"| {c['a']} | {c['b']} | {c['ratio']} |")
    else:
        L.append("（暂无，共现率 <30% 的组合不记）")
    L += ["", "## 范式边界", "", f"本骨架覆盖：{store.meta('paradigm_scope', '（待填）')}", ""]
    para = [a for a in axes if a["name"].startswith("范式：")]
    if para:
        L.append("平行范式候选：" + "、".join(f"[[{a['axis_id']}]]" for a in para))
    L += ["", "## 全局评测（所有工作都要过）", ""]
    glob = _clauses(store, "benchmark", axis_id="")
    L += [f"- {c['text']}（支持率 {c['support']}）" for c in glob] or ["（待挖掘）"]
    L += ["", "## 共识与争议", ""]
    if claims:
        for c in claims:
            L.append(f"- [{c['status']}] {c['statement']}")
    else:
        L.append("（暂无）")
    return "\n".join(L) + "\n"


def axis_page(store: Store, axis: dict) -> str:
    aid = axis["axis_id"]
    with store.structure() as s:
        edges = [dict(r) for r in rows(s, "SELECT * FROM edges WHERE coalesce(axis_id,'')=? OR src=? OR dst=?",
                                       (aid, axis["name"], axis["name"]))]
        holes = [dict(r) for r in rows(s, "SELECT * FROM holes WHERE axis_id=? AND status IN ('open','running')", (aid,))]
        cou = [dict(r) for r in rows(s, "SELECT * FROM couplings WHERE a=? OR b=?", (aid, aid))]
    with store.papers() as p:
        members = [dict(r) for r in rows(
            p, """SELECT p.paper_id,p.title,p.year,p.venue,COUNT(c.id) n
                  FROM contributions c JOIN papers p ON p.paper_id=c.paper_id
                  WHERE c.axis_id=? GROUP BY p.paper_id ORDER BY n DESC, p.year DESC LIMIT 20""", (aid,))]
    L = ["---", f"axis_id: {aid}", f"name: {axis['name']}",
         f"definition: {(axis['definition'] or '').replace(chr(10), ' ')}",
         f"status: {axis['status']}", f"papers: {axis['evidence_count'] or 0}",
         f"groups: {axis['group_count'] or 0}", "---", "",
         f"# 改进轴：{axis['name']}", "",
         "> 改本页的 name / definition 会直接生效并钉住该轴；其余部分每次渲染覆盖。", "",
         "## 定义", "", axis["definition"] or "（待补）", "",
         "## 核心矛盾", "", axis["tension"] or "（待归纳：这个轴的基本取舍是什么）", "",
         "## 演化链", ""]
    if edges:
        L += ["| 从 | 到 | 解决的问题 | 证据 |", "|---|---|---|---|"]
        for e in edges:
            L.append(f"| {e['src']} | {e['dst']} | {(e['problem'] or '').replace('|','/')} | {e['evidence'] or ''} |")
    else:
        L.append("（暂无演化边：需要贡献记录里填写 addresses 与 technique）")
    L += ["", "## 标准评测与基线", ""]
    for kind, label in (("baseline", "基线"), ("benchmark", "必测算例"), ("param", "必报参数")):
        cs = _clauses(store, kind, axis_id=aid)
        if cs:
            L.append(f"**{label}**")
            L += [f"- {c['text']}（支持率 {c['support']}）" for c in cs]
            L.append("")
    L += ["## 与其他轴的组合", ""]
    L += [f"- 与 {c['a'] if c['b']==aid else c['b']} 共变率 {c['ratio']}" for c in cou] or ["（暂无）"]
    L += ["", "## 开放问题", ""]
    L += [f"- [{h['kind']}] {h['desc']}：{h['action']}" for h in holes] or ["（暂无）"]
    L += ["", "## 成员论文（按贡献数）", "", "| 年份 | 论文 | 贡献数 |", "|---|---|---:|"]
    for m in members:
        L.append(f"| {m['year']} | {m['title'][:80]} | {m['n']} |")
    return "\n".join(L) + "\n"


def sop_page(store: Store) -> str:
    axes = _axis_rows(store)
    L = ["---", f"domain: {store.meta('domain', store.root.name)}",
         f"sop_version: {store.meta('sop_version', '0.1')}", f"generated: {now()}", "---", "",
         "# 领域 SOP", "", "> 六步框架与领域无关，每步的内容来自条款挖掘。", ""]
    L += ["## ① 定位", "", "先判断你的改动落在哪个轴上：", ""]
    L += [f"- {a['name']}（{a['axis_id']}，{a['evidence_count'] or 0} 篇）" for a in axes] or ["- （轴尚未归纳）"]
    L += ["", "再判断是单轴改进、跨轴组合，还是新轴。跨轴组合先查领域地图的耦合与组合表。", "",
          "## ② 查路线", "", "看目标轴的演化链走到哪一环，上一环留下了什么开放问题：", ""]
    L += [f"- [[{a['axis_id']}]] {a['name']}" for a in axes]
    L += ["", "## ③ 定对照组", ""]
    for kind, label in (("baseline", "基线"), ("benchmark", "必测算例"), ("param", "必报参数")):
        cs = _clauses(store, kind)
        if cs:
            L.append(f"**{label}**")
            L += [f"- {c['text']}（支持率 {c['support']}，来源 {c['axis_id'] or '全局'}）" for c in cs]
            L.append("")
    L += ["## ④ 定评测", "", "除上面的必测项，还要过全局评测（见领域地图「全局评测」）。", "",
          "## ⑤ 查红线", ""]
    red = _clauses(store, "redline")
    L += [f"- {c['text']}（依据：{c['evidence'][:120] if c['evidence'] else '—'}）" for c in red] or ["（暂无规范性条款）"]
    L += ["", "## ⑥ 写贡献", "",
          "按「在 X 轴上，针对上一环 Y 的问题 Z，提出 W，在 B 上相对基线 C 提升 D」表述；"
          "断言强度不得超过证据规模（单势/单算例不得写普适）。", ""]
    return "\n".join(L) + "\n"


def workbench(store: Store) -> str:
    with store.structure() as s:
        esc = [dict(r) for r in rows(s, "SELECT * FROM proposals WHERE status IN ('escalated','needs_source') "
                                        "ORDER BY created_at DESC LIMIT 50")]
        auto = [dict(r) for r in rows(s, "SELECT * FROM proposals WHERE status='agent_approved' "
                                         "ORDER BY decided_at DESC LIMIT 30")]
        un = [dict(r) for r in rows(s, "SELECT * FROM unadopted ORDER BY id DESC LIMIT 20")]
        holes = [dict(r) for r in rows(s, "SELECT * FROM holes WHERE status IN ('open','running') "
                                          "ORDER BY created_at DESC")]
        refl = s.execute("SELECT COUNT(*) t, SUM(CASE WHEN candidates=0 THEN 1 ELSE 0 END) z FROM reflections").fetchone()
    from .review_agent import quality
    from .queue import queue_stats
    q = quality(store)
    qs = queue_stats(store)
    L = ["---", f"generated: {now()}", "---", "", "# 工作台", "",
         f"- 升级待你判断：**{len(esc)}** 条；自动入库（可撤销）：{len(auto)} 条",
         f"- 复盘单元 {refl['t'] or 0} 个，其中零沉淀 {refl['z'] or 0} 个",
         f"- 审查：A1 通过率 {q.get('A1_pass_rate', '—')}，升级率 {q.get('escalation_rate', '—')}，驳回率 {q.get('reject_rate', '—')}",
         "", "## 待你判断", ""]
    if esc:
        for p in esc:
            checks = json.loads(p["agent_checks"] or "{}")
            why = "；".join(f"{k}:{v['result']}" for k, v in checks.items())
            L += [f"### `{p['proposal_id']}` · {p['kind']} · {p['status']}",
                  f"- 理由：{p['reason'] or '—'}",
                  f"- 检查：{why or '—'}",
                  f"- 证据：{(p['evidence'] or '—')[:200]}",
                  f"- 批准：`scholar.py queue approve {p['proposal_id']}` ／ 驳回：`scholar.py queue reject {p['proposal_id']} --reason \"...\"`", ""]
    else:
        L += ["（没有待判断项）", ""]
    L += ["## 自动入库（可撤销）", ""]
    if auto:
        for p in auto[:15]:
            pl = json.loads(p["payload"] or "{}")
            L.append(f"- `{p['proposal_id']}` {p['kind']}：{(pl.get('text') or pl.get('src','') or '')[:70]}"
                     f"（撤销：`scholar.py queue revert {p['proposal_id']}`）")
    else:
        L.append("（暂无）")
    L += ["", "## 未采纳（被沉淀门挡下）", ""]
    if un:
        for u in un[:10]:
            L.append(f"- [{u['failed_gate']}] {u['reason'][:100]}")
    else:
        L.append("（暂无）")
    L += ["", "## 缺口单", ""]
    if holes:
        for h in holes:
            L.append(f"- `{h['hole_id']}` [{h['kind']}] {h['desc']} → {h['action']}（关闭条件：{h['close_condition']}）")
    else:
        L.append("（暂无）")
    return "\n".join(L) + "\n"


def render_all(store: Store) -> dict:
    out = []
    out.append(_write(store, store.vault / "领域地图.md", domain_map(store)))
    for a in _axis_rows(store):
        if a["status"] in ("candidate", "stable", "declining"):
            out.append(_write(store, store.vault / "路线" / f"{a['axis_id']}.md", axis_page(store, a)))
    out.append(_write(store, store.vault / "领域SOP.md", sop_page(store)))
    out.append(_write(store, store.vault / "工作台.md", workbench(store)))
    return {"pages": len(out), "edited": [o["path"] for o in out if o["edited"]], "detail": out}
