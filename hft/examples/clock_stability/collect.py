#!/usr/bin/env python3
"""Read-only Linux clock comparison; Python 3.8+, standard library only."""

import argparse
from contextlib import ExitStack
import csv
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import stat
import sys
import time


CLOCK_NAMES = {
    "realtime": "CLOCK_REALTIME",
    "monotonic": "CLOCK_MONOTONIC",
    "raw": "CLOCK_MONOTONIC_RAW",
    "tai": "CLOCK_TAI",
}
FIELDS = [
    "sample_index", "scheduled_monotonic_ns", "sample_monotonic_ns",
    "reference_before_ns", "target_ns", "reference_after_ns", "bracket_ns",
    "target_clock", "reference_clock",
]


def interval_ns(value):
    try:
        seconds = Decimal(value)
        if not seconds.is_finite() or seconds <= 0:
            raise ValueError
        result = int(seconds * 1_000_000_000)
        if result < 1:
            raise ValueError
        return result
    except (InvalidOperation, ValueError, OverflowError):
        raise argparse.ArgumentTypeError("interval must be finite and at least 1 ns")


def open_clock(name, stack):
    if name in CLOCK_NAMES:
        constant = CLOCK_NAMES[name]
        if not hasattr(time, constant):
            raise ValueError("Python/platform does not expose " + constant)
        clock_id = getattr(time, constant)
    elif re.fullmatch(r"/dev/ptp[0-9]+", name):
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        stack.callback(os.close, fd)
        if not stat.S_ISCHR(os.fstat(fd).st_mode):
            raise ValueError(name + " is not a character device")
        # Linux FD_TO_CLOCKID(fd); keep fd open throughout sampling.
        clock_id = ((~fd) << 3) | 3
    else:
        raise ValueError("clock must be realtime, monotonic, raw, tai, or /dev/ptpN")
    time.clock_gettime_ns(clock_id)  # Fail before sampling if unsupported.
    return clock_id


def sample(rows, target_id, reference_id, count, interval):
    read = time.clock_gettime_ns
    monotonic = time.monotonic_ns
    deadline = monotonic()
    for index in range(count):
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            time.sleep(remaining / 1_000_000_000)
        observed = monotonic()
        before = read(reference_id)
        target = read(target_id)
        after = read(reference_id)
        rows.append((index, deadline, observed, before, target, after, after - before))
        deadline += interval
        now = monotonic()
        # Skip missed schedule slots instead of producing catch-up bursts.
        if deadline <= now:
            deadline += ((now - deadline) // interval + 1) * interval


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="realtime")
    parser.add_argument("--reference", default="raw")
    parser.add_argument("--count", type=int, default=600)
    parser.add_argument("--interval", type=interval_ns, default=100_000_000,
                        metavar="SECONDS", help="sampling interval, default 0.1 s")
    parser.add_argument("--output", type=Path, required=True,
                        help="new CSV path; existing files are never overwritten")
    args = parser.parse_args(argv)
    if args.count < 2:
        parser.error("--count must be at least 2")
    if not sys.platform.startswith("linux"):
        parser.error("collection requires Linux; offline analysis is portable")
    rows = []
    failure = None
    try:
        with ExitStack() as stack:
            target_id = open_clock(args.target, stack)
            reference_id = open_clock(args.reference, stack)
            # Reserve output before a long run, but defer all CSV writes.
            output = stack.enter_context(args.output.open("x", newline="", encoding="utf-8"))
            try:
                sample(rows, target_id, reference_id, args.count, args.interval)
            except (KeyboardInterrupt, OSError) as exc:
                failure = "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
            writer = csv.writer(output)
            writer.writerow(FIELDS)
            for row in rows:
                writer.writerow((*row, args.target, args.reference))
    except (OSError, ValueError) as exc:
        parser.exit(1, "error: " + str(exc) + "\n")
    print("saved {} samples to {}".format(len(rows), args.output))
    if failure:
        print("partial recording: " + failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
