import json
import tempfile
import types
import unittest
from pathlib import Path

import scripts.live_promoter as live_promoter


class LivePromoterGateTests(unittest.TestCase):
    def setUp(self):
        self._orig_data_dir = live_promoter.DATA_DIR
        self._orig_repo_root = live_promoter.REPO_ROOT
        self._orig_gather = live_promoter.gather_run_stats
        self._orig_thresholds = (
            live_promoter.MIN_CLOSED,
            live_promoter.MIN_ROI_PCT,
            live_promoter.MIN_WR_PCT,
            live_promoter.MAX_BIG_WIN_SHARE,
            live_promoter.MAX_DRAWDOWN_PCT,
        )

    def tearDown(self):
        live_promoter.DATA_DIR = self._orig_data_dir
        live_promoter.REPO_ROOT = self._orig_repo_root
        live_promoter.gather_run_stats = self._orig_gather
        (
            live_promoter.MIN_CLOSED,
            live_promoter.MIN_ROI_PCT,
            live_promoter.MIN_WR_PCT,
            live_promoter.MAX_BIG_WIN_SHARE,
            live_promoter.MAX_DRAWDOWN_PCT,
        ) = self._orig_thresholds

    def _setup_run(self, tmp: str, name: str, pnls: list[float], equity_curve: list[float]) -> None:
        root = Path(tmp)
        live_promoter.REPO_ROOT = root
        live_promoter.DATA_DIR = root / "data"
        run_dir = live_promoter.DATA_DIR / "dry_runs" / name
        run_dir.mkdir(parents=True)
        journal = run_dir / "journal.jsonl"
        journal.write_text(
            "\n".join(
                json.dumps({"closed_at": "2026-05-23T00:00:00+00:00", "realized_pnl": pnl})
                for pnl in pnls
            )
        )
        curve = run_dir / "equity_curve.jsonl"
        curve.write_text("\n".join(json.dumps({"equity": equity}) for equity in equity_curve))
        profiles = root / "configs" / "profiles"
        profiles.mkdir(parents=True)
        (profiles / f"{name}.toml").write_text("[run]\nstarting_cash = 20.0\n")

    def test_evaluate_rejects_one_trade_wonder(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._setup_run(tmp, "candidate", [12.0, -1.0, -1.0, -1.0], [20.0, 31.0])
            live_promoter.gather_run_stats = lambda _base, _run: types.SimpleNamespace(
                closed_trades=30,
                roi_pct=50.0,
                win_rate_pct=70.0,
                equity=30.0,
            )
            self.assertIsNone(live_promoter._evaluate())

    def test_evaluate_accepts_balanced_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._setup_run(tmp, "candidate", [2.0, 2.0, 2.0, -1.0], [20.0, 22.0, 24.0])
            live_promoter.gather_run_stats = lambda _base, _run: types.SimpleNamespace(
                closed_trades=30,
                roi_pct=20.0,
                win_rate_pct=70.0,
                equity=24.0,
            )
            winner = live_promoter._evaluate()
            self.assertIsNotNone(winner)
            self.assertEqual(winner["profile"], "candidate")
            self.assertEqual(winner["realized_pnl"], 5.0)


if __name__ == "__main__":
    unittest.main()
