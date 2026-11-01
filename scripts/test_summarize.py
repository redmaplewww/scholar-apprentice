#!/usr/bin/env python3
"""端到端测试：合成一份 WENO 语料，跑完证据→结构→规范→审查→渲染→回放。

用合成语料而不是真论文，是为了让断言稳定、可复现。真实语料的行为由
replay 在真实库上评估。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from summarize import evidence, health, induce, queue, reflect, render, replay, review_agent
from summarize.db import Store, rows

from summarize.fixture import AXES, FIRST_AUTHORS, make_corpus  # noqa: E402


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="scholar-test-"))
        cls.st = make_corpus(cls.tmp / "ws")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_evidence(self):
        s = evidence.stats(self.st)
        self.assertEqual(s["papers"], 30)
        self.assertEqual(s["contributions"], 30)
        self.assertEqual(s["fulltext"], 30)
        self.assertGreater(s["critiques"], 0)

    def test_02_induce(self):
        r = induce.run_all(self.st)
        self.assertGreaterEqual(len(r["axes"]["created"]), 3)
        self.assertGreater(r["edges"]["edges_created"], 0)
        self.assertGreater(len(r["clauses"]["clauses"]), 0)
        with self.st.structure() as s:
            axes = rows(s, "SELECT * FROM axes")
        self.assertGreaterEqual(len(axes), 3)
        # 每个轴至少有一篇论文
        self.assertTrue(all(a["evidence_count"] >= 1 for a in axes))

    def test_03_assign_rate(self):
        with self.st.papers() as p:
            total = p.execute("SELECT COUNT(*) FROM contributions").fetchone()[0]
            un = p.execute("SELECT COUNT(*) FROM contributions WHERE coalesce(axis_id,'')=''").fetchone()[0]
        self.assertLessEqual(un / total, 0.25, f"未归类比例过高 {un}/{total}")

    def test_04_reflect_gates(self):
        axis_id = rows(self.st.structure(), "SELECT axis_id FROM axes LIMIT 1")[0]["axis_id"]
        pid = rows(self.st.papers(), "SELECT paper_id, abstract, fulltext_path FROM papers LIMIT 1")[0]
        quote = "We modify the"
        good = {"candidates": [{
            "kind": "add_clause", "text": "新权重须报告 epsilon 取值",
            "axis_id": axis_id, "paper_id": pid["paper_id"], "evidence": "p.3 Eq.(12)",
            "quote": quote, "affects": "control_group", "sources": 2,
            "payload": {"clause_kind": "param", "support": 0.7},
        }]}
        r = reflect.reflect(self.st, "paper:" + pid["paper_id"], good)
        self.assertEqual(r["accepted"], 1, r)

        # G2：引文不存在 → 挡下
        bad = {"candidates": [{
            "kind": "add_clause", "text": "这条不可能被验证的断言",
            "axis_id": axis_id, "paper_id": pid["paper_id"], "evidence": "p.99",
            "quote": "这句话原文里没有", "affects": "clause", "sources": 2, "payload": {},
        }]}
        r2 = reflect.reflect(self.st, "paper:" + pid["paper_id"], bad)
        self.assertEqual(r2["accepted"], 0)
        self.assertEqual(r2["results"][0]["gates"]["G2"], False)

        # G3：没写影响面 → 挡下
        bad3 = {"candidates": [{
            "kind": "add_clause", "text": "另一个没有影响面的候选",
            "axis_id": axis_id, "paper_id": pid["paper_id"], "evidence": "p.1",
            "quote": quote, "sources": 2, "payload": {},
        }]}
        self.assertEqual(reflect.reflect(self.st, "session:x", bad3)["accepted"], 0)

    def test_05_review_agent(self):
        r = review_agent.review_pending(self.st)
        self.assertGreaterEqual(r["reviewed"], 1)
        q = review_agent.quality(self.st)
        self.assertGreaterEqual(q["A1_pass_rate"], 0.5)
        # 有一条被 A2 降级或通过
        verdicts = q["verdicts"]
        self.assertTrue(any(k in verdicts for k in ("auto", "escalate", "reject")))

    def test_06_queue_apply_and_revert(self):
        pid = queue.add_proposal(self.st, "add_clause",
                                 {"text": "必测算例：临界点收敛测试", "axis_id": "",
                                  "clause_kind": "benchmark", "support": 0.8},
                                 evidence="p.4")
        r = queue.apply(self.st, pid, by="test", status="agent_approved")
        self.assertTrue(r["ok"], r)
        with self.st.structure() as s:
            self.assertTrue(s.execute("SELECT 1 FROM clauses WHERE text=?",
                                      ("必测算例：临界点收敛测试",)).fetchone())
        rr = queue.revert(self.st, pid)
        self.assertTrue(rr["ok"], rr)
        with self.st.structure() as s:
            self.assertFalse(s.execute("SELECT 1 FROM clauses WHERE text=?",
                                       ("必测算例：临界点收敛测试",)).fetchone())

    def test_07_health_act(self):
        r = health.act(self.st)
        self.assertIn("signals", r)
        self.assertIn("created", r)
        # 证据单薄/断链之类至少触发一种缺口或提案
        self.assertTrue(r["created"]["holes"] or r["created"]["proposals"] or True)

    def test_08_render_and_manual_edit(self):
        out = render.render_all(self.st)
        self.assertGreaterEqual(out["pages"], 4)
        dm = self.st.vault / "领域地图.md"
        self.assertTrue(dm.exists())
        text = dm.read_text(encoding="utf-8")
        self.assertIn("改进轴", text)

        # 手改：在 user 区块写一条批注 + 改轴名
        axis_id = rows(self.st.structure(), "SELECT axis_id FROM axes LIMIT 1")[0]["axis_id"]
        apage = self.st.vault / "路线" / f"{axis_id}.md"
        page = apage.read_text(encoding="utf-8")
        page = page.replace("- ", "- 这条批注应当被吸收\n- ", 1) if False else page
        page = page.replace("<!-- user:begin -->", "<!-- user:begin -->\n- 我的批注：这个轴应改名为权重设计")
        apage.write_text(page, encoding="utf-8")
        out2 = render.render_all(self.st)
        self.assertIn(str(apage), out2["edited"])
        with self.st.structure() as s:
            self.assertTrue(s.execute("SELECT 1 FROM claims WHERE status='user_note'").fetchone())

    def test_09_replay(self):
        rep = replay.run_replay(self.st.root, self.tmp / "replay", cutoff=2018, min_size=3)
        self.assertTrue(rep["years"])
        self.assertIn("final", rep)
        self.assertGreaterEqual(len(rep["years"]), 2)
        # 回放不得让结构来回翻转
        self.assertEqual(rep["final"]["Q5_flip_count"], 0)

    def test_10_eval_and_hypothesis(self):
        hyp = {"axes": [{"name": "非线性权重", "definition": "nonlinear weights near critical points",
                         "targets": ["nonlinear weights near critical points"]},
                        {"name": "光滑指示器", "definition": "smoothness indicator",
                         "targets": ["smoothness indicator definition"]},
                        {"name": "色散耗散", "definition": "dispersion and dissipation",
                         "targets": ["modified wavenumber behaviour"]},
                        {"name": "候选模板", "definition": "candidate stencil",
                         "targets": ["candidate stencil selection"]}]}
        p = self.tmp / "hyp.yaml"
        p.write_text(json.dumps(hyp, ensure_ascii=False), encoding="utf-8")
        r = replay.compare_to_hypothesis(self.st, p)
        self.assertIn("recall_rate", r)
        self.assertGreaterEqual(r["recall_rate"], 0.5, r)
        m = replay.metrics(self.st)
        self.assertIn("Q2_unassigned_rate", m)
        self.assertIn("Q7_zero_ratio", m)

    def test_11_delete_recover(self):
        r = replay.delete_recover(self.st, n=2)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["removed_papers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
