#!/usr/bin/env python3
"""Synthetic tests validate arithmetic and CLI behavior, not clock hardware."""

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from analyze import analyze, half_string
from collect import FIELDS


HERE = Path(__file__).resolve().parent


def synthetic_rows(reference_epoch=1_800_000_000_000_000_000, target_epoch=None):
    if target_epoch is None:
        target_epoch = reference_epoch + 123
    rows = []
    # Known +10 ppm, 1 s spacing, and curvature orthogonal to the fitted line.
    # Residuals [2, -1, -2, -1, 2] ns have zero mean and zero linear trend.
    for i, noise in enumerate([2, -1, -2, -1, 2]):
        reference = reference_epoch + i * 1_000_000_000
        target = target_epoch + i * 1_000_010_000 + noise
        rows.append(dict(zip(FIELDS, [
            i, i * 1_000_000_000, i * 1_000_000_000,
            reference - 50, target, reference + 50, 100, "synthetic_target", "synthetic_reference",
        ])))
    return rows


class AnalysisTests(unittest.TestCase):
    def test_known_frequency_and_nanosecond_residuals(self):
        summary, series = analyze(synthetic_rows())
        self.assertAlmostEqual(summary["relative_frequency_ppm"], 10.0, places=10)
        self.assertEqual(summary["initial_target_minus_reference_ns_exact"], "125.0")
        self.assertEqual(summary["final_offset_change_ns_exact"], "40000.0")
        for row, expected in zip(series, [2, -1, -2, -1, 2]):
            self.assertAlmostEqual(row[3], expected, places=8)

    def test_independent_epoch_translation_preserves_fit(self):
        first, series_a = analyze(synthetic_rows(reference_epoch=0, target_epoch=0))
        second, series_b = analyze(synthetic_rows(reference_epoch=10**18, target_epoch=10**20))
        self.assertEqual(first["relative_frequency_ppm"], second["relative_frequency_ppm"])
        self.assertEqual(series_a, series_b)
        self.assertEqual(half_string(-1), "-0.5")

    def test_filter_counts_and_reverse_suppression(self):
        rows = synthetic_rows()
        rows[2]["reference_before_ns"] -= 1000
        rows[2]["reference_after_ns"] += 1000
        rows[2]["bracket_ns"] += 2000
        summary, _ = analyze(rows, max_bracket_ns=100)
        self.assertEqual(summary["samples_excluded"], 1)
        self.assertEqual(summary["bracket_all_nonnegative_ns"]["max"], 2100)
        rows[3]["target_ns"] = rows[2]["target_ns"] - 1
        summary, series = analyze(rows)
        self.assertEqual(summary["observed_reversals"]["target_between_samples"], 1)
        self.assertIsNone(summary["relative_frequency_ppm"])
        self.assertTrue(all(row[3] is None for row in series))

    def test_offline_cli_and_no_overwrite(self):
        with tempfile.TemporaryDirectory(prefix=".clock_test_", dir=HERE) as directory:
            raw = Path(directory) / "synthetic.csv"
            derived = Path(directory) / "derived.csv"
            with raw.open("w", newline="", encoding="utf-8") as output:
                writer = csv.DictWriter(output, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(synthetic_rows())
            command = [sys.executable, str(HERE / "analyze.py"), str(raw), "--series", str(derived)]
            result = subprocess.run(command, text=True, capture_output=True, check=True)
            self.assertAlmostEqual(json.loads(result.stdout)["relative_frequency_ppm"], 10.0)
            self.assertTrue(derived.exists())
            second = subprocess.run(command, text=True, capture_output=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("File exists", second.stderr)


if __name__ == "__main__":
    unittest.main()
