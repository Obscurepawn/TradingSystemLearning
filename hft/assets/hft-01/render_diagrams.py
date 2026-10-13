from pathlib import Path
from html import escape
import subprocess


OUT = Path(__file__).resolve().parent
BLUE = "#2563eb"
PURPLE = "#7c3aed"
ORANGE = "#ea580c"
GRAY = "#6b7280"
DARK = "#111827"


class Diagram:
    def __init__(self, width, height, title):
        self.lines = []
        self.lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">')
        self.lines.append(f'<title>{escape(title)}</title>')
        self.lines.append('<style>text { font-family: "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif; }</style>')
        self.lines.append('<defs>')
        for color in [BLUE, PURPLE, ORANGE, GRAY]:
            self.lines.append(f'<marker id="a{color[1:]}" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><polygon points="0 0,10 3.5,0 7" fill="{color}"/></marker>')
        self.lines.append('</defs>')
        self.rect(0, 0, width, height, "#ffffff", stroke="none", radius=0)

    def rect(self, x, y, w, h, fill="#ffffff", stroke="#d1d5db", radius=12, extra=""):
        self.lines.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1.5" {extra}/>')

    def text(self, x, y, value, size=21, color=DARK, weight=400, anchor="start"):
        self.lines.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}">{escape(str(value))}</text>')

    def label(self, x, y, value, width, color=GRAY, size=18):
        self.rect(x - width / 2, y - size - 5, width, size + 14, "#ffffff", "none", 4)
        self.text(x, y, value, size=size, color=color, anchor="middle")

    def path(self, d, color=BLUE, dashed=False, width=2.5, arrow=True):
        dash = 'stroke-dasharray="7 5"' if dashed else ""
        marker = f'marker-end="url(#a{color[1:]})"' if arrow else ""
        self.lines.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round" {dash} {marker}/>')

    def node(self, x, y, w, h, title, subtitle, fill="#ffffff", color=DARK, extra=None):
        self.rect(x, y, w, h, fill)
        title_y = y + (36 if extra else 43)
        self.text(x + w / 2, title_y, title, size=23, color=color, weight=600, anchor="middle")
        self.text(x + w / 2, title_y + 33, subtitle, size=17, color=GRAY, anchor="middle")
        if extra:
            self.text(x + w / 2, title_y + 61, extra, size=17, color=GRAY, anchor="middle")

    def save(self, name):
        self.lines.append('</svg>')
        svg = OUT / f'{name}.svg'
        png = OUT / f'{name}.png'
        svg.write_text('\n'.join(self.lines), encoding="utf-8")
        subprocess.run(["rsvg-convert", str(svg)], stdout=subprocess.DEVNULL, check=True)
        subprocess.run(["rsvg-convert", "-w", "1920", str(svg), "-o", str(png)], check=True)
        print(f'{svg.name} -> {png.name}')


def trading_flow():
    d = Diagram(1440, 1040, "从订单提交到下一轮交易决策")
    d.text(60, 60, "从订单提交到下一轮交易决策", 32, weight=600)
    d.text(60, 100, "订单请求、私有回报与公开行情，承担不同职责。", 21, GRAY)

    d.node(80, 180, 280, 110, "投资者 / 交易策略", "产生买卖、改单与撤单意图", "#eff6ff")
    d.node(580, 180, 280, 110, "券商 / 机构交易网关", "账户检查 · 风控 · 路由与编码")
    d.node(1080, 180, 280, 110, "交易所", "接入检查 → 排序 → 撮合", "#eff6ff", extra="维护交易所内部订单簿")
    d.node(580, 390, 280, 110, "自己的订单与持仓", "私有回报 → OMS → 风险状态", "#faf5ff")
    d.node(1080, 650, 280, 110, "行情发布", "新增 / 修改 / 撤销 / 成交")
    d.node(580, 650, 280, 110, "行情接收与处理", "解析 · 序号检查 · 缺口恢复")
    d.node(80, 650, 280, 110, "本地市场订单簿", "重建可见状态 · 驱动策略", "#eff6ff")

    d.path("M 360 235 H 580")
    d.label(470, 222, "交易意图", 96, BLUE)
    d.path("M 860 235 H 1080")
    d.label(970, 222, "新单 / 修改 / 撤单", 175, BLUE)

    d.path("M 1080 265 H 970 V 445 H 860", PURPLE)
    d.label(970, 355, "私有订单回报", 136, PURPLE)
    d.path("M 580 445 H 450 V 265 H 360", PURPLE)
    d.label(450, 355, "成交 / 敞口", 126, PURPLE)

    d.path("M 1220 290 V 650", GRAY, dashed=True)
    d.label(1220, 500, "簿变化与成交事件", 174)
    d.path("M 1080 705 H 860", GRAY, dashed=True)
    d.label(970, 692, "行情消息流", 116)
    d.path("M 580 705 H 360", GRAY, dashed=True)
    d.label(470, 692, "连续有效的更新", 156)
    d.path("M 220 650 V 290", PURPLE)
    d.label(220, 500, "市场状态", 96, PURPLE)

    d.path("M 80 840 H 135", BLUE)
    d.text(151, 847, "订单请求", 18, GRAY)
    d.path("M 365 840 H 420", PURPLE)
    d.text(436, 847, "私有回报 / 本地状态反馈", 18, GRAY)
    d.path("M 845 840 H 900", GRAY, dashed=True)
    d.text(916, 847, "公开行情事件", 18, GRAY)

    d.rect(60, 895, 1320, 100, "#f9fafb", stroke="none")
    d.text(85, 935, "成交回报与公开行情走不同通道，不能简单假定它们在本地的接收顺序。", 21)
    d.text(85, 970, "成交后的清算交收，通过相应的券商、清算及托管链路继续完成。", 19, GRAY)
    d.save("01-trading-flow")


def book_table(d, x, y, title, rows, after=False):
    width = 570
    d.text(x, y - 23, title, 25, weight=600)
    d.rect(x, y, width, 350, "#ffffff")
    d.rect(x + 1, y + 1, width - 2, 62, "#f3f4f6", stroke="none", radius=10)
    cols = [x + 30, x + 145, x + 305, x + 466]
    for cx, value in zip(cols, ["订单", "卖价", "先后序号", "剩余量"]):
        d.text(cx, y + 39, value, 20, GRAY, 600)
    for i, row in enumerate(rows):
        top = y + 64 + i * 70
        fill = "#f9fafb" if after and i < 2 else ("#eff6ff" if i < 2 else "#ffffff")
        d.rect(x + 2, top, width - 4, 68, fill, stroke="none", radius=0)
        color = GRAY if after and i < 2 else DARK
        for cx, value in zip(cols, row):
            d.text(cx, top + 43, value, 22, color)


def price_time():
    d = Diagram(1440, 940, "价格优先，再按同价队列先后成交")
    d.text(60, 60, "价格优先，再按同价队列先后成交", 32, weight=600)
    d.text(60, 101, "普通可见限价单 · 连续竞价 · 同价 FIFO", 21, GRAY)
    d.rect(70, 140, 1300, 75, "#eff6ff", "#bfdbfe")
    d.text(100, 187, "到达的买单：限价 100.02，数量 400", 27, BLUE, 600)

    before = [["S1", "100.01", "105", "100"], ["S2", "100.01", "110", "200"], ["S3", "100.02", "80", "150"], ["S4", "100.03", "90", "300"]]
    after = [["S1", "100.01", "105", "已移除"], ["S2", "100.01", "110", "已移除"], ["S3", "100.02", "80", "50"], ["S4", "100.03", "90", "300"]]
    book_table(d, 70, 295, "撮合前：卖方队列", before)
    book_table(d, 800, 295, "撮合后：S3 仍剩 50", after, after=True)
    d.path("M 640 470 H 800")
    d.label(720, 453, "依次成交", 100, BLUE)

    for x, heading, value in [(70, "成交 1 · S1", "100 股 × 100.01"), (520, "成交 2 · S2", "200 股 × 100.01"), (970, "成交 3 · S3", "100 股 × 100.02")]:
        d.node(x, 700, 380, 105, heading, value, "#eff6ff")
    d.text(70, 854, "S3 虽然更早进入队列，但卖价更高，仍排在 S1、S2 后面。", 22)
    d.text(70, 893, "限价控制可接受的价格边界；本例按簿上对手订单的价格成交。", 20, GRAY)
    d.save("02-price-time-matching")


def race_panel(d, x, title, buy_first):
    y = 165
    d.rect(x, y, 630, 560, "#ffffff")
    d.rect(x + 1, y + 1, 628, 65, "#eff6ff" if buy_first else "#faf5ff", stroke="none")
    d.text(x + 25, y + 43, title, 24, weight=600)
    maker, engine, taker = x + 85, x + 315, x + 545
    for xx, name in [(maker, "报价方"), (engine, "撮合引擎"), (taker, "抢单方")]:
        d.text(xx, 276, name, 20, weight=600, anchor="middle")
        d.path(f"M {xx} 300 V 600", GRAY, dashed=True, width=1.2, arrow=False)
    d.label(engine, 320, "新信息出现", 130, GRAY, 18)
    d.rect(engine - 7, 350, 14, 155, "#dbeafe" if buy_first else "#ede9fe", stroke="none", radius=0)

    if buy_first:
        d.path(f"M {taker} 382 H {engine + 7}", BLUE)
        d.label((engine + taker) / 2, 369, "买单 +18 µs", 145, BLUE)
        d.path(f"M {maker} 468 H {engine - 7}", ORANGE)
        d.label((maker + engine) / 2, 455, "撤单 +25 µs", 145, ORANGE)
        d.rect(x + 188, 530, 254, 48, "#eff6ff", "#bfdbfe")
        d.text(engine, 562, "旧卖单在 100.01 成交", 20, BLUE, 600, "middle")
        d.text(x + 28, 645, "买单先被处理，成交已经发生。", 21)
        d.text(x + 28, 683, "后到的撤单无法撤回这笔成交。", 19, GRAY)
    else:
        d.path(f"M {maker} 382 H {engine - 7}", ORANGE)
        d.label((maker + engine) / 2, 369, "撤单 +15 µs", 145, ORANGE)
        d.path(f"M {taker} 468 H {engine + 7}", BLUE)
        d.label((engine + taker) / 2, 455, "买单 +22 µs", 145, BLUE)
        d.rect(x + 188, 530, 254, 48, "#faf5ff", "#e9d5ff")
        d.text(engine, 562, "旧卖单已撤销", 20, PURPLE, 600, "middle")
        d.text(x + 28, 645, "撤单先被处理，旧报价已经失效。", 21)
        d.text(x + 28, 683, "后到的买单无法再与该报价成交。", 19, GRAY)


def cancel_race():
    d = Diagram(1440, 930, "旧报价上的竞争：买单和撤单谁先被处理")
    d.text(60, 60, "旧报价上的竞争：买单和撤单谁先被处理", 32, weight=600)
    d.text(60, 102, "旧卖价 100.01，新估值 100.05；假设请求分别在以下时刻被处理。", 21, GRAY)
    race_panel(d, 60, "情形 A：买单先被处理", True)
    race_panel(d, 750, "情形 B：撤单先被处理", False)
    d.path("M 80 779 H 135", BLUE)
    d.text(152, 786, "主动买入请求", 19, GRAY)
    d.path("M 400 779 H 455", ORANGE)
    d.text(472, 786, "撤单请求", 19, GRAY)
    d.text(800, 786, "时间相对新信息出现时刻；自上而下推进", 18, GRAY)
    d.rect(60, 824, 1320, 65, "#f9fafb", stroke="none")
    d.text(85, 866, "本地发出撤单后，订单仍可能成交；风险持续到撤单在交易所生效。", 23, weight=600)
    d.save("03-cancel-take-race")


if __name__ == "__main__":
    trading_flow()
    price_time()
    cancel_race()
