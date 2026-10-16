#!/usr/bin/env python3
"""Analyze relative clock readings without turning epoch nanoseconds into floats."""

import argparse
import csv
import json
import math
from pathlib import Path
import sys


def half_string(twice_value):
    sign = "-" if twice_value < 0 else ""
    magnitude = abs(twice_value)
    return "{}{}.{}".format(sign, magnitude // 2, 5 if magnitude % 2 else 0)


def stats(values):
    ordered = sorted(values)
    def percentile(p):
        return ordered[max(0, math.ceil(p * len(ordered)) - 1)]
    return {"min": ordered[0], "p50": percentile(0.50),
            "p99": percentile(0.99), "max": ordered[-1]}


def load_rows(path):
    with path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    if len(rows) < 2:
        raise ValueError("at least two samples are required")
    integer_fields = ("sample_index", "reference_before_ns", "target_ns",
                      "reference_after_ns", "bracket_ns")
    for row in rows:
        for key in integer_fields:
            row[key] = int(row[key])
        if row["bracket_ns"] != row["reference_after_ns"] - row["reference_before_ns"]:
            raise ValueError("bracket does not match raw timestamps")
    if len({(row["target_clock"], row["reference_clock"]) for row in rows}) != 1:
        raise ValueError("input mixes clock pairs; analyze each recording separately")
    return rows


def analyze(rows, max_bracket_ns=None):
    if len(rows) < 2:
        raise ValueError("at least two samples are required")
    reversals = {
        "reference_within_bracket": sum(row["bracket_ns"] < 0 for row in rows),
        "reference_between_samples": sum(b["reference_before_ns"] < a["reference_after_ns"]
                                         for a, b in zip(rows, rows[1:])),
        "target_between_samples": sum(b["target_ns"] < a["target_ns"]
                                      for a, b in zip(rows, rows[1:])),
    }
    kept = [row for row in rows if row["bracket_ns"] >= 0 and
            (max_bracket_ns is None or row["bracket_ns"] <= max_bracket_ns)]
    if len(kept) < 2:
        raise ValueError("fewer than two samples remain after bracket filtering")
    # Twice the midpoint and offset preserve exact half-nanoseconds as integers.
    mid2 = [row["reference_before_ns"] + row["reference_after_ns"] for row in kept]
    offset2 = [2 * row["target_ns"] - mid for row, mid in zip(kept, mid2)]
    elapsed2 = [mid - mid2[0] for mid in mid2]
    change2 = [offset - offset2[0] for offset in offset2]
    # Subtract integer origins FIRST. Floating point only sees elapsed/change values.
    x = [elapsed / 2_000_000_000 for elapsed in elapsed2]
    y = [change / 2 for change in change2]
    warnings = [
        "Results describe the observed clock pair, not absolute UTC accuracy.",
        "No forward-step detector is implemented; a step or servo change can bias a whole-run fit.",
    ]
    summary = {
        "target_clock": rows[0]["target_clock"],
        "reference_clock": rows[0]["reference_clock"],
        "samples_total": len(rows), "samples_kept": len(kept),
        "samples_excluded": len(rows) - len(kept),
        "max_bracket_filter_ns": max_bracket_ns,
        "duration_reference_s": x[-1],
        "initial_target_minus_reference_ns_exact": half_string(offset2[0]),
        "final_offset_change_ns_exact": half_string(change2[-1]),
        "relative_offset_change_ns": stats(y),
        "bracket_all_nonnegative_ns": stats([r["bracket_ns"] for r in rows if r["bracket_ns"] >= 0]),
        "bracket_kept_ns": stats([r["bracket_ns"] for r in kept]),
        "observed_reversals": reversals,
        "relative_frequency_ppm": None,
        "fit_intercept_relative_ns": None,
        "residual_ns": None,
        "absolute_residual_ns": None,
        "warnings": warnings,
    }
    residuals = [None] * len(kept)
    if any(reversals.values()):
        warnings.append("Backward readings observed: fit suppressed; inspect raw data and split into continuous segments.")
    else:
        mean_x = math.fsum(x) / len(x)
        mean_y = math.fsum(y) / len(y)
        xx = math.fsum((value - mean_x) ** 2 for value in x)
        if xx == 0:
            raise ValueError("reference clock has no measurable elapsed span")
        slope = math.fsum((a - mean_x) * (b - mean_y) for a, b in zip(x, y)) / xx
        intercept = mean_y - slope * mean_x
        residuals = [b - (intercept + slope * a) for a, b in zip(x, y)]
        summary.update({
            "relative_frequency_ppm": slope / 1000,
            "fit_intercept_relative_ns": intercept,
            "residual_ns": stats(residuals),
            "absolute_residual_ns": stats([abs(value) for value in residuals]),
        })
    series = [(row["sample_index"], half_string(t), half_string(d), residual, row["bracket_ns"])
              for row, t, d, residual in zip(kept, elapsed2, change2, residuals)]
    return summary, series


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--max-bracket-ns", type=int,
                        help="optional observation-width filter; reports exclusions")
    parser.add_argument("--series", type=Path,
                        help="new CSV for retained offset changes and fit residuals")
    args = parser.parse_args(argv)
    if args.max_bracket_ns is not None and args.max_bracket_ns < 0:
        parser.error("--max-bracket-ns must be nonnegative")
    try:
        summary, series = analyze(load_rows(args.input), args.max_bracket_ns)
        if args.series:
            with args.series.open("x", newline="", encoding="utf-8") as output:
                writer = csv.writer(output)
                writer.writerow(["sample_index", "elapsed_reference_ns", "offset_from_first_ns",
                                 "fit_residual_ns", "bracket_ns"])
                writer.writerows(series)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, "error: " + str(exc) + "\n")
    print(json.dumps(summary, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
