#!/usr/bin/env python3
"""核对协议篇的构造报文与状态转换，不连接交易服务。"""

from decimal import Decimal
import struct
import unittest


ITCH_A = bytes.fromhex(
    "41 0007 0000 0000000f4240 00000000000003e9 "
    "42 00000064 5445535420202020 000f4c04"
)


def apply_depth(book, cursor, first, last, bids=(), asks=()):
    if last <= cursor:
        return cursor
    if first > cursor + 1:
        raise ValueError("depth gap")
    for side, changes in (("bids", bids), ("asks", asks)):
        for price, quantity in changes:
            price, quantity = Decimal(price), Decimal(quantity)
            if quantity:
                book[side][price] = quantity
            else:
                book[side].pop(price, None)
    return last


def mbp_message(entries, end_of_event=False):
    # CME schema id=1, version=9, template 46.
    root = struct.pack("<QB2x", 123456789, 128 if end_of_event else 0)
    mbp = struct.pack("<HB", 32, len(entries))
    mbp += b"".join(struct.pack("<qiiIiBBB5x", *entry) for entry in entries)
    empty_mbo = struct.pack("<H5xB", 24, 0)
    size = 10 + len(root) + len(mbp) + len(empty_mbo)
    return struct.pack("<HHHHH", size, 11, 46, 1, 9) + root + mbp + empty_mbo


def decode_mbp(message):
    if len(message) < 10:
        raise ValueError("short header")
    size, root_size, template, schema, version = struct.unpack_from("<HHHHH", message)
    if size != len(message) or (template, schema, version) != (46, 1, 9):
        raise ValueError("invalid envelope")
    offset = 10 + root_size
    if offset + 3 > size:
        raise ValueError("short group header")
    stride, count = struct.unpack_from("<HB", message, offset)
    offset += 3
    if stride < 27 or offset + stride * count + 8 > size:
        raise ValueError("short entries")
    entries = [
        struct.unpack_from("<qiiIiBBB", message, offset + i * stride)
        for i in range(count)
    ]
    offset += stride * count
    mbo_stride = struct.unpack_from("<H", message, offset)[0]
    mbo_count = message[offset + 7]
    if offset + 8 + mbo_stride * mbo_count != size:
        raise ValueError("invalid second group")
    return entries, bool(message[18] & 128)


def apply_mbp(levels, entries, depth=4):
    for price, qty, _security, _seq, _orders, level, action, side in entries:
        assert side == ord("0")
        index = level - 1
        if action == 0:
            levels.insert(index, (price, qty))
            del levels[depth:]
        elif action == 1:
            assert levels[index][0] == price
            levels[index] = (price, qty)
        elif action == 2:
            del levels[index]
        else:
            raise ValueError("unsupported action")


class ArticleExamples(unittest.TestCase):
    def test_itch_wire_layout(self):
        self.assertEqual(len(ITCH_A), 36)
        self.assertEqual(ITCH_A[0:1], b"A")
        self.assertEqual(int.from_bytes(ITCH_A[1:3], "big"), 7)
        self.assertEqual(int.from_bytes(ITCH_A[5:11], "big"), 1_000_000)
        self.assertEqual(int.from_bytes(ITCH_A[11:19], "big"), 1001)
        self.assertEqual(ITCH_A[19:20], b"B")
        self.assertEqual(int.from_bytes(ITCH_A[20:24], "big"), 100)
        self.assertEqual(ITCH_A[24:32], b"TEST    ")
        self.assertEqual(Decimal(int.from_bytes(ITCH_A[32:36], "big")) / 10000,
                         Decimal("100.25"))

    def test_binance_absolute_quantity_and_gap(self):
        book = {"bids": {Decimal(100): Decimal(5), Decimal(99): Decimal(8)},
                "asks": {Decimal(101): Decimal(6), Decimal(102): Decimal(9)}}
        cursor = apply_depth(book, 100, 100, 102, [("100", "7")], [("100.5", "3")])
        self.assertEqual(book["bids"][Decimal(100)], 7)
        cursor = apply_depth(book, cursor, 103, 104, [("99", "0")], [("101", "4")])
        self.assertNotIn(Decimal(99), book["bids"])
        self.assertEqual(book["asks"][Decimal(101)], 4)
        before = {side: dict(values) for side, values in book.items()}
        self.assertEqual(apply_depth(book, cursor, 100, 102, [("100", "99")]), 104)
        self.assertEqual(book, before)
        with self.assertRaises(ValueError):
            apply_depth(book, cursor, 107, 108, [("100", "6")])
        self.assertEqual(book, before)

    def test_cme_lengths_offsets_and_event_boundary(self):
        first = (5_000_250_000_000, 6, 7, 51, 2, 1, 0, ord("0"))
        second = (5_000_000_000_000, 7, 7, 52, 2, 2, 1, ord("0"))
        message = mbp_message([first, second])
        self.assertEqual(len(message), 96)
        self.assertEqual(struct.unpack_from("<HHHHH", message), (96, 11, 46, 1, 9))
        self.assertEqual(struct.unpack_from("<HB", message, 21), (32, 2))
        self.assertEqual(struct.unpack_from("<q", message, 24)[0], first[0])
        self.assertEqual(struct.unpack_from("<q", message, 56)[0], second[0])
        self.assertEqual(struct.unpack_from("<H", message, 88)[0], 24)
        self.assertEqual(message[95], 0)
        packet = struct.pack("<IQ", 700, 123456790) + message
        self.assertEqual(len(packet), 108)
        entries, publish = decode_mbp(packet[12:])
        self.assertEqual(entries, [first, second])
        self.assertFalse(publish)
        levels = [(5_000_000_000_000, 10), (4_999_750_000_000, 12),
                  (4_999_500_000_000, 8), (4_999_250_000_000, 9)]
        apply_mbp(levels, entries)
        self.assertEqual(levels[:3], [(first[0], 6), (second[0], 7), (4_999_750_000_000, 12)])
        deletion = mbp_message([(2**63-1, 2**31-1, 7, 53, 2**31-1, 1, 2, ord("0"))], True)
        entries, publish = decode_mbp(deletion)
        apply_mbp(levels, entries)
        self.assertTrue(publish)
        self.assertEqual(levels, [(5_000_000_000_000, 7), (4_999_750_000_000, 12),
                                  (4_999_500_000_000, 8)])

    def test_truncated_cme_entry_is_rejected(self):
        entry = (5_000_250_000_000, 6, 7, 51, 2, 1, 0, ord("0"))
        message = bytearray(mbp_message([entry]))
        message[23] = 2  # 两条组计数，实际只有一条。
        with self.assertRaises(ValueError):
            decode_mbp(message)

    def test_itch_order_identity_and_aggregate(self):
        orders = {900: (Decimal("100"), 50)}
        orders[1001] = (Decimal("100.25"), 100)
        orders[1001] = (orders[1001][0], orders[1001][1] - 20)
        self.assertEqual(orders[1001][1], 80)
        orders[1001] = (orders[1001][0], orders[1001][1] - 30)
        self.assertEqual(orders[1001][1], 50)
        del orders[1001]
        orders[1002] = (Decimal("100"), 40)
        self.assertEqual(sum(q for p, q in orders.values() if p == 100), 90)
        del orders[1002]
        self.assertEqual(orders, {900: (Decimal("100"), 50)})

    def test_szse_fixed_layout(self):
        body = struct.pack(
            ">Hq3s8s4sqqcqc",
            1, 100, b"011", b"000001  ", b"102 ",
            186400, 10000, b"1", 20261019093000123, b"2",
        )
        self.assertEqual(len(body), 51)
        message = struct.pack(">II", 300192, len(body)) + body
        checksum = sum(message) % 256
        message += struct.pack(">I", checksum)
        self.assertEqual(len(message), 63)
        self.assertEqual(struct.unpack_from(">q", message, 33)[0], 186400)
        self.assertEqual(struct.unpack_from(">q", message, 41)[0], 10000)
        self.assertEqual(struct.unpack_from(">q", message, 50)[0], 20261019093000123)
        self.assertEqual(message[58:59], b"2")

    def test_fill_deduplication_with_cancel(self):
        fills = {}
        for trade_id, qty in [("T1", 4), ("T2", 2), ("T1", 4)]:
            fills.setdefault(trade_id, qty)
        self.assertEqual(sum(fills.values()), 6)
        canceled_remaining = 10 - sum(fills.values())
        self.assertEqual(canceled_remaining, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
