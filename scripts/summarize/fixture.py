#!/usr/bin/env python3
"""合成 WENO 语料，供测试与演示使用（不依赖真实论文）。"""
from __future__ import annotations

import json
from pathlib import Path

from . import evidence
from .db import Store

AXES = {
    "weights": {
        "targets": [
            "construction of nonlinear weights that approach the optimal linear weights in smooth regions",
            "nonlinear weights formulation that avoids accuracy loss at critical points",
            "nonlinear weights based on a global smoothness indicator to restore convergence order",
            "nonlinear weighting strategy that reduces dissipation near discontinuities",
            "blending of nonlinear weights with optimal linear weights at smooth stencils",
        ],
        "tech": ["weno-js", "weno-z"],
        "bench": ["critical point convergence", "shu-osher"],
    },
    "indicator": {
        "targets": [
            "smoothness indicator definition for each candidate stencil",
            "smoothness indicator that detects discontinuities inside a candidate stencil",
            "refined smoothness indicator for high order reconstruction at critical points",
            "smoothness measurement of the candidate stencil using undivided differences",
        ],
        "tech": ["js-beta", "global-tau"],
        "bench": ["critical point convergence", "square wave overshoot"],
    },
    "stencil": {
        "targets": [
            "candidate stencil selection among several sub-stencils for reconstruction",
            "stencil width and bias configuration for the candidate stencil",
            "compact candidate stencil arrangement for high order reconstruction",
        ],
        "tech": ["js-stencil", "teno-stencil"],
        "bench": ["shu-osher", "double mach reflection"],
    },
    "spectral": {
        "targets": [
            "dispersion and dissipation of the linear scheme measured by modified wavenumber",
            "spectral resolution of the optimal linear weights in smooth regions",
            "modified wavenumber behaviour of the reconstruction at high wavenumbers",
        ],
        "tech": ["optimal-linear", "drp"],
        "bench": ["adr curve", "long time wave propagation"],
    },
}

FIRST_AUTHORS = ["borges", "liu", "shu", "castro", "yamaleev", "henrick", "martin", "zhu"]


def make_corpus(root: Path) -> Store:
    st = Store(root)
    st.init()
    st.set_meta("domain", "weno")
    st.set_meta("core_question", "间断附近无振荡与光滑区高阶精度之间的矛盾")
    st.set_meta("shared_principles", "光滑区退化为最优线性格式，间断附近退化为选模板")
    st.set_meta("paradigm_scope", "有限差分/有限体积的 WENO 型重构")
    old = root / "fulltext"
    old.mkdir(exist_ok=True)

    n = 0
    for axis, spec in AXES.items():
        for i, target in enumerate(spec["targets"]):
            for j in range(2):                      # 每个目标两篇，造出组数
                n += 1
                year = 2012 + (n % 12) + (0 if axis != "stencil" else 4)
                pid = f"doi:10.example/{axis}{i}{j}"
                tech = spec["tech"][(i + j) % len(spec["tech"])]
                prev = spec["tech"][(i + j + 1) % len(spec["tech"])]
                quote = f"We modify the {target} to recover accuracy near discontinuities."
                text = (f"Abstract. We study high order reconstruction. {quote} "
                        f"The scheme is tested on {spec['bench'][0]} and compared with WENO-JS. "
                        f"Results show improved resolution of shock structures.")
                fp = old / f"{axis}{i}{j}.txt"
                fp.write_text(text, encoding="utf-8")
                evidence.upsert_paper(st, {
                    "paper_id": pid, "doi": f"10.example/{axis}{i}{j}",
                    "title": f"{target} study {i}{j}",
                    "year": year, "venue": "JCP",
                    "authors": [f"{FIRST_AUTHORS[n % len(FIRST_AUTHORS)].title()}, A."],
                    "abstract": text, "source": "fixture",
                    "fulltext_path": str(fp), "fulltext_chars": len(text),
                    "meta_level": "fulltext",
                })
                with st.papers() as p:
                    p.execute("""INSERT INTO contributions(paper_id,target_text,technique,method,claim,
                                  addr_technique,addr_problem,evidence,level,source,created_at)
                                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                              (pid, target, tech, f"modify {target}", "improved accuracy",
                               prev, "accuracy loss", "p.3 Eq.(12)", "fulltext", "fixture",
                               "2026-01-01T00:00:00"))
                    p.execute("""INSERT INTO practices(paper_id,axis_id,baselines,benchmarks,params,metrics,
                                  evidence,level,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                              (pid, "", json.dumps(["WENO-JS", "WENO-Z"]),
                               json.dumps(spec["bench"]), json.dumps(["epsilon"]),
                               json.dumps(["L1"]), "p.10 Tab.2", "fulltext", "2026-01-01T00:00:00"))
                    p.execute("""INSERT INTO critiques(paper_id,target_technique,target_text,problem,kind,
                                  evidence,source,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                              (pid, prev, target, "third order at critical points",
                               "critique", "p.2", "fixture", "2026-01-01T00:00:00"))
    return st


