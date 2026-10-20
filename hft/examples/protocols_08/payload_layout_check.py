#!/usr/bin/env python3
"""仅用于核算文章里 FIX 4.4 与深交所 Binary demo payload 的字节长度、偏移与校验和，
不连接任何交易服务，也不作为交付测试。运行后把打印结果抄进文章。"""
import struct

SOH = "\x01"

# ---------- FIX 4.4 NewOrderSingle ----------
body_fields = [
    ("35", "D"),
    ("49", "BUYER"),
    ("56", "BROKER"),
    ("34", "7"),
    ("52", "20261020-09:30:00.123"),
    ("11", "ORD0001"),
    ("21", "1"),
    ("55", "AAPL"),
    ("54", "1"),
    ("60", "20261020-09:30:00.123"),
    ("40", "2"),
    ("38", "100"),
    ("44", "186.40"),
    ("59", "0"),
]
body = "".join(f"{t}={v}{SOH}" for t, v in body_fields)
body_len = len(body.encode("ascii"))
head = f"8=FIX.4.4{SOH}9={body_len}{SOH}"
chk_src = head + body
checksum = sum(chk_src.encode("ascii")) % 256
msg = chk_src + f"10={checksum:03d}{SOH}"
raw = msg.encode("ascii")
print("=== FIX 4.4 NewOrderSingle ===")
print("BodyLength =", body_len)
print("CheckSum   =", f"{checksum:03d}")
print("total bytes=", len(raw))
off = 0
for seg_name, seg in [("head", head), ("body", body), ("tail", f"10={checksum:03d}{SOH}")]:
    print(f"{name} region bytes [{off},{off+len(seg.encode())})" if False else "", end="")
    off += len(seg.encode())
# 逐字段偏移
print("-- field offsets --")
o = 0
for t, v in [("8", "FIX.4.4"), ("9", str(body_len))] + body_fields + [("10", f"{checksum:03d}")]:
    field = f"{t}={v}{SOH}"
    print(f"tag {t:>3} @offset {o:>3}  len {len(field.encode()):>2}  {field.strip(chr(1))!r}")
    o += len(field.encode())
print("-- SOH positions (hex view, SOH shown as |) --")
print(msg.replace(SOH, "|").strip("|"))

# ---------- 深交所 Binary 300192 逐笔委托 ----------
print("\n=== SZSE Binary 300192 逐笔委托 ===")
chan, appseq = 1, 100
mdstream = b"011"
secid = b"000001  "
secsrc = b"102 "
price = 186400      # N13(4) -> 18.6400
qty = 10000         # N15(2) -> 100.00
side = b"1"
trtime = 20261019093000123
ordtype = b"2"
body300192 = struct.pack(">Hq", chan, appseq) + mdstream + secid + secsrc + \
    struct.pack(">qq", price, qty) + side + struct.pack(">q", trtime) + ordtype
print("body len =", len(body300192))
msg300192 = struct.pack(">II", 300192, len(body300192)) + body300192
cks = sum(msg300192) % 256
msg300192 += struct.pack(">I", cks)
print("checksum =", cks, " total =", len(msg300192))
print("hex:", msg300192.hex(" "))
# 偏移表
rows = [
    ("MsgType", 0, 4, "uInt32 BE = 300192"),
    ("BodyLength", 4, 4, f"uInt32 BE = {len(body300192)}"),
    ("ChannelNo", 8, 2, f"uInt16 = {chan}"),
    ("ApplSeqNum", 10, 8, f"Int64 = {appseq}"),
    ("MDStreamID", 18, 3, "char[3] = 011"),
    ("SecurityID", 21, 8, "char[8] = '000001  '"),
    ("SecurityIDSource", 29, 4, "char[4] = '102 '"),
    ("Price", 33, 8, f"Int64 N13(4) = {price} -> 18.6400"),
    ("OrderQty", 41, 8, f"Int64 N15(2) = {qty} -> 100.00"),
    ("Side", 49, 1, "char = '1' 买"),
    ("TransactTime", 50, 8, f"Int64 = {trtime}"),
    ("OrdType(扩展)", 58, 1, "char = '2' 限价"),
    ("Checksum", 59, 4, f"uInt32 = {cks}"),
]
for n, a, l, d in rows:
    assert msg300192[a:a+l] or True
    print(f"[{a:>2}:{a+l:>2}] {n:<18}{d}")

# ---------- 深交所 Binary 300191 逐笔成交 ----------
print("\n=== SZSE Binary 300191 逐笔成交 ===")
bidseq, offerseq = 100, 96
lastpx, lastqty = 186400, 5000   # 18.6400, 50.00
exectype = b"F"
b = struct.pack(">Hq", chan, 101) + mdstream + struct.pack(">qq", bidseq, offerseq) + \
    secid + secsrc + struct.pack(">qq", lastpx, lastqty) + exectype + struct.pack(">q", trtime)
print("body len =", len(b))
full = struct.pack(">II", 300191, len(b)) + b
c2 = sum(full) % 256
full += struct.pack(">I", c2)
print("checksum =", c2, " total =", len(full))
print("hex:", full.hex(" "))
