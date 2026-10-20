"""生成 positions_architecture_aws.svg：分布式持仓服务总体架构，虚线框标出各层使用的 AWS 服务。

用法：python3 positions_architecture_aws.py
"""
from html import escape
from pathlib import Path

W, H = 1400, 1630
FONT = "-apple-system, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif"

# 填充色、边框色、AWS 服务名颜色
KIND = {
    "ext":     ("#F3F4F6", "#9CA3AF", "#6B7280"),
    "stream":  ("#FFF5E8", "#E39B3F", "#B4691A"),
    "compute": ("#EAF2FC", "#4F86C6", "#2F64A3"),
    "db":      ("#EAF7EF", "#4FA36D", "#2E7D4F"),
}
out = []
add = out.append


def text(x, y, s, size=13, color="#374151", weight="normal", anchor="middle"):
    add(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" '
        f'text-anchor="{anchor}" dominant-baseline="middle">{escape(s)}</text>')


def group(x, y, w, h, label, align="left", ext=False):
    stroke, fill = ("#9CA3AF", "#FAFAFA") if ext else ("#475569", "#F8FAFC")
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}" '
        f'stroke-width="1.6" stroke-dasharray="7 5"/>')
    lx, anchor = (x + 14, "start") if align == "left" else (x + w - 14, "end")
    text(lx, y + 18, label, size=13.5, color="#6B7280" if ext else "#334155", weight="600", anchor=anchor)


def node(x, y, w, h, title, sub=None, service=None, kind="compute"):
    fill, stroke, tag = KIND[kind]
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
    lines = [(title, 15, "#111827", "600")]
    if sub:
        lines.append((sub, 12.5, "#4B5563", "normal"))
    if service:
        lines.append((service, 11.5, tag, "600"))
    y0 = y + h / 2 - 10 * (len(lines) - 1)
    for i, (s, size, color, weight) in enumerate(lines):
        text(x + w / 2, y0 + i * 20, s, size=size, color=color, weight=weight)


def arrow(pts, style="solid"):
    d = "M " + " L ".join(f"{px} {py}" for px, py in pts)
    dash = {"solid": "", "dashed": ' stroke-dasharray="6 4"', "dotted": ' stroke-dasharray="2 4"'}[style]
    add(f'<path d="{d}" fill="none" stroke="#4B5563" stroke-width="1.6"{dash} marker-end="url(#ah)"/>')


def label(x, y, lines, anchor="start"):
    for i, s in enumerate(lines):
        text(x, y + i * 17, s, size=12, color="#374151", anchor=anchor)


add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">')
add('<title>分布式持仓服务总体架构（按 AWS 服务分组）</title>')
add('<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
    'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="#4B5563"/></marker></defs>')
add(f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>')

# ---------- 虚线框：每一层使用的 AWS 服务 ----------
group(90, 18, 1270, 130, "上游系统（不在 AWS 内）", ext=True)
group(200, 222, 800, 148, "接入层 · Amazon MSK + Amazon EKS", align="right")
group(560, 430, 480, 135, "事件日志 · Amazon MSK")
group(1080, 430, 280, 135, "对账 · Amazon EKS + Athena")
group(300, 620, 810, 175, "计算层 · Amazon EKS（EC2 节点）")
group(1140, 640, 220, 135, "Amazon DynamoDB")
group(560, 850, 480, 125, "权威输出 · Amazon MSK")
group(1140, 850, 220, 125, "Amazon S3")
group(200, 1040, 520, 500, "查询支路 · EKS + ElastiCache + DynamoDB + NLB")
group(760, 1040, 600, 500, "账户汇总支路 · Amazon EKS + Amazon MSK", align="right")

# ---------- 组件 ----------
node(120, 50, 220, 80, "OMS", "Transactional Outbox", kind="ext")
node(420, 50, 220, 80, "交易所 / 券商网关", "FIX", kind="ext")
node(720, 50, 260, 80, "调度 / 运营系统", "交易日切换、拆股合股", kind="ext")
node(1110, 50, 220, 80, "Drop Copy", "交易所的独立成交副本", kind="ext")

node(250, 262, 270, 86, "raw topics", "按上游会话分区", "Amazon MSK", kind="stream")
node(640, 262, 320, 86, "Normalizer", "序号去重 / 缺口检测 / 标的映射", "Amazon EKS", kind="compute")

node(630, 462, 340, 86, "trading-events", "唯一事实来源 · 按 (account, instrument) 分区",
     "Amazon MSK · 跨 AZ 多副本 · Tiered Storage", kind="stream")
node(1110, 462, 220, 86, "对账", "日内 / 日终对账", "Amazon EKS · Athena", kind="compute")

node(340, 660, 250, 100, "Hot standby（另一 AZ）", "只追到已提交 offset，不输出", "Amazon EKS", kind="compute")
node(640, 660, 320, 100, "Position Engine（主）", "每分区 single writer · 内存状态", "Amazon EKS StatefulSet · EC2", kind="compute")
node(1165, 672, 170, 86, "选主 lease", "条件写", "Amazon DynamoDB", kind="db")

node(640, 876, 320, 86, "position-updates", "唯一权威输出 · compacted", "Amazon MSK", kind="stream")
node(1165, 876, 170, 86, "快照 / 归档", "Object Lock", "Amazon S3", kind="db")

node(300, 1080, 300, 86, "Projector", "按 (generation, version) 条件写", "Amazon EKS", kind="compute")
node(300, 1245, 300, 86, "读模型", "Redis 缓存 + DynamoDB 持久化", "ElastiCache · DynamoDB", kind="db")
node(300, 1420, 300, 86, "Query API", "按请求选择一致性等级", "Amazon EKS + NLB", kind="compute")

node(940, 1080, 320, 86, "Repartitioner", "按 account 重分区", "Amazon EKS", kind="compute")
node(940, 1195, 320, 78, "by-account 流", None, "Amazon MSK", kind="stream")
node(940, 1310, 320, 86, "Account Aggregator", "按账户汇总持仓", "Amazon EKS", kind="compute")
node(940, 1420, 320, 86, "account-summary", "账户汇总 · compacted", "Amazon MSK", kind="stream")

# ---------- 数据流 ----------
arrow([(230, 130), (330, 262)]);   label(266, 194, ["订单请求"], anchor="end")
arrow([(530, 130), (410, 262)]);   label(494, 186, ["交易所回报", "成交 / bust / correct"])
arrow([(850, 130), (500, 262)]);   label(748, 180, ["交易日切换 / 拆股合股", "每个分区各写一条"])
arrow([(520, 305), (640, 305)])
arrow([(800, 348), (800, 462)]);   label(812, 392, ["标准化事件", "+ watermark：数据完整到哪一刻"])
arrow([(1300, 130), (1300, 462)]); label(1288, 288, ["按 (venue, exec_id)", "逐字段比对"], anchor="end")
arrow([(970, 505), (1110, 505)])
arrow([(800, 548), (800, 660)]);   label(812, 600, ["按分区顺序消费"])
arrow([(660, 548), (560, 660)], "dashed"); label(588, 590, ["跟随消费"], anchor="end")
arrow([(960, 710), (1165, 715)], "dashed"); label(972, 694, ["主备通过 lease 选主"])
arrow([(800, 760), (800, 876)]);   label(812, 806, ["持仓变更 + 进度记录", "与输入 offset 同一事务提交"])
arrow([(960, 748), (1165, 905)]);  label(1120, 818, ["提交边界的快照"])
arrow([(700, 962), (700, 1123), (600, 1123)])
arrow([(900, 962), (900, 1123), (940, 1123)])
arrow([(450, 1166), (450, 1245)])
arrow([(450, 1331), (450, 1420)])
arrow([(1100, 1166), (1100, 1195)]); label(1112, 1180, ["同一账户落到同一分区"])
arrow([(1100, 1273), (1100, 1310)])
arrow([(1100, 1396), (1100, 1420)]); label(1112, 1407, ["多空敞口 / 在途挂单 / 已实现盈亏"])
arrow([(740, 760), (740, 820), (160, 820), (160, 1463), (300, 1463)], "dotted")
label(172, 900, ["STRONG / CAUSAL 读", "直连引擎 leader"])

# ---------- 图例 ----------
ly = 1585
add(f'<rect x="200" y="{ly - 12}" width="44" height="24" rx="5" fill="#F8FAFC" stroke="#475569" '
    f'stroke-width="1.4" stroke-dasharray="6 4"/>')
text(254, ly, "虚线框：该层部署所用的 AWS 服务", size=12.5, anchor="start")
x = 560
for kind, name in [("compute", "计算组件"), ("stream", "Kafka topic"), ("db", "存储 / 数据库"), ("ext", "外部系统")]:
    fill, stroke, _ = KIND[kind]
    add(f'<rect x="{x}" y="{ly - 10}" width="30" height="20" rx="4" fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>')
    text(x + 40, ly, name, size=12.5, anchor="start")
    x += 170
add(f'<path d="M 1240 {ly} L 1290 {ly}" stroke="#4B5563" stroke-width="1.6" stroke-dasharray="2 4" marker-end="url(#ah)"/>')
text(1298, ly, "同步读", size=12.5, anchor="start")
add('</svg>')

target = Path(__file__).with_suffix(".svg")
target.write_text("\n".join(out), encoding="utf-8")
print(target)
