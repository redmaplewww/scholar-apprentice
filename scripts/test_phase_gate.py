"""Regression test for phase_gate: locks the 12 material x claim combinations and the
should-I / birth-cert behaviour. Run: python -m unittest -v test_phase_gate.py"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GATE = Path(__file__).resolve().parent / "phase_gate.py"

LEDGER = {"instruments": {"inst-a": {
    "status": "partial", "version": "1",
    "materials": {"mat1": {"level": "quantitative", "note": ""},
                  "mat2": {"level": "qualitative", "note": "reason"},
                  "mat3": {"level": "screening", "note": ""}},
    "forbidden_claims": []}}}


def run(*args, cwd=None):
    r = subprocess.run([sys.executable, str(GATE), *args], capture_output=True, text=True, cwd=cwd)
    return r.returncode, r.stdout


class TestPhaseGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name) / "研究档案"
        d.mkdir(parents=True)
        (d / "instruments.json").write_text(json.dumps(LEDGER), encoding="utf-8")
        cls.ws = cls.tmp.name

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_coverage_matrix(self):
        cases = [("mat1", "quantitative", 0), ("mat1", "qualitative", 0),
                 ("mat2", "quantitative", 1), ("mat2", "qualitative", 0),
                 ("mat3", "qualitative", 1), ("mat3", "screening", 0),
                 ("unknown", "screening", 2)]
        for mat, claim, want in cases:
            with self.subTest(mat=mat, claim=claim):
                rc, out = run("check", "--ws", self.ws, "--instrument", "inst-a",
                              "--material", mat, "--claim", claim)
                self.assertEqual(rc, want, out)

    def test_unknown_instrument(self):
        rc, _ = run("check", "--ws", self.ws, "--instrument", "nope",
                    "--material", "mat1", "--claim", "qualitative")
        self.assertEqual(rc, 2)

    def test_should_i_threshold(self):
        rc, _ = run("should-I", "--self-generated")
        self.assertEqual(rc, 1, "one criterion must not force the stage")
        rc, _ = run("should-I", "--self-generated", "--conventions")
        self.assertEqual(rc, 0, "two criteria must force the stage")
        rc, _ = run("should-I", "--self-generated", "--new-pipeline", "--not-covered",
                    "--external", "--conventions", "--multi-setting")
        self.assertEqual(rc, 0)

    def test_birth_cert(self):
        base = ["birth-cert", "--instrument", "i", "--version", "1", "--status", "partial"]
        self.assertEqual(run(*base, "--scope", "s", "--in-scope", "yes")[0], 0)
        self.assertEqual(run(*base, "--in-scope", "yes")[0], 1, "missing scope must fail")
        self.assertEqual(run(*base, "--scope", "s", "--in-scope", "no")[0], 1)

    def test_draft_instrument_blocks_everything(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "研究档案"
            d.mkdir(parents=True)
            (d / "instruments.json").write_text(json.dumps(
                {"instruments": {"d": {"status": "draft",
                                       "materials": {"m": {"level": "quantitative"}}}}}), encoding="utf-8")
            rc, _ = run("check", "--ws", t, "--instrument", "d", "--material", "m",
                        "--claim", "screening")
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
