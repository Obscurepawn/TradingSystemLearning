#!/usr/bin/env python3
"""Generate figures for HFT Engineering 10 (rare jitter sampling & attribution).

Outputs SVG + PNG (via rsvg-convert) into this directory.
Style mirrors the existing hft-0X assets: white background, PingFang/Noto CJK,
ink #15243b, blue/orange/green/red semantic palette.
"""
from __future__ import annotations
import math
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent

FONT = '"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif'
INK = "#15243b"
MUTED = "#64748b"
FAINT = "#94a3b8"

BLUE = "#2563eb"; BLUE_BG = "#eff6ff"; BLUE_BD = "#93c5fd"
ORANGE = "#c2410c"; ORANGE_BG = "#fff7ed"; ORANGE_BD = "#fdba74"
GREEN = "#059669"; GREEN_BG = "#f0fdf4"; GREEN_BD = "#6ee7b7"
RED = "#dc2626"; RED_BG = "#fef2f2"; RED_BD = "#fca5a5"
PURPLE = "#7c3aed"; PURPLE_BG = "#f5f3ff"; PURPLE_BD = "#c4b5fd"
GRAY_BG = "#f8fafc"; GRAY_BD = "#e5e7eb"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Svg:
    def __init__(self, w: int, h: int, title: str, subtitle: str = ""):
        self.w, self.h = w, h
        self.p: list[str] = []
        self.p.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">')
        self.p.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff"/>')
        self.p.append(f'<text x="44" y="50" font-family={FONT!r} font-size="27" font-weight="600" fill="{INK}">{esc(title)}</text>')
        if subtitle:
            self.p.append(f'<text x="44" y="80" font-family={FONT!r} font-size="16" fill="{MUTED}">{esc(subtitle)}</text>')

    def text(self, x, y, s, size=16, bold=False, color=INK, anchor="start", italic=False):
        fw = "600" if bold else "400"
        st = ' font-style="italic"' if italic else ""
        self.p.append(
            f'<text x="{x}" y="{y}" font-family={FONT!r} font-size="{size}" font-weight="{fw}" '
            f'fill="{color}" text-anchor="{anchor}"{st}>{esc(s)}</text>')

    def rect(self, x, y, w, h, fill="#fff", bd=GRAY_BD, bw=1.5, rx=8, dash=None, opacity=1.0):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.p.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
            f'stroke="{bd}" stroke-width="{bw}"{d} opacity="{opacity}"/>')

    def line(self, x1, y1, x2, y2, color=FAINT, bw=1.6, dash=None, marker=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        m = f' marker-end="url(#{marker})"' if marker else ""
        self.p.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{bw}"{d}{m}/>')

    def arrow(self, x1, y1, x2, y2, color=BLUE, bw=2):
        self.p.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{bw}" marker-end="url(#ah)"/>')

    def path(self, d, fill="none", color=BLUE, bw=2, dash=None):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self.p.append(f'<path d="{d}" fill="{fill}" stroke="{color}" stroke-width="{bw}"{da}/>')

    def circle(self, x, y, r, fill=BLUE, bd=BLUE, bw=1):
        self.p.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}" stroke="{bd}" stroke-width="{bw}"/>')

    def raw(self, s):
        self.p.append(s)

    def save(self, name):
        self.p.append("</svg>")
        svg = "\n".join(self.p)
        sp = HERE / f"{name}.svg"
        sp.write_text(svg, encoding="utf-8")
        r = subprocess.run(["rsvg-convert", "-w", "1360", "-o", str(HERE / f"{name}.png"), str(sp)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"rsvg failed {name}: {r.stderr}")
        print(f"ok {name}")


ARROW_DEF = ('<defs><marker id="ah" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">'
             '<path d="M0,0 L10,4 L0,8 Z" fill="#2563eb"/></marker></defs>')


# ---------------------------------------------------------------- fig 1
def fig_tail_profile():
    s = Svg(1280, 600, "中位数很好看，钱亏在你看不见的那一档",
            "p50/p99 漂亮不代表系统稳：p99.99 上那根几万次才出现一次的刺，才是真正打钱的地方")
    x0, x1, y0, y1 = 96, 1190, 130, 470
    # axes
    s.line(x0, y1, x1 + 10, y1, INK, 1.8)
    s.line(x0, y0 - 10, x0, y1, INK, 1.8)

    def X(t):  # log10 latency us -> px, domain 1..100
        return x0 + (math.log10(t) / 2.0) * (x1 - x0)

    # density curve (gaussian in log space, center 2.5us)
    mu, sig = math.log10(2.6), 0.105
    pts = []
    for i in range(0, 420):
        t = 10 ** (2.0 * i / 419)
        lt = math.log10(t)
        d = math.exp(-((lt - mu) ** 2) / (2 * sig * sig))
        # near-zero tail after 8us, with rare-event spikes
        tail = 0.0
        bumps = [(20, 0.05), (40, 0.16), (63, 0.04)]
        for bt, bh in bumps:
            tail += bh * math.exp(-((lt - math.log10(bt)) ** 2) / (2 * 0.012 ** 2))
        v = d * 0.92 + tail * (1 if t > 12 else 0)
        pts.append((X(t), y1 - v * 300))
    # split fill: blue main, red tail (>10us)
    split_x = X(10)
    main = [(x0, y1)] + [(px, py) for px, py in pts if px <= split_x] + [(split_x, y1)]
    tailp = [(split_x, y1)] + [(px, py) for px, py in pts if px > split_x] + [(x1, y1)]
    s.path("M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in main), fill=BLUE_BG, color=BLUE, bw=2)
    s.path("M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in tailp), fill=RED_BG, color=RED, bw=2)

    # percentile markers
    for t, lab, c in [(2.3, "p50", GREEN), (4.5, "p99", ORANGE), (40, "p99.99", RED)]:
        xx = X(t)
        s.line(xx, y0 - 6, xx, y1, c, 1.6, dash="5 4")
        s.text(xx, y0 - 14, lab, 15, True, c, "middle")
    s.text((x0 + X(10)) / 2, y0 + 24, "99% 的请求：2 ~ 5µs", 17, True, BLUE, "middle")
    s.text((X(10) + x1) / 2, y0 + 24, "0.01% 的请求：几十 µs", 17, True, RED, "middle")
    s.text(X(40), y1 - 60, "一次尖峰 = 错过一档成交", 14, True, RED, "middle")
    s.text(x1 - 6, y1 + 24, "单次处理延迟（µs，对数轴）", 15, color=MUTED, anchor="end")
    s.text(x0 - 12, y0 - 6, "请求数量", 15, color=MUTED, anchor="end")
    s.text(x0 + 8, y1 + 44, "1", 13, color=MUTED)
    s.text(x1 - 4, y1 + 44, "100", 13, color=MUTED, anchor="end")
    s.text(640, 540, "这条尾巴不是统计噪音：正常时它只占万分之一，可一旦你的仓位暴露在市场里，它就是实实在在的亏损",
           16, color=MUTED, anchor="middle")
    s.save("jitter-01-tail-profile")


# ---------------------------------------------------------------- fig 2
def fig_sampling_miss():
    s = Svg(1280, 540, "为什么 profiler 永远抓不到那一下",
            "采样是抽签：尖峰只活 40µs，而探针一毫秒才路过一次，撞上它的概率和它本身的概率一样低")
    # row A: requests
    s.text(70, 168, "请求流", 17, True)
    s.text(70, 190, "2µs / 次", 14, color=MUTED)
    ya, h = 175, 34
    x_start, x_end = 180, 1180
    spike_x = 660
    bw_, gap = 13, 4
    x = x_start
    i = 0
    while x < x_end:
        if spike_x - 20 <= x < spike_x + 78:
            x += bw_ + gap
            i += 1
            continue
        s.rect(x, ya, bw_, h, BLUE_BG, BLUE_BD, 1, rx=3)
        x += bw_ + gap
        i += 1
    s.rect(spike_x - 8, ya - 6, 96, h + 12, RED_BG, RED, 2, rx=4)
    s.text(spike_x + 40, ya - 16, "40µs 尖峰", 15, True, RED, "middle")
    s.text(spike_x + 40, ya + h + 32, "约 5 万次请求里才有一次（时间轴已压缩）", 13, color=RED, anchor="middle")
    s.line(x_start, ya + h + 52, x_end, ya + h + 52, MUTED, 1.2)

    # row B: profiler samples
    yb = 330
    s.text(70, yb - 5, "profiler", 17, True)
    s.text(70, yb + 17, "~1ms 采一次", 14, color=MUTED)
    samples = [230, 350, 470, 590, 830, 950, 1070]
    for sx in samples:
        s.line(sx, yb - 34, sx, yb + 26, "#7c3aed", 2)
        s.p.append(f'<path d="M{sx-6},{yb+18} L{sx},{yb+30} L{sx+6},{yb+18} Z" fill="#7c3aed"/>')
        s.text(sx, yb + 52, "采样", 12, color=PURPLE, anchor="middle")
    s.line(spike_x - 8, yb - 40, spike_x + 88, yb - 40, RED, 1.6)
    s.text(spike_x + 40, yb - 48, "尖峰存活窗口", 13, True, RED, "middle")
    s.text(590, yb - 58, "最近的一次采样也差了 0.4ms", 13, color=MUTED, anchor="end")

    s.rect(180, 430, 1000, 74, GRAY_BG, GRAY_BD, 1.4, rx=10)
    s.text(210, 462, "采样间隔 1ms  ≫  尖峰宽度 40µs：它落在你采样间隔里的概率不到 4%；", 16, True)
    s.text(210, 490, "就算真撞上了，等你看到报表、attach 上去，那次抖动早就结束了——案发现场只存在于它发生的那几十微秒", 16)
    s.save("jitter-02-sampling-miss")


# ---------------------------------------------------------------- fig 3
def fig_coordinated_omission():
    s = Svg(1280, 620, "还有一种看不见，是压测器自己瞎了",
            "Coordinated Omission：闭环压测线程被卡住时，卡顿期间本应发出的请求根本没有出生，尾部门形被系统性低估")
    # left: closed loop timeline
    s.text(70, 130, "闭环压测线程（发一个、等一个）", 17, True)
    y = 170
    xs = [90 + i * 34 for i in range(11)]
    for i, xx in enumerate(xs):
        s.rect(xx, y, 26, 30, BLUE_BG, BLUE_BD, 1, rx=4)
    s.text(xs[-1] + 30, y + 20, "…", 20, color=MUTED)
    # stall
    sx0 = xs[-1] + 60
    s.rect(sx0, y - 8, 190, 46, RED_BG, RED, 2, rx=5)
    s.text(sx0 + 95, y + 12, "卡在这一笔上", 15, True, RED, "middle")
    s.text(sx0 + 95, y + 32, "（尾延迟尖峰）", 13, color=RED, anchor="middle")
    # ghost requests
    for i in range(5):
        gx = sx0 + 10 + i * 36
        s.rect(gx, y - 66, 26, 30, "#ffffff", RED_BD, 1.6, rx=4, dash="5 4")
        s.text(gx + 13, y - 30, "✕", 12, True, RED, anchor="middle")
    s.text(sx0 + 95, y - 78, "卡顿期间本应发出的请求：从未发出，也就永远不会出现在统计里", 14, True, RED, "middle")
    s.arrow(sx0 - 12, y - 50, sx0, y - 8, RED, 1.6)
    # after
    for i in range(3):
        xx = sx0 + 210 + i * 34
        s.rect(xx, y, 26, 30, BLUE_BG, BLUE_BD, 1, rx=4)
    s.line(70, y + 64, 1100, y + 64, MUTED, 1.4, marker=None)
    s.text(70, y + 88, "时间 →", 13, color=MUTED)

    # right: two histograms
    hx = 770
    # measured
    s.text(hx, 272, "压测器实测分布", 16, True)
    base_y = 386
    s.rect(hx, base_y - 92, 34, 92, BLUE, BLUE, 1, rx=3)
    s.rect(hx + 44, base_y - 15, 34, 15, RED, RED, 1, rx=3)
    s.text(hx + 60, base_y + 26, "只有卡住的那 1 笔被记成慢请求，尾巴几乎看不见", 13, color=MUTED)
    s.line(hx - 10, base_y, hx + 260, base_y, INK, 1.4)
    # corrected
    s.text(hx, 464, "校正后（open-loop + recordCorrectedValue）", 16, True)
    base_y2 = 576
    s.rect(hx, base_y2 - 92, 34, 92, BLUE, BLUE, 1, rx=3)
    for i, hh in enumerate([40, 54, 66, 78, 90]):
        s.rect(hx + 44 + i * 34, base_y2 - hh, 28, hh, RED_BD, RED_BD, 1, rx=3)
    s.text(hx + 110, base_y2 + 26, "缺席的请求全部补记为受影响延迟，尾巴现形", 13, color=MUTED)
    s.line(hx - 10, base_y2, hx + 260, base_y2, INK, 1.4)

    s.rect(70, 430, 640, 150, ORANGE_BG, ORANGE_BD, 1.5, rx=10)
    s.text(96, 464, "对策", 16, True, ORANGE)
    s.text(96, 494, "① 改用开环（open-loop）压测：请求按独立时钟发出，不等上一笔返回", 15)
    s.text(96, 522, "② HdrHistogram 的 recordCorrectedValue：把卡顿区间内应发未发的请求", 15)
    s.text(112, 546, "按预期发起时间一并记成受影响延迟", 15)
    s.save("jitter-03-coordinated-omission")


# ---------------------------------------------------------------- fig 4
def fig_three_layers():
    s = Svg(1280, 700, "一套三层取证体系：从进程内一路问到物理链路",
            "每层只回答自己能回答的问题，成本模型也完全不同；关键是三层共用同一个时间基")
    bands = [
        ("应用层", "每请求固定|纳秒级", BLUE_BG, BLUE_BD, BLUE,
         ["rdtscp 分段打点（RX→解析→决策→风控→TX）", "per-thread 无锁 Ring 黑匣子 + HdrHistogram", "rdpmc 计数器差分：指令/周期/缓存缺失"],
         "慢了多少？慢在哪一类：多干活、等内存、进内核，还是被切走了？"),
        ("内核层", "关闭≈0|事件时 50–200ns/条", GREEN_BG, GREEN_BD, GREEN,
         ["rtla timerlat / osnoise、hwlat_detector", "ftrace tracepoint：fault / sched_switch / irq", "perf（短窗体检）/ eBPF"],
         "具体元凶是谁：fault 打在哪个地址、CPU 切给了谁、是哪个 IRQ、关中断关了多久？"),
        ("物理 / 网络层", "对被测|主机零负载", ORANGE_BG, ORANGE_BD, ORANGE,
         ["光分路器 TAP + FPGA 抓包卡（纳秒硬件时间戳）", "低延迟网卡 + kernel bypass（ef_vi / DPDK）", "云上：Nitro PTP 硬件时钟 + 纳秒硬件包时间戳"],
         "独立第三方事实：这个包到底几点几分到的网卡？嫌疑人不能给自己作证"),
    ]
    y = 112
    for name, cost, bg, bd, c, items, q in bands:
        s.rect(44, y, 1192, 168, bg, bd, 1.6, rx=12)
        s.rect(44, y, 150, 168, c, c, 0, rx=12)
        s.text(119, y + 76, name, 22, True, "#ffffff", "middle")
        for k, cl in enumerate(cost.split("|")):
            s.text(119, y + 106 + k * 18, cl, 12, "#ffffff", "middle")
        for i, it in enumerate(items):
            s.text(222, y + 40 + i * 32, "• " + it, 15)
        s.text(222, y + 150, "回答：" + q, 15, True, c)
        y += 182
    s.rect(44, 660, 1192, 34, GRAY_BG, GRAY_BD, 1.4, rx=9)
    s.text(640, 683, "统一时间基：机内全部用 TSC（ftrace 设 trace_clock=x86-tsc）  ·  跨机用 PTP + PPS/GPS Grandmaster  ·  云上用 Nitro PHC —— 三层证据才能对齐到同一条时间线",
           14, True, anchor="middle")
    s.save("jitter-04-three-layer-forensics")


# ---------------------------------------------------------------- fig 5
def fig_flight_recorder():
    s = Svg(1280, 720, "常驻黑匣子 + 阈值快照：保证那一次一定被录下来",
            "热路径上只允许做纳秒级的四件事；冻结现场、回溯、落盘全部交给独立的遥测核")
    stages = ["RX 收包", "解析", "决策", "风控", "TX 发单"]
    bx, by, bw, h, gap = 60, 130, 168, 60, 34
    for i, st_ in enumerate(stages):
        x = bx + i * (bw + gap)
        s.rect(x, by, bw, h, BLUE_BG, BLUE_BD, 1.6, rx=9)
        s.text(x + bw / 2, by + 38, st_, 18, True, BLUE, "middle")
        if i < len(stages) - 1:
            s.arrow(x + bw + 4, by + h / 2, x + bw + gap - 4, by + h / 2, BLUE, 2)
    # probes
    probes = [bx, bx + bw + gap, bx + 2 * (bw + gap), bx + 3 * (bw + gap), bx + 4 * (bw + gap)]
    labels = ["t0", "t1", "t2", "t3", "t4"]
    for px, lab in zip(probes, labels):
        s.circle(px, by + h + 22, 12, ORANGE_BG, ORANGE, 2)
        s.text(px, by + h + 27, lab, 12, True, ORANGE, anchor="middle")
        s.line(px, by + h, px, by + h + 10, ORANGE, 1.6)
    s.text(70, 246, "__rdtscp() 半串行打点，启动时校准 cycles→ns；探测记录只写 per-thread 预分配无锁环形缓冲", 14, color=MUTED)

    # ring
    cx, cy, rx_, ry_ = 320, 470, 200, 105
    s.p.append(f'<ellipse cx="{cx}" cy="{cy}" rx="{rx_}" ry="{ry_}" fill="{GRAY_BG}" stroke="{GRAY_BD}" stroke-width="1.6"/>')
    s.p.append(f'<ellipse cx="{cx}" cy="{cy}" rx="{rx_-26}" ry="{ry_-26}" fill="#ffffff" stroke="{GRAY_BD}" stroke-width="1.4" stroke-dasharray="4 4"/>')
    s.path(f"M {cx+150} {cy-70} A {rx_-20} {ry_-14} 0 0 1 {cx+150} {cy+70}", color=BLUE, bw=2.4)
    s.raw(f'<path d="M{cx+146},{cy+78} l14,-2 l-6,-12 z" fill="{BLUE}"/>')
    s.text(cx, cy - 8, "SPSC 无锁环形缓冲", 17, True, anchor="middle")
    s.text(cx, cy + 18, "滚动覆盖最近 N 万条", 14, color=MUTED, anchor="middle")
    s.text(cx, cy + 42, "不分配 · 不加锁 · 不 syscall · 不落盘", 13, True, BLUE, anchor="middle")
    for px in probes[:3]:
        s.line(px, by + h + 36, cx - 60, cy - 70, ORANGE, 1.2, dash="4 3")

    # threshold trigger
    s.rect(600, 300, 200, 74, RED_BG, RED, 2, rx=10)
    s.text(700, 330, "ΔT > 阈值？", 18, True, RED, "middle")
    s.text(700, 356, "原子发布 seq/t0/t1：5~10ns", 13, color=RED, anchor="middle")
    s.line(probes[-1] - 30, by + h + 30, 660, 300, RED, 1.4, dash="5 4")
    s.text(660, 286, "热线程立即去处理下一请求，不等任何东西", 13, True, RED, anchor="middle")

    # telemetry core
    tx, ty, tw, th = 852, 300, 388, 320
    s.rect(tx, ty, tw, th, GREEN_BG, GREEN_BD, 1.8, rx=12)
    s.text(tx + tw / 2, ty + 34, "遥测核（独立物理核，SCHED_FIFO 低优先级）", 16, True, GREEN, "middle")
    steps = [
        "① 轮询原子标志 / eventfd 被唤醒",
        "② write trace_marker：seq + 裸 TSC",
        "③ echo 1 > snapshot：内核原子交换缓冲指针",
        "④ 从冻结副本读异常前后窗口，异步落盘",
        "⑤ latch + 冷却窗口，防止触发风暴",
    ]
    for i, st_ in enumerate(steps):
        s.text(tx + 24, ty + 76 + i * 44, st_, 15)
    s.arrow(800, 337, tx, 337, RED, 2)
    s.rect(600, 420, 200, 120, ORANGE_BG, ORANGE_BD, 1.5, rx=10)
    s.text(700, 450, "关键：不是去 live ring 里", 14, True, ORANGE, "middle")
    s.text(700, 474, "“捞最近事件”", 14, True, ORANGE, "middle")
    s.text(700, 502, "调度延迟期间现场会被冲掉；", 13, color=INK, anchor="middle")
    s.text(700, 524, "必须先原子换缓冲，再读冻结副本", 13, color=INK, anchor="middle")
    s.save("jitter-05-flight-recorder")


# ---------------------------------------------------------------- fig 6
def fig_head_of_line():
    s = Svg(1280, 580, "快照不是免费的：它会制造一场队头阻塞",
            "串行热路径上，异常请求被同步延长 C，排在它后面的所有请求都要陪着等——预算约束是 C ≪ 1/λ")
    # wrong row
    s.text(70, 120, "✗ 异常当场同步抓栈、回溯、memcpy、落盘", 17, True, RED)
    y = 150
    ideal_y = y + 96
    s.line(70, ideal_y, 1210, ideal_y, FAINT, 1.2, dash="3 4")
    s.text(70, ideal_y - 6, "理想位置：", 12, color=MUTED)
    # N extended
    s.rect(180, y, 240, 56, RED_BG, RED, 2, rx=6)
    s.text(300, y + 26, "请求 N（元凶，处理越限）", 14, True, RED, "middle")
    s.text(300, y + 46, "+ 同步快照成本 C", 13, color=RED, anchor="middle")
    # delayed followers
    for i, lab in enumerate(["N+1", "N+2", "N+3", "N+4"]):
        x = 436 + i * 96
        s.rect(x, y, 80, 56, ORANGE_BG, ORANGE_BD, 1.6, rx=6)
        s.text(x + 40, y + 33, lab, 14, True, ORANGE, "middle")
        # ideal ghost
        s.rect(x - 120, ideal_y - 22, 80, 44, "#ffffff", BLUE_BD, 1.2, rx=6, dash="4 4", opacity=0.7)
        s.text(x - 80, ideal_y + 4, lab, 12, color=BLUE, anchor="middle", )
        s.line(x - 80, ideal_y + 24, x + 40, y + 56, RED_BD, 1.2, dash="3 3")
    s.text(300, y + 86, "异常请求被拉长 C", 13, color=RED, anchor="middle")
    s.text(720, y + 90, "后面的请求整体后移，直到队列排空——它们是受害者，不是独立 jitter", 14, True, ORANGE)

    # formula
    s.rect(180, 286, 920, 64, GRAY_BG, GRAY_BD, 1.4, rx=10)
    s.text(220, 314, "被波及请求数 ≈ λ × C", 18, True)
    s.text(220, 338, "1M pps、C = 5µs  →  约 5 个无辜请求被拖慢；利用率 ρ 越接近 1，排空越慢，污染被放大", 14)

    # right row
    s.text(70, 404, "✓ 热核上只做纳秒四件事：写 ring → 比阈值 → 一次原子 store → 保存寄存器/TSC", 17, True, GREEN)
    y2 = 436
    s.rect(180, y2, 150, 52, RED_BG, RED, 2, rx=6)
    s.text(255, y2 + 24, "请求 N 越限", 14, True, RED, "middle")
    s.text(255, y2 + 44, "原子发布 ~5ns", 12, color=RED, anchor="middle")
    for i, lab in enumerate(["N+1", "N+2", "N+3", "N+4"]):
        x = 346 + i * 84
        s.rect(x, y2, 72, 52, BLUE_BG, BLUE_BD, 1.4, rx=6)
        s.text(x + 36, y2 + 32, lab, 14, True, BLUE, "middle")
    s.rect(720, y2 - 6, 470, 64, GREEN_BG, GREEN_BD, 1.6, rx=9)
    s.text(744, y2 + 20, "栈回溯、符号化、压缩、落盘、全量 PMU 读取", 14, True, GREEN)
    s.text(744, y2 + 44, "全部 offload 到遥测核；会消失的现场靠常驻 tracepoint 自己留痕", 13, color=INK)
    s.arrow(334, y2 + 26, 714, y2 + 26, GREEN, 2)
    s.save("jitter-06-head-of-line-blocking")


# ---------------------------------------------------------------- fig 7
def fig_decision_tree():
    s = Svg(1280, 860, "拿到 ΔT 之后，怎么给这一次抖动定罪",
            "ΔT(TSC) 只有一个数，但它的构成有四种；用相邻边界的计数器差分把它们区分开")
    # root
    s.rect(340, 104, 600, 76, GRAY_BG, INK, 1.8, rx=12)
    s.text(640, 136, "这一次 ΔT(TSC) 变大", 20, True, anchor="middle")
    s.text(640, 164, "= 用户态执行 + 内核态(fault/IRQ/syscall) + 被切走时间 + halt/虚拟化时间", 14, color=MUTED, anchor="middle")

    leaves = [
        ("Δinstructions 明显增大", "路径变长：多干了活", "数据相关冷分支、异常分支、fault 处理指令", BLUE, BLUE_BG, BLUE_BD),
        ("inst 正常，cycles:u 增大 + LLC-miss 高", "微架构停顿：在等硬件", "内存/缓存、分支预测失败、NUMA 远端访问", PURPLE, PURPLE_BG, PURPLE_BD),
        ("cycles:u 不动，内核时间变长，minflt +1", "内核介入", "minor/major page fault、系统调用、内核线程", ORANGE, ORANGE_BG, ORANGE_BD),
        ("TSC 照走，ref-cycles 停走（出现缺口）", "off-core：核被拿走了", "被抢占、vCPU 被 host deschedule、halt 进 C-state、SMI", RED, RED_BG, RED_BD),
        ("core-cycles / ref-cycles 比例下降", "降频", "AVX-512 license、温度/功耗墙、云上频率被限", GREEN, GREEN_BG, GREEN_BD),
    ]
    top, cw, ch, gap_y = 220, 1192, 108, 18
    for i, (cond, verdict, detail, c, bg, bd) in enumerate(leaves):
        y = top + i * (ch + gap_y)
        s.rect(44, y, cw, ch, bg, bd, 1.7, rx=11)
        s.rect(44, y, 14, ch, c, c, 0, rx=11)
        s.text(84, y + 38, "观测：" + cond, 16, True, c)
        s.text(720, y + 38, "→ " + verdict, 17, True, c)
        s.text(84, y + 72, detail, 14, color=MUTED)
        s.line(640, 180, 640, y, bd, 1.2)
        s.line(640, y, 58, y, bd, 1.2)
    s.text(640, 842, "注意计数器是单调累计的：必须每个边界都读、相邻请求差分复用，等触发了才第一次读，拿到的会是横跨几万次请求的区间",
           15, color=MUTED, anchor="middle")
    s.save("jitter-07-pmc-decision-tree")


# ---------------------------------------------------------------- fig 8
def fig_halt_gap():
    s = Svg(1280, 500, "一个经典签名：TSC 在走，CPU 计数器停了",
            "HLT/MWAIT 进 C-state 深睡时，所有 UNHALTED 事件冻结而 TSC 不停；醒过来还要交一笔退出税")
    x0, x1 = 150, 1150
    # TSC row
    s.text(70, 168, "TSC", 18, True)
    s.text(70, 192, "（墙上时间）", 13, color=MUTED)
    s.rect(x0, 150, x1 - x0, 40, GREEN_BG, GREEN, 2, rx=8)
    for i in range(11):
        xx = x0 + i * (x1 - x0) / 10
        s.line(xx, 150, xx, 190, GREEN, 1)
    s.text(x1 - 16, 175, "连续、单调，谁也停不下来", 14, True, GREEN, anchor="end")

    # unhalted row
    s.text(70, 308, "UNHALTED", 16, True)
    s.text(70, 332, "cycles/ref-cycles", 12, color=MUTED)
    g0, g1 = 600, 760
    s.rect(x0, 290, g0 - x0, 40, BLUE_BG, BLUE, 2, rx=8)
    s.rect(g1, 290, x1 - g1, 40, BLUE_BG, BLUE, 2, rx=8)
    s.rect(g0, 290, g1 - g0, 40, ORANGE_BG, ORANGE, 2, rx=8)
    s.text((g0 + g1) / 2, 315, "halt：HLT/MWAIT 进入 C1~C6", 14, True, ORANGE, "middle")
    s.text(x0 + 20, 315, "正常执行，计数器走", 13, True, BLUE)
    s.text(x1 - 20, 315, "中断唤醒，计数器恢复", 13, True, BLUE, anchor="end")

    # exit tax bracket
    s.line(g1, 356, g1, 392, RED, 1.6)
    s.line(g1 - 30, 392, g1 + 30, 392, RED, 1.6)
    s.text(g1, 414, "唤醒/退出税", 14, True, RED, "middle")
    s.text(g1, 438, "C1 退出 ~1µs；C6/CC6 深睡可达几~几十 µs", 13, color=RED, anchor="middle")

    s.rect(150, 60, 0, 0, "#fff", "#fff")
    s.text(640, 96, "两条时间线之间的缺口 = 这一次抖动的铁证（vCPU 被宿主机拿走时签名相同，靠 %st 与 sched_switch 区分）",
           14, color=MUTED, anchor="middle")
    s.text(150, 470, "注：PAUSE 指令不是 halt；忙轮询 + intel_idle.max_cstate=1 + idle=poll 可以在 guest 内防住自己主动睡过去的那一类",
           13, color=MUTED)
    s.save("jitter-08-halt-cstate-gap")


# ---------------------------------------------------------------- fig 9
def fig_replay_matrix():
    s = Svg(1280, 700, "同一份 trace 跑五组：干净跑出真值，带探针的只找瓶颈",
            "不要指望一次运行既测准又看清；相同 profiling 配置的开销会在 A/B 差分里自动抵消")
    cols = [(44, 130), (174, 470), (644, 470), (1114, 122)]
    heads = ["轮次", "配置", "产出", "当真？"]
    y = 104
    for (x, w), htxt in zip(cols, heads):
        s.rect(x, y, w, 40, INK, INK, 0, rx=6)
        s.text(x + w / 2, y + 27, htxt, 16, True, "#fff", "middle")
    rows = [
        ("P0", "干净基准轮：零诊断（只有常驻黑匣子），全速跑", "p50~p99.99 真值，过 replay parity 校验", "唯一当真值", GREEN, GREEN_BG, GREEN_BD),
        ("P1/P2", "on-CPU 挖瓶颈：perf record -F99 --call-graph fp、perf stat 分组、PEBS/VTune", "热点比例、IPC/cache/branch（只取相对占比）", "只看相对", BLUE, BLUE_BG, BLUE_BD),
        ("P3/P4", "阻塞与内核：off-CPU 火焰图、perf sched、function_graph 短窗、timerlat/hwlat soak", "锁等待、调度、fault、IRQ、硬件噪声", "只看相对", BLUE, BLUE_BG, BLUE_BD),
        ("P5", "加压/注入：倍速回放、指定 seq 注入 fault/缺口/重复包、噪声机、netem、过夜 soak", "把 1/几万 的罕见根因喂成 1/几百，高频复现", "实验轮", ORANGE, ORANGE_BG, ORANGE_BD),
    ]
    y = 144
    rh = 116
    for tag, cfg, out, trust, c, bg, bd in rows:
        s.rect(44, y, 1192, rh, bg, bd, 1.6, rx=9)
        s.text(109, y + 60, tag, 22, True, c, "middle")
        s.text(194, y + 48, cfg, 15)
        s.text(194, y + 84, out, 15)
        s.text(1175, y + 64, trust, 15, True, c, "middle")
        y += rh + 10
    s.text(44, 668, "尾部数字只信 P0；回放无法重现 cache 冷热、真实网络与云邻居，上线前仍需小流量 canary + 常驻黑匣子复核",
            15, color=MUTED)
    s.save("jitter-09-replay-matrix")


# ---------------------------------------------------------------- fig 10
def fig_cloud_path():
    s = Svg(1280, 680, "上了公有云，先要想清楚自己买到的是几级独占",
            "云上的目标从“消灭抖动”退化为：量化它 → 分清我的问题还是云的问题 → 用选型规避")
    steps = [
        ("t 系列突发型", "可超卖、credit 限速、可跨核重调度", "低延迟直接排除", RED_BD),
        ("固定性能 VM", "vCPU = 专用物理超线程，不抢核", "仍有 SMT 兄弟干扰 + hypervisor", ORANGE_BD),
        ("固定 VM + 关 SMT", "启动参数 ThreadsPerCore=1，价格不变", "1 vCPU = 1 整个物理核，性价比甜点", BLUE_BD),
        ("Dedicated Host / Instance", "整机单租户（合规 / BYOL 为主）", "hypervisor 仍在，对抖动增量有限", PURPLE_BD),
        (".metal 裸金属", "OS 直跑硬件，无 hypervisor", "无 steal/VM-Exit，C/P-state、PMU 全可控", GREEN_BD),
    ]
    bx, bw, bh = 90, 200, 92
    centers_x = [bx + i * 225 + bw / 2 for i in range(5)]
    box_y = [520, 450, 380, 310, 240]
    for i, (name, fact, note, c) in enumerate(steps):
        x = bx + i * 225
        y = box_y[i]
        s.rect(x, y, bw, bh, "#ffffff", c, 2, rx=10)
        s.text(centers_x[i], y + 34, name, 16, True, anchor="middle")
        for j, ln in enumerate(_wrap(fact, 18)):
            s.text(centers_x[i], y + 60 + j * 19, ln, 12, color=MUTED, anchor="middle")
        s.text(centers_x[i], y - 14, note, 12, True, c, "middle")
        if i < len(steps) - 1:
            ax1, ay1 = x + bw, y
            ax2, ay2 = x + 225, box_y[i + 1] + bh
            s.path(f"M{ax1+6},{ay1-8} L{ax2-6},{ay2+8}", color=BLUE, bw=2)
            s.raw(f'<path d="M{ax2-16},{ay2+2} l12,2 l-6,11 z" fill="{BLUE}"/>')
    # axes
    s.line(70, 560, 70, 150, INK, 1.8)
    s.raw(f'<path d="M64,162 L70,146 L76,162 Z" fill="{INK}"/>')
    s.text(46, 176, "抖动控制力", 14, True)
    s.text(46, 198, "（成本也越高）", 12, color=MUTED)
    s.line(70, 614, 1230, 614, INK, 1.8)
    s.text(1230, 642, "独占级别 →", 14, True, anchor="end")
    s.rect(200, 104, 880, 40, GRAY_BG, GRAY_BD, 1.4, rx=9)
    s.text(640, 130, "即使在普通 VM：rdtsc 通常透传可用；rdpmc/PT 要看机型和权限——vPMU 缺失时快路径直接落入 2~9µs 的 read()",
           13, color=MUTED, anchor="middle")
    s.save("jitter-10-cloud-exclusive-path")


def _wrap(text_: str, n: int) -> list[str]:
    out, line = [], ""
    for ch in text_:
        line += ch
        if len(line) >= n and ch in " ，、":
            out.append(line.strip())
            line = ""
    if line.strip():
        out.append(line.strip())
    return out


if __name__ == "__main__":
    fig_tail_profile()
    fig_sampling_miss()
    fig_coordinated_omission()
    fig_three_layers()
    fig_flight_recorder()
    fig_head_of_line()
    fig_decision_tree()
    fig_halt_gap()
    fig_replay_matrix()
    fig_cloud_path()
