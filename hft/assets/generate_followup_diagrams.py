#!/usr/bin/env python3
"""Regenerate the SVG and PNG figures for HFT Engineering 06–09."""
from pathlib import Path
from html import escape
import subprocess
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent
BLUE = '#2563eb'
GRAY = '#6b7280'
GREEN = '#059669'
ORANGE = '#ea580c'


class Diagram:
    def __init__(self, width, height, title, subtitle):
        self.width, self.height = width, height
        self.lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
        self.lines.append('<style>text {font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif; fill:#111827;}</style>')
        self.lines.append('<defs>')
        for name, color in [('blue', BLUE), ('gray', GRAY), ('green', GREEN), ('orange', ORANGE)]:
            self.lines.append(f'<marker id="{name}" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto"><path d="M0,0 L9,3.5 L0,7 Z" fill="{color}"/></marker>')
        self.lines.append('</defs>')
        self.rect(0, 0, width, height, '#ffffff', 'none', 0)
        self.text(48, 48, title, 28, weight=600)
        self.text(48, 80, subtitle, 17, color=GRAY)

    def rect(self, x, y, w, h, fill='#ffffff', stroke='#d1d5db', radius=10, dash=None):
        extra = f' stroke-dasharray="{dash}"' if dash else ''
        self.lines.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{extra}/>')

    def text(self, x, y, label, size=18, color='#111827', weight=400, anchor='start'):
        self.lines.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" style="fill:{color}">{escape(str(label))}</text>')

    def box(self, x, y, w, h, title, details=(), fill='#eff6ff'):
        self.rect(x, y, w, h, fill)
        self.text(x+w/2, y+35, title, 21, weight=600, anchor='middle')
        for i, detail in enumerate(details):
            self.text(x+w/2, y+65+i*27, detail, 16, color=GRAY, anchor='middle')

    def arrow(self, pts, label=None, lx=None, ly=None, color='blue', dashed=False):
        colors = {'blue':BLUE, 'gray':GRAY, 'green':GREEN, 'orange':ORANGE}
        path = 'M ' + ' L '.join(f'{x},{y}' for x, y in pts)
        dash = ' stroke-dasharray="6 5"' if dashed else ''
        self.lines.append(f'<path d="{path}" fill="none" stroke="{colors[color]}" stroke-width="2.3" marker-end="url(#{color})"{dash}/>')
        if label:
            assert lx is not None and ly is not None
            width = sum(16 if ord(c)>255 else 8.5 for c in label)+16
            self.rect(lx-width/2, ly-18, width, 26, '#ffffff', 'none', 3)
            self.text(lx, ly, label, 16, color=colors[color], anchor='middle')

    def note(self, y, lines):
        self.rect(48, y, self.width-96, 30+30*len(lines), '#f9fafb', '#e5e7eb')
        for i, line in enumerate(lines):
            self.text(66, y+30+30*i, line, 17, color=GRAY)

    def save(self, folder, name):
        self.lines.append('</svg>')
        path = ROOT/folder/f'{name}.svg'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(self.lines)+'\n')
        with TemporaryDirectory(prefix='hft-svg-check-') as temp:
            subprocess.run(['rsvg-convert', str(path), '-o', str(Path(temp)/'check.png')], check=True)
        subprocess.run(['rsvg-convert', '-w', '1920', str(path), '-o', str(path.with_suffix('.png'))], check=True)
        print(path.relative_to(ROOT))


def kernel_path():
    d = Diagram(1360, 800, '普通 socket：报文经过哪里，谁来处理', '常规 DMA 网卡、复制式收发；蓝色箭头表示处理顺序，并不表示每一段都复制载荷。')
    xs, w = [48, 316, 584, 852, 1120], 192
    d.text(48, 133, '接收：UDP 行情 → 应用', 22, weight=600)
    nodes = [
        ('网卡接收', ['帧进入 RX 队列', '使用接收描述符']),
        ('主机接收缓冲', ['DMA 写入载荷', '描述符记录地址']),
        ('驱动 / NAPI', ['CPU 按预算收包', '建立报文元数据']),
        ('IP / UDP', ['协议检查与分流', '排入 socket 队列']),
        ('应用 recvmsg', ['复制到用户缓冲', '解析业务消息']),
    ]
    for x, (title, details) in zip(xs, nodes): d.box(x, 160, w, 130, title, details)
    for i, label in enumerate(['DMA', '取包', '协议', '读取']):
        d.arrow([(xs[i]+w,225),(xs[i+1],225)], label, (xs[i]+w+xs[i+1])/2, 210)
    d.text(48, 325, '中断通知 → 调度 NAPI；应用已在运行时，也可以直接读取已有数据。', 17, color=GRAY)
    d.text(48, 393, '发送：应用订单 → TCP → 网卡', 22, weight=600)
    nodes = [
        ('应用 sendmsg', ['用户缓冲区', '处理部分接受 / 重试']),
        ('TCP 内核缓冲', ['复制已接受字节', '保留重传能力']),
        ('IP / qdisc', ['路由与邻居解析', '调度发送或排队']),
        ('驱动 TX 队列', ['提交 DMA 描述符', '等待完成后回收']),
        ('网卡发送', ['DMA 读取载荷', '报文进入线路']),
    ]
    for x, (title, details) in zip(xs, nodes): d.box(x, 420, w, 130, title, details, '#f0fdf4')
    for i, label in enumerate(['复制', '发送', '提交', 'DMA']):
        d.arrow([(xs[i]+w,485),(xs[i+1],485)], label, (xs[i]+w+xs[i+1])/2, 470)
    d.note(606, ['描述符环存放地址、长度与状态；报文载荷放在描述符引用的缓冲区。',
                 'send 返回：应用可复用已接受部分的原缓冲区；TCP 确认与交易所订单回报随后发生。',
                 '阻塞读取或 epoll 等待结束后，线程还要获得 CPU；队列积压与调度会改变延迟。'])
    d.save('hft-06', 'kernel-data-path')


def xdp_ownership():
    d = Diagram(1280, 850, 'AF_XDP：四类环交接的是缓冲区所有权', '单个 socket / 队列的基本流程。UMEM 存放载荷，环中的描述符引用 UMEM 内的 frame。')
    d.text(180, 127, '应用', 23, weight=600, anchor='middle')
    d.text(640, 127, '共享环', 23, weight=600, anchor='middle')
    d.text(1100, 127, '内核 / 驱动', 23, weight=600, anchor='middle')
    rows = [
        (160, '交出空闲 frame', 'FILL', 'addr', '接收侧取得 frame', True),
        (285, '取得已收报文', 'RX', 'addr + len', '写完接收数据', False),
        (410, '填好待发 frame', 'TX', 'addr + len', '取得待发数据', True),
        (535, '收回 frame，可复用', 'COMPLETION', 'addr', '处理完 TX 描述符', False),
    ]
    for y, app, ring, detail, kernel, forward in rows:
        d.box(48,y,264,92,app,fill='#eff6ff')
        d.box(496,y,288,92,ring,[detail],fill='#faf5ff')
        d.box(968,y,264,92,kernel,fill='#f0fdf4')
        if forward:
            d.arrow([(312,y+46),(496,y+46)],'发布',404,y+30)
            d.arrow([(784,y+46),(968,y+46)],'消费',876,y+30)
        else:
            d.arrow([(968,y+46),(784,y+46)],'发布',876,y+30)
            d.arrow([(496,y+46),(312,y+46)],'消费',404,y+30)
    d.note(671,['应用消费 RX 后，可将 frame 重新交给 FILL，或填入 TX；同一 frame 同时只由一方持有。',
                '消费 TX 描述符不等于交还 frame；应用读取 COMPLETION 后才可复用。',
                'COMPLETION 也会回收被拒绝的无效描述符，因此只据它判断所有权归还。',
                '零拷贝模式由网卡直接访问 UMEM；复制模式仍遵循上述交接规则。'])
    d.save('hft-06', 'af-xdp-ownership')


def components():
    d = Diagram(1280, 900, '一条事件的编码、传输与记录', '上方展示 Aeron IPC 的同机共享内存数据路径；下方两种记录方案按用途选择。')
    d.box(48,145,280,145,'C++ 行情接入',['规范化事件 → SBE 编码','Aeron C++ Wrapper → C API'])
    d.box(500,145,280,145,'Aeron log buffer',['发布位置 / 订阅位置','共享内存中的 term'])
    d.box(952,145,280,145,'C++ 策略 / 网关',['Aeron C++ Wrapper → C API','poll → 解码 → 业务处理'])
    d.arrow([(328,210),(500,210)],'offer / commit',414,193)
    d.arrow([(780,210),(952,210)],'poll',866,193)
    d.box(48,370,280,120,'Media Driver',['创建、管理 log 与连接','IPC 载荷由客户端读写'], '#fff7ed')
    d.arrow([(328,425),(416,425),(416,320),(580,320),(580,290)],'管理',416,353,'orange',True)
    d.box(500,530,280,140,'Aeron Archive',['JVM 记录服务','订阅事件并写 recording','按 position 重放'], '#f0fdf4')
    d.arrow([(640,290),(640,530)],'订阅记录流',640,443)
    d.box(952,530,280,140,'Chronicle 记录服务',['JVM 进程接收选定事件','appender → queue 文件','tailer 按 index 读取'], '#f0fdf4')
    d.arrow([(1092,290),(1092,530)],'业务事件',1092,443)
    d.note(714,['跨机 UDP：发送客户端 → 本地 log → Driver Sender → UDP → 对端 Driver Receiver → 接收 log。',
                'Archive 与 Chronicle Queue 各自保留记录位置；业务检查点还要保存状态与已处理事件。',
                '诊断日志从业务线程进入异步日志队列，独立消费；它不承担交易状态恢复。'])
    d.arrow([(56,855),(100,855)],color='blue')
    d.text(112,862,'事件数据',16,color=GRAY)
    d.arrow([(276,855),(320,855)],color='orange',dashed=True)
    d.text(332,862,'管理控制',16,color=GRAY)
    d.save('hft-07', 'component-flow')


def archive_replay():
    d = Diagram(1280, 780, 'Archive：从业务检查点追到实时流', 'position 是原始流中的字节位置；选择兼容的 session、term 参数与衔接位置。')
    d.box(48,145,300,125,'恢复起点',['载入业务状态 S(P₀)','从已处理位置 P₀ 继续读取'])
    d.box(490,145,300,125,'历史重放',['Archive 读取 recording','业务按顺序应用事件'])
    d.box(932,145,300,125,'实时订阅',['跟上同一原始流的位置','衔接后停止重放订阅'])
    d.arrow([(348,207),(490,207)],'P₀',419,192)
    d.arrow([(790,207),(932,207)],'追赶 / 合并',861,192)
    d.text(48,335,'同一条流的三个进度',22,weight=600)
    for y, end, title, fill in [(405,720,'业务已应用的位置 P_app','#dbeafe'),(475,925,'Archive 已记录的位置 P_rec','#dcfce7'),(545,1180,'实时发布位置 P_live','#ede9fe')]:
        d.text(48,y,title,18)
        d.rect(398,y-22,end-398,28,fill,'none',5)
    d.arrow([(398,590),(1200,590)],'原始流 position 增长',800,613)
    d.note(650,['记录位置前进表示 Archive 完成相应记录写入；存储同步行为由 fileSyncLevel 配置决定。',
                '合并时保持原始流连续并处理重叠；重放字节到达后，业务仍要按自己的恢复游标应用。'])
    d.save('hft-07', 'archive-replay')


def snapshot_handoff():
    d = Diagram(1280, 860, '快照和增量怎样接成同一本订单簿', '以 Binance Spot diff-depth 为例：L 是快照水位；U、u 是一条增量覆盖的首末更新编号。')
    d.box(48,150,300,140,'先订阅并缓存',['持续接收增量区间','[100,102]、[103,104]…'])
    d.box(490,150,300,140,'取得快照',['安装快照中的价位数量','lastUpdateId：L = 100'])
    d.box(932,150,300,140,'寻找衔接事件',['丢弃 u ≤ L 的旧事件','第一条覆盖 L + 1 = 101'])
    d.arrow([(348,220),(490,220)],'同时请求',419,203)
    d.arrow([(790,220),(932,220)],'对齐水位',861,203)
    d.text(48,359,'从快照水位向前推进',23,weight=600)
    headers = [(48, '输入'), (365,'检查'), (770,'本地簿与可发布状态')]
    for x,t in headers: d.text(x,408,t,18,weight=600)
    rows = [
        ('[100,102]', '100 ≤ 101 ≤ 102，覆盖缺少的起点', '应用绝对数量；本地 u = 102'),
        ('[103,104]', 'U = 103，接上本地 102', '继续应用；本地 u = 104'),
        ('[100,102] 再次到达', '末尾 u ≤ 本地 104，已处理', '丢弃重复，簿保持不变'),
        ('[107,108]', 'U > 104 + 1，缺少 105、106', '停止发布；重新获取快照并衔接'),
    ]
    for i,(a,b,c) in enumerate(rows):
        y = 430+i*70
        d.rect(48,y,1184,58,'#f9fafb' if i%2==0 else '#ffffff','#e5e7eb',4)
        for x,t in [(64,a),(365,b),(770,c)]: d.text(x,y+36,t,18)
    d.note(741,['每档数量是更新后的绝对值；数量为 0 表示删除该价位。',
                '图中编号用于验算衔接。CME 的事件边界和 ITCH 的订单级更新按各自协议处理。'])
    d.save('hft-08','snapshot-handoff')


def order_recovery():
    d = Diagram(1280, 875, '断线恢复：订单台账与行情簿分别恢复', '订单请求发出后连接断开，先保留“结果未知”；它仍可能已被接受或成交。')
    d.box(420,135,440,110,'订单请求结果未知',['保留客户端标识、原始请求与在途风险'], '#fff7ed')
    d.box(80,350,440,130,'恢复私有订单与成交',['续接会话 / 补回报 / 查询核对','按订单和成交标识去重、更新累计成交'])
    d.box(760,350,440,130,'恢复公开行情',['补缺口或重新衔接快照与增量','重建到协议允许发布的完整状态'])
    d.arrow([(530,245),(530,296),(300,296),(300,350)],'核对请求结果',300,313)
    d.arrow([(750,245),(750,296),(980,296),(980,350)],'核对行情连续性',980,313)
    d.box(420,605,440,110,'两项检查同时通过',['订单台账一致，且行情可用于决策','重新开放相应策略的发单能力'], '#f0fdf4')
    d.arrow([(300,480),(300,550),(530,550),(530,605)],'订单已核对',300,525)
    d.arrow([(980,480),(980,550),(750,550),(750,605)],'行情已恢复',980,525)
    d.note(760,['恢复期间继续接收新成交；查询结果与实时回报按标识、版本或累计成交关系合并。',
                '无法确认的原请求保留风险占用；按该接口的幂等和查询规则决定是否重发。'])
    d.save('hft-08','order-recovery')


def dual_time():
    d = Diagram(1280, 820, '一次成交发生后，策略何时知道', '合成案例：买价 100、委托 20 手。时间单位为 µs；每行连接同一事件的发生与可见时刻。')
    d.box(48,128,480,105,'交易所侧',['真实订单簿 + 交易所订单状态'])
    d.box(752,128,480,105,'本地策略侧',['已收到的行情簿 + 已收到的订单回报'])
    rows = [
        ('0：产生初始行情', '30：策略收到行情', '行情延迟'),
        ('60：买单挂入队尾', '80：收到订单接受回报', '回报延迟'),
        ('90：成交 5，剩余 15', '110：本地得知成交 5', '回报延迟'),
        ('130：再成交 10，剩余 5', '150：本地累计得知成交 15', '回报延迟'),
        ('140：撤掉剩余 5', '160：本地确认撤单完成', '回报延迟'),
    ]
    for i,(ex,local,label) in enumerate(rows):
        y=270+i*76
        d.rect(48,y,480,52,'#f0fdf4','#d1d5db',6)
        d.rect(752,y,480,52,'#eff6ff','#d1d5db',6)
        d.text(68,y+34,ex,21)
        d.text(772,y+34,local,21)
        d.arrow([(528,y+26),(752,y+26)],label,640,y+9)
    d.note(690,['本地下单：40 发出 → 60 到达；本地撤单：120 发出 → 140 生效。',
                '120—140 期间，原单仍参与撮合；130 的额外成交先发生，150 才被策略观察到。',
                '模拟器保留两侧各两份状态，按事件交付时间更新，策略只访问本地已知状态。'])
    d.save('hft-09','dual-timeline')


def backtest_engine():
    d = Diagram(1280, 890, '回测引擎：同一份策略，替换事件来源与执行环境', '虚拟调度器推进时间；公开行情、请求到达、成交回报和计算完成都是带交付时间的事件。')
    d.box(48,140,280,125,'事件源',['历史报文 / 规范化事件','共用实盘解码器'])
    d.box(500,140,280,125,'虚拟事件调度器',['按交付时间与顺序取事件','计算忙碌时继续排队'])
    d.box(952,140,280,125,'同一份策略代码',['行情 / 回报 / 定时器输入','下单 / 撤单 / 改单输出'])
    d.arrow([(328,202),(500,202)],'带时间的事件',414,184)
    d.arrow([(780,202),(952,202)],'可见事件',866,184)
    d.box(500,470,280,145,'模拟交易所',['撮合规则 + 队列模型','真实簿与交易所订单状态','产生订单与成交回报'], '#f0fdf4')
    d.box(952,470,280,145,'延迟与计算模型',['请求何时到达','回报何时可见','策略何时完成计算'], '#fff7ed')
    d.arrow([(1092,265),(1092,470)],'委托请求',1092,370)
    d.arrow([(952,540),(780,540)],'请求到达',866,523)
    d.arrow([(560,265),(560,470)],'背景市场事件',560,335)
    d.arrow([(720,470),(720,265)],'安排延迟回报',720,405)
    d.box(48,470,280,145,'订单与成交账本',['成交、手续费、持仓','冻结参数与随机种子','输出可比较的事件记录'], '#faf5ff')
    d.arrow([(500,540),(328,540)],'状态变化',414,523)
    d.note(695,['实盘适配器用真实行情和网关代替模拟环境；策略接口保持相同。',
                '按日或按标的并行前，先确认没有跨日持仓、跨标的对冲或共享资金依赖。',
                '固定输入、事件排序与随机种子，再比较逐笔账本；参数搜索复用这些确定的边界。'])
    d.save('hft-09','engine-components')


def market_response():
    d = Diagram(1280, 1020, '新增订单之后，后续市场事件从哪里来', '固定历史保留已记录的背景事件；交互模拟让背景参与者根据收到的信息产生新动作。')
    d.text(48,135,'固定历史回放',23,weight=600)
    d.box(48,170,300,125,'历史事件文件',['既有挂单、撤单、成交','时间和内容已经确定'])
    d.box(490,170,300,125,'模拟订单簿',['应用历史事件','加入我方委托与撮合状态'])
    d.box(932,170,300,125,'我方策略',['收到行情与回报','产生新的委托请求'])
    d.arrow([(348,232),(490,232)],'顺序回放',419,214)
    d.arrow([(790,232),(932,232)],'延迟交付',861,214)
    d.arrow([(1092,295),(1092,350),(640,350),(640,295)],'订单到达',866,340)
    d.text(48,397,'新增订单会改变模拟簿；历史文件中其他参与者的后续动作仍按原记录到达。',18,color=GRAY)
    d.text(48,465,'有背景参与者响应的交互模拟',23,weight=600)
    d.box(48,510,300,135,'我方策略',['新增挂单或主动吃单','按真实发送延迟到达'])
    d.box(490,510,300,135,'模拟交易所',['更新可见深度与成交','向参与者发布新状态'], '#f0fdf4')
    d.box(932,510,300,135,'背景参与者',['收到行情后计算','根据模型决定新动作'], '#fff7ed')
    d.arrow([(348,577),(490,577)],'委托请求',419,558)
    d.arrow([(790,577),(932,577)],'行情延迟',861,558)
    d.arrow([(1092,645),(1092,730),(640,730),(640,645)],'新的挂单、撤单或吃单',865,718,color='orange')
    d.arrow([(490,610),(422,610),(422,780),(198,780),(198,645)],'新行情与回报',306,768,color='green')
    d.note(855,['响应从背景参与者收到信息之后发生，其网络与计算延迟也进入虚拟事件队列。',
                '行为模型决定响应方向和强度，仍需用市场数据校准；交互本身不保证响应真实。',
                'Queue-Reactive 等状态相关模型直接由簿状态生成订单流；多参与者模型还能表示不同观察与行为。'])
    d.save('hft-09','market-response')


def fifo_queue():
    d = Diagram(1280, 800, 'FIFO 队列：前方数量怎样消耗', '同价位按到达顺序成交；A、B 是先到订单，我方原始委托为 20 手。')
    d.text(48,130,'事件',19,weight=600)
    d.text(450,130,'从队首到队尾',19,weight=600)
    d.text(1040,130,'我方累计成交',19,weight=600)
    rows = [
        ('60：我方入队',[('A 40',40,'#dbeafe'),('B 20',20,'#ede9fe'),('我方 20',20,'#dcfce7')],0),
        ('70：主动卖出 30',[('A 10',10,'#dbeafe'),('B 20',20,'#ede9fe'),('我方 20',20,'#dcfce7')],0),
        ('75：B 撤掉 20',[('A 10',10,'#dbeafe'),('我方 20',20,'#dcfce7')],0),
        ('90：主动卖出 15',[('我方 15',15,'#dcfce7')],5),
        ('130：主动卖出 10',[('我方 5',5,'#dcfce7')],15),
        ('140：撤掉剩余 5',[],15),
    ]
    for i,(event,blocks,filled) in enumerate(rows):
        y=160+i*83
        d.text(48,y+32,event,20)
        x=450
        for label,qty,fill in blocks:
            width=max(95,qty*5)
            d.rect(x,y,width,48,fill,'#d1d5db',5)
            d.text(x+width/2,y+31,label,18,anchor='middle')
            x+=width+8
        if not blocks: d.text(450,y+32,'订单退出队列',19,color=GRAY)
        d.text(1100,y+32,str(filled),23,weight=600,anchor='middle')
    d.note(690,['按订单数据可识别 B 位于我方前面；按价位汇总的数据只显示总量减少，撤单位置由模型估计。',
                '图中块宽只辅助比较数量；每次撮合先消耗队首，剩余主动数量才轮到我方。'])
    d.save('hft-09','fifo-queue')


if __name__ == '__main__':
    kernel_path()
    xdp_ownership()
    components()
    archive_replay()
    snapshot_handoff()
    order_recovery()
    dual_time()
    backtest_engine()
    fifo_queue()
    market_response()
