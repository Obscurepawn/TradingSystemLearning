#!/usr/bin/env python3
"""Check RFC 1624 incremental updates against a full IPv4 header checksum."""

import random
import struct


def checksum(data):
    """Independent full checksum, treating input as network-order bytes."""
    if len(data) % 2:
        data += b"\x00"
    total = sum(int.from_bytes(data[i:i + 2], "big")
                for i in range(0, len(data), 2))
    while total >> 16:
        total = (total & 0xffff) + (total >> 16)
    return (~total) & 0xffff


def replace_word(old_checksum, old_word, new_word):
    total = ((~old_checksum) & 0xffff) + ((~old_word) & 0xffff) + new_word
    total = (total & 0xffff) + (total >> 16)
    total = (total & 0xffff) + (total >> 16)
    return (~total) & 0xffff


def full(words):
    return checksum(struct.pack("!10H", *words))


def main():
    # IPv4 header with a zero checksum field; update the identification word.
    baseline = [0x4500, 60, 0x5555, 0x4000, 0x4011, 0,
                0xc000, 0x0201, 0xc633, 0x6401]
    original = full(baseline)
    for value in range(65536):
        changed = baseline.copy()
        changed[2] = value
        updated = replace_word(original, baseline[2], value)
        assert updated == full(changed), (value, updated, full(changed))
        changed[5] = updated
        assert full(changed) == 0

    rng = random.Random(1624)
    candidates = [1, 2, 3, 4, 6, 7, 8, 9]
    for _ in range(10000):
        words = baseline.copy()
        for index in candidates:
            words[index] = rng.randrange(65536)
        updated = full(words)
        for index in rng.sample(candidates, rng.randint(1, len(candidates))):
            value = rng.randrange(65536)
            updated = replace_word(updated, words[index], value)
            words[index] = value
        assert updated == full(words)

    # RFC 1624 Section 4: the positive-zero / negative-zero edge case.
    assert replace_word(0xdd2f, 0x5555, 0x3285) == 0x0000
    print("single-word replacements: 65536 passed")
    print("multi-word cases (seed=1624): 10000 passed")
    print("RFC 1624 zero-boundary case: passed")


if __name__ == "__main__":
    main()
