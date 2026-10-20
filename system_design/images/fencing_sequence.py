"""生成 fencing_sequence.svg：Position Engine 主备切换时，Kafka 事务的 producer epoch 如何隔离旧主实例。

用法：python3 fencing_sequence.py
"""
from html import escape
from pathlib import Path

W, H = 1000, 860
FONT = "-apple-system, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif"
COLORS = {"gray": "#475569", "blue": "#2563EB", "red": "#DC2626", "orange": "#D97706",
          "green": "#16A34A", "muted": "#94A3B8"}
out = []
add = out.append


def text_width(s, size):
    return sum(size if ord(c) > 0x2E80 else size * 0.58 for c in s)


def text(x, y, s, size=12.5, color="#1F2937", weight="normal", anchor="middle", bg=False):
    if bg:
        w = text_width(s, size) + 8
        left = {"middle": x - w / 2, "start": x - 4, "end": x - w + 4}[anchor]
        add(f'<rect x="{left:.1f}" y="{y - size / 2 - 3:.1f}" width="{w:.1f}" height="{size + 6}" fill="#FFFFFF"/>')
    add(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" '
        f'text-anchor="{anchor}" dominant-baseline="middle">{escape(s)}</text>')


def lane(cx, w, title, sub, fill, stroke, top=74):
    add(f'<rect x="{cx - w / 2}" y="{top}" width="{w}" height="50" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
    text(cx, top + 19, title, size=14, color="#111827", weight="600")
    text(cx, top + 38, sub, size=12, color="#4B5563")


def msg(x1, x2, y, label, color="gray", dashed=False, bold=False):
    c = COLORS[color]
    dx = 3 if x2 > x1 else -3
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    add(f'<line x1="{x1 + dx}" y1="{y}" x2="{x2 - dx}" y2="{y}" stroke="{c}" stroke-width="1.6"{dash} marker-end="url(#m-{color})"/>')
    text((x1 + x2) / 2, y - 11, label, color=COLORS["red"] if bold else "#1F2937",
         weight="600" if bold else "normal", bg=True)


def note(cx, y, w, s, fill="#FFF7ED", stroke="#FDBA74"):
    add(f'<rect x="{cx - w / 2}" y="{y - 15}" width="{w}" height="30" rx="6" fill="{fill}" stroke="{stroke}"/>')
    text(cx, y, s, size=12)


add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">')
add('<title>Position Engine fencing 时序：standby B 接管分区 17 后，旧主实例 A 的提交被拒绝</title>')
add('<defs>' + "".join(
    f'<marker id="m-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
    f'<path d="M0 0L10 5L0 10z" fill="{v}"/></marker>' for k, v in COLORS.items()) + '</defs>')
add(f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>')

A, B, DDB, TC, PU, DS = 95, 245, 395, 555, 720, 900
OY = 60  # 顶部说明区高度

text(W / 2, 22, "Position Engine 分区 17 的主备切换", size=15, color="#111827", weight="600")
text(W / 2, 44, "A、B 是负责同一个分区的两个引擎实例，共用 transactional.id = pos-engine-17", size=12.5, color="#475569")
add(f'<rect x="16" y="{OY + 6}" width="308" height="66" rx="10" fill="none" stroke="#2563EB" stroke-width="1.4" stroke-dasharray="6 4"/>')
text(170, OY + 6, "Position Engine（分区 17）", size=12, color="#2563EB", weight="600", bg=True)
for cx in (A, B, DDB, TC, PU, DS):
    add(f'<line x1="{cx}" y1="124" x2="{cx}" y2="812" stroke="#CBD5E1" stroke-width="1.2" stroke-dasharray="4 4"/>')
lane(A, 140, "引擎旧主实例 A", "原 leader，epoch 7", "#FEF2F2", "#F87171")
lane(B, 140, "引擎 standby B", "另一个 AZ", "#EFF6FF", "#60A5FA")
lane(DDB, 140, "DynamoDB", "lease 表", "#F0FDF4", "#4ADE80")
lane(TC, 160, "Kafka 事务协调者", "维护 producer epoch", "#FFF7ED", "#FB923C")
lane(PU, 140, "position-updates", "分区 17", "#FFFBEB", "#F59E0B")
lane(DS, 140, "下游消费者", "read_committed", "#F8FAFC", "#94A3B8")

msg(A, PU, 165, "① 事务 T1 写入中（epoch 7，尚未提交）")
note(A, 207, 184, "② A 卡在 GC，lease 未续约")
msg(B, DDB, 255, "③ 条件写 owner=B", "blue")
msg(DDB, B, 288, "成功，B 成为新 leader", "muted", dashed=True)
msg(B, TC, 332, "④ initTransactions(pos-engine-17)", "blue")
note(TC, 372, 246, "⑤ epoch 7 → 8，作废 A 未提交的 T1")

add(f'<line x1="14" y1="405" x2="986" y2="405" stroke="{COLORS["red"]}" stroke-width="1.2" stroke-dasharray="8 5"/>')
text(18, 405, "fence 生效：此后带 epoch 7 的请求一律被拒", size=12, color=COLORS["red"], weight="600", anchor="start", bg=True)

msg(TC, PU, 438, "⑥ 写入 T1 的 abort 标记", "orange")
msg(TC, B, 472, "返回：producer epoch = 8", "muted", dashed=True)
note(B, 510, 262, "⑦ 读已提交 offset C，恢复并重放到 C", fill="#EFF6FF", stroke="#93C5FD")
msg(B, PU, 558, "⑧ 事务 T2：从 C+1 继续处理，提交成功（epoch 8）", "blue")
note(A, 602, 184, "⑨ A 醒来，继续提交 T1")
msg(A, TC, 650, "commitTransaction（epoch 7）")
msg(TC, A, 688, "ProducerFenced：epoch 已过期，拒绝", "red", dashed=True, bold=True)
note(A, 732, 190, "⑩ 停止读写，重启后当 standby", fill="#FEF2F2", stroke="#FCA5A5")
msg(PU, DS, 784, "⑪ 只交付 T2，跳过 T1", "green")

text(500, 842, "注：epoch 数值为示例。A 的提交和 B 的 initTransactions 由协调者串行处理，只会有一个先生效。",
     size=12, color="#64748B")
add('</svg>')

target = Path(__file__).with_suffix(".svg")
target.write_text("\n".join(out), encoding="utf-8")
print(target)
