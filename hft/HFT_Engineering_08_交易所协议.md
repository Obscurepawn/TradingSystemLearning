# HFT Engineering 08：交易所协议——从报文字节到订单簿与断线恢复

一笔限价买单已经发出，连接随即断开。公开行情仍在刷新，卖一价上的数量少了两手。自己的买单成交了吗？重新发送会不会多买一笔？此时能回答这些问题的，是私有订单回报、成交记录和请求标识；公开行情只能说明市场发生了变化。

文中的短序号、价格和数量用于逐步演算，协议字段、更新规则和恢复条件来自篇末规范。

## 1. 一条交易链上的参与者

在传统证券和期货市场，客户程序经券商或期货公司的柜台完成账户检查、资金与持仓检查，再由机构网关向交易所提交请求。具备相应资格和接入权限的参与方，可以使用交易所的原生协议。综合交易平台（Comprehensive Transaction Platform，CTP）则是期货公司部署的柜台及其客户端接口体系，客户调用 CTP 时面对的是柜台服务。

```text
公开行情：交易所行情系统 ──→ 直连行情接收程序 ──→ 本地市场订单簿
                       └─→ 行情供应商/柜台 ──→ 客户程序

交易请求：策略 ──→ 订单管理与风控 ──→ 券商/期货公司柜台 ──→ 交易所网关 ──→ 撮合
私有回报：策略 ←── 自己的订单台账 ←───────────────────────────┘
独立监督：风险监控 ←── Drop Copy 接收方 ←── 私有回报副本
恢复数据：行情快照、缺口补发、订单查询、成交查询 ──→ 各自的状态重建器
```

订单管理系统（Order Management System，OMS）负责记录订单意图、请求、回报和成交，并限制策略当前能采取的动作。它至少保存两类状态：市场订单簿描述公开的买卖盘，订单台账描述本账户的订单、成交和未决请求。公开订单 ID 与自己的客户订单 ID 属于不同命名空间，只有协议明确提供映射时才能关联。

Drop Copy 是私有回报的独立副本接收服务，供风险监督和对账使用。它有自己的权限、延迟和连接状态。主交易进程失去连接时，独立接收方可能还在工作，因此它能为恢复提供额外证据。

行情授权、交易资格和账户操作权限分别控制不同连接。下载某个软件开发工具包（Software Development Kit，SDK）后，只是获得了调用接口的程序库；账户、前置地址、密钥、会话和产品权限仍由服务提供方开通。Binance、Bybit 在下文均指中心化加密资产交易所，其公开行情与账户私有流同样分开授权。

## 2. 从一段字节读出一条消息

网络接收缓冲区保存线上格式，即协议规定的字节排列；SDK 回调或解码器暴露字段；接入程序再把字段转换成内部事件。三者可以表达同一笔数量变化，却有不同的内存布局和生存期。

```text
接收缓冲区                     解码字段                         内部状态
字节、偏移、长度、大小端 ──→ 价格=100.25、数量=7 ──→ 卖盘[100.25] = 7
          ↑                         ↑                             ↑
    包/消息边界校验             模板与精度转换                 顺序、去重、发布
```

用 `reinterpret_cast<MyMessage*>(buffer)` 直接读取线上数据，会把编译器的对齐填充、主机大小端和对象生命周期都带进解析过程。即使字段名称相同，C++ 结构体的 `sizeof` 也可能大于线上的消息长度。解码器应按协议读取指定宽度，再转换成内部的整数价格、数量和标识。若解码器只引用接收缓冲区，缓冲区复用前就要完成消费或复制。

用户数据报协议（User Datagram Protocol，UDP）保留数据报边界，由应用处理缺口与重复；传输控制协议（Transmission Control Protocol，TCP）提供可靠的有序字节流，由应用识别消息边界。三种常见封装如下。图中的偏移均从应用层载荷开始，不包含以太网、IP、UDP 或 TCP 头。

```text
Binance WebSocket 消息
  {"stream":"btcusdt@depth","data":{ "U":100,"u":102,"b":[...],"a":[...] }}
  └──── 可选的组合订阅外层 ────┘     └──────── JSON 业务字段 ──────────┘

Nasdaq MoldUDP64 数据报
  [Session:10][Sequence:8][Count:2] [Length:2][ITCH A:36] [Length:2][ITCH X:23]
  └────────── 20 字节包头 ────────┘ └──── 消息 N ─────┘ └──── 消息 N+1 ────┘

CME MDP 3.0 数据报
  [MsgSeqNum:4][SendingTime:8] [MsgSize:2][SBE header:8][根字段][重复组...]
  └────── 12 字节包头 ───────┘ └──────────────── 消息 1 ─────────────────┘
                             [MsgSize:2][SBE header:8][根字段][重复组...]
                             └──────────────── 消息 2 ─────────────────┘
```

JSON（JavaScript Object Notation）是按字段名组织的文本格式，键的先后顺序与空白都可改变，因此 `b` 的位置没有固定字节偏移。WebSocket 库交付完整消息后，应用再解析 JSON；组合订阅多一层 `stream/data`，原始订阅直接给业务对象。[1]

MoldUDP64 的包头保存十字节会话名、八字节首条消息序号和两字节消息数，整数使用大端。一个包含三条消息、首序号为 700 的包覆盖 700、701、702；下一个消息序号是 703。消息数为 0 时表示心跳，为 65535 时表示会话结束；解析器为它们单独设置分支。[2]

CME 的市场数据平台（Market Data Platform，MDP）3.0 以包序号检测频道缺口。`MsgSize` 包含自身和 SBE 头；SBE 是简单二进制编码（Simple Binary Encoding），其 schema 描述字段类型、偏移和重复组。CME 使用小端。`BlockLength` 只给根字段块长度，重复组位于其后；`TemplateID/SchemaID/Version` 一起决定按哪份定义解码。[3]

TCP 提供有序字节流，应用仍要组装消息。Nasdaq SoupBinTCP 每个包先放两字节大端长度，长度计算后面的类型字节与载荷，不包含这两个长度字节。一次 `recv()` 既可能只收到半条消息，也可能收到多条。解析器先等到长度字段，再等足载荷，消费后继续读取下一条。[4]

### 2.1 价格、时间和序号分别属于谁

以 Nasdaq 的四位小数价格为例，线上整数 `1002500` 表示 `100.2500`；CME 的 `PRICENULL9` 以八字节有符号整数尾数乘 `10^-9` 得到价格，固定指数由 schema 给定，不再在线上传一个指数。价格转换要先识别空值，再做缩放。JSON 的 `"100.25000000"` 也适合直接转成定点整数：根据标的最小变动价位检查整除关系，比先转 `double` 再四舍五入更容易保持一致。

时间字段同样有自己的单位和起点。ITCH 的六字节时间戳是自当天午夜开始的纳秒数，日期来自交易会话；CME `TransactTime` 是自 Unix 纪元开始的纳秒数；Binance JSON 流默认使用毫秒，并允许通过 `timeUnit=MICROSECOND` 请求微秒。把三者统一存进 `uint64_t` 后，还要保存时钟来源和单位，否则一个“时间更大”的比较就会排错事件。[1][5][6]

序号的作用取决于计数对象。Mold 数消息，CME 包头数数据报，CME `RptSeq` 数某个标的的更新，Binance `U/u` 给出一个更新区间。`MatchEventIndicator` 则标记一组撮合结果是否结束，它不负责填补序号缺口。内部事件最好保留这些原始标识，再附加本地接收序号，用于回放和定位故障。

金融信息交换协议（Financial Information eXchange，FIX）同时涉及交易业务语义与会话管理。经典 tag-value 编码用 `35=D` 表示新订单单笔请求，字段间由 SOH 字节分隔；FIX 性能协议（FIX Performance Protocol，FIXP）负责建立连接上的有序业务会话，CME iLink 将它与 SBE 业务消息组合使用。名称中出现 `Binary` 只说明采用二进制表示，具体字段和承载方式仍由场所规范决定。

| 标识 | 关联对象 | 接入程序保存什么 |
| --- | --- | --- |
| `MsgSeqNum` | 某方向、某会话内的消息位置 | 会话身份、方向、重置周期、连续接收位置 |
| `ClOrdID` | 客户给订单请求分配的身份 | 原始意图、改单/撤单链、协议规定的唯一性范围 |
| `OrderID` | 场所分配的订单身份 | 场所、账户、交易日等共同组成的定位键 |
| `ExecID` | 某条执行报告的身份 | 由场所定义的执行身份；实际成交再关联交易/成交 ID |

`ExecID` 在 FIX 中标识执行报告，范围包括接受、撤单等非成交事件。只有成交事件才增加持仓，且应按该场所的成交身份去重。会话重放上的重复标志与业务去重一起使用：重放过来的同一笔成交，不能再次增加仓位。[7]

FAST（FIX Adapted for STreaming）通过模板、字典和 copy、delta 等字段算子压缩消息。若某字段沿用前值，后续消息省略它也能解码，因为接收方保存了字典状态。依赖跨消息字典的流丢包后，要补齐缺失消息或按规范进入字典重置点，再继续解码；采用独立包字典或频繁重置的配置，恢复边界则不同。SBE 的固定字段解码没有这层跨消息字典依赖，业务订单簿仍然依赖连续事件。[8]

会话心跳检查连接是否还能交换消息。长时间无消息时发送 `TestRequest`，对端通过携带相应标识的心跳响应，超时后断开连接。心跳间隔与断开条件属于所选会话协议；行情是否足够新、订单请求是否已经有结果，另由业务计时器判断。

## 3. 行情怎样变成本地订单簿

按价位汇总的订单簿称为 Market by Price，简称 MBP：同一侧、同一价格的数量合并保存。逐笔订单簿称为 Market by Order，简称 MBO：先按订单 ID 保存剩余量，再聚合成价位。快照给出某个时点的状态，增量说明随后如何修改；接入程序的第一项工作是找到两者之间可验证的交接位置。

| 行情实例 | 快照如何到达 | 覆盖范围与交接字段 |
| --- | --- | --- |
| Binance Spot diff depth | 先缓存 WebSocket，再经 REST 请求快照 | 单标的，每侧最多 5,000 档；`lastUpdateId` 与 `U/u` 衔接 |
| Bybit V5 50 档 | 订阅后先推 snapshot，随后推 delta | 收到新 snapshot 替换旧簿；`u=1` 表示服务重启后的快照 |
| CME MDP 3.0 MBP | 独立 Market Recovery 通道循环发送 | 每个标的的 `RptSeq/TransactTime` 与频道水位 `LastMsgSeqNumProcessed` |
| Nasdaq GLIMPSE | 独立 SoupBinTCP 连接发送现存显示订单 | 末尾 `G` 消息指示开始消费实时 ITCH 的序号 |

Bybit 的 `seq` 用于比较不同深度流中数据产生的先后，不能自行假定相邻推送恰好加一。其 Linear、Inverse、Spot 的 Level 1 只发 snapshot；`cts` 是撮合引擎生成该簿数据的时间，`ts` 是系统生成推送数据的时间。选择恢复规则时，要读该产品与该深度的定义。[9]

### 3.1 Binance：一个价位的新数量

下面使用单标的的两档快照。`bids` 从高价到低价排列，`asks` 从低价到高价排列。REST（Representational State Transfer，表述性状态转移）是一种面向资源的接口设计风格，这里的 HTTP 请求用于取当前深度；WebSocket 持续交付更新。

```json
{
  "lastUpdateId": 100,
  "bids": [["100.00", "5.00"], ["99.00", "8.00"]],
  "asks": [["101.00", "6.00"], ["102.00", "9.00"]]
}
```

请求快照之前就启动缓冲。否则快照在服务端形成以后、WebSocket 在本地开始接收以前的变化会消失，随后再快的解码器也补不回这段历史。

```json
{"e":"depthUpdate","E":1000,"s":"BTCUSDT","U":100,"u":102,
 "b":[["100.00","7.00"]],"a":[["100.50","3.00"]]}
```

这条消息覆盖更新 100—102。`b` 表示买盘修改，`a` 表示卖盘修改，里面的数量是该价位的新绝对量。因此买一数量变成 7，卖盘增加 `100.50 × 3`。把 7 累加到原有的 5 会得到 12，从第一条更新就把簿做错了。

```json
{"e":"depthUpdate","E":1001,"s":"BTCUSDT","U":103,"u":104,
 "b":[["99.00","0.00"]],"a":[["101.00","4.00"]]}
```

零量删除买盘 99.00，卖盘 101.00 的数量覆盖为 4。再收到 `[U=100,u=102]` 这条旧消息时，本地游标已经是 104，整条丢弃。如果下一条是：

```json
{"e":"depthUpdate","E":1002,"s":"BTCUSDT","U":107,"u":108,
 "b":[["100.00","6.00"]],"a":[]}
```

本地缺少 105、106，因此停止应用 107—108，并冻结该簿。缺少的更新可能删掉整个卖一价，当前事件只修改买盘也无法修复它。

| 输入 | 解码事件 | 应用后的簿，格式为“价格×数量” | 可发布状态 |
| --- | --- | --- | --- |
| 快照 `L=100` | 安装初始状态 | 买 `100×5, 99×8`；卖 `101×6, 102×9` | 等待交接 |
| `[100,102]` | 买 100 覆盖为 7；卖 100.5 覆盖为 3 | 买 `100×7, 99×8`；卖 `100.5×3, 101×6, 102×9` | 游标 102，可发布 |
| `[103,104]` | 删除买 99；卖 101 覆盖为 4 | 买 `100×7`；卖 `100.5×3, 101×4, 102×9` | 游标 104，可发布 |
| 重复 `[100,102]` | 旧事件，丢弃 | 保持原状 | 保持 104 |
| `[107,108]` | 缺少 105—106 | 旧簿保留供诊断，但不再供策略决策 | 重建中 |

![Binance 快照与增量的衔接](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/26f74936c47b06b0fd0e85e37020ed7a01484946/hft/assets/hft-08/snapshot-handoff.png)

初始化时，记下缓冲中第一条消息的 `U`。如果快照 `L` 小于它，就重新取快照；丢弃 `u <= L` 的旧事件后，按照官方初始化步骤核对首条剩余事件与快照的重叠关系。贯穿更新过程的连续性条件是：新事件覆盖下一条尚未处理的更新，即 `U <= local_id + 1 <= u`；`u <= local_id` 的完整旧事件可以丢弃，`U > local_id + 1` 则触发重建。本例的首条 `[100,102]` 同时覆盖快照位置 100 和下一位置 101。[1]

恢复时再建一个候选簿，缓存继续到来的增量，完成快照交接后才替换策略可见的簿。缓存也要有长度或时间上限；快照请求长期失败、缓存已经丢数据时，重新开始缓冲与取快照。

快照最多覆盖每侧 5,000 档，最初范围之外的价位只有发生更新后才逐渐被知道。这个范围适合记在簿的元数据中。另一个 `<symbol>@depth5/10/20` 接口持续提供有限深度的周期快照，消费方式是替换当前可见的 top N 状态，不沿用上面的 REST 初始化加 diff depth 流程。

### 3.2 CME：重复组、档位移动与完整撮合事件

CME 的同一条增量消息可以携带多个价位条目。接收程序先读根字段，再读重复组计数，最后逐条更新各个标的。为给出可复算的偏移，下面固定使用 `SchemaID=1, Version=9` 的 `MDIncrementalRefreshBook46`，其 XML 日期为 `20180308`，来自公开代码库保留的 CME schema。该版本用于字节演算；接入当前频道时，按频道启用的 XML 生成解码器，并以实际报文头校验版本。当前官方业务规范仍用于核对下述 New、Change、Delete 和事件恢复语义。[6][10]

用 E-mini S&P 500 期货的直接买卖盘说明档位行为；合约 ID、频道地址、深度和最小变动价位从当日配置及 Security Definition 取得。演算只展示买盘前三档；假定初始状态为 `5000.00×10, 4999.75×12, 4999.50×8`，其余可见档位继续按同一规则移动。

下表从 `MsgSize` 的第一个字节起算。给两个 MBP 条目、零个 MBO 条目时，消息共 `10 + 11 + 3 + 2×32 + 8 = 96` 字节。

| 偏移 | 长度 | 字段 | 类型/大小端、单位 | 解码值 | 用途 |
| --- | --- | --- | --- | --- | --- |
| 0 | 2 | `MsgSize` | uint16，小端，字节 | 96 | 找到下一条消息 |
| 2 | 2 | `BlockLength` | uint16，小端，字节 | 11 | 根字段块长度 |
| 4 | 2 | `TemplateID` | uint16，小端 | 46 | MBP/MBO 增量模板 |
| 6 | 2 | `SchemaID` | uint16，小端 | 1 | schema 身份 |
| 8 | 2 | `Version` | uint16，小端 | 9 | 本例版本 |
| 10 | 8 | `TransactTime` | uint64，小端，Unix 纳秒 | `T` | 事件开始处理时间 |
| 18 | 1 | `MatchEventIndicator` | 位图 | `0x00` | 此消息尚未结束事件 |
| 19 | 2 | 根块剩余字节 | 按 `BlockLength` 跳过 | — | 不自行压缩根块 |
| 21 | 2 | 组 `blockLength` | uint16，小端，字节 | 32 | 每个 MBP 条目的步长 |
| 23 | 1 | `numInGroup` | uint8 | 2 | MBP 条目数 |
| 24 | 32 | 第一个 MBP 条目 | 见下表 | New | 先应用 |
| 56 | 32 | 第二个 MBP 条目 | 见下表 | Change | 后应用 |
| 88 | 8 | MBO 组头 | `groupSize8Byte` | 条目长度 24，数量 0 | 数量在组头偏移 7，仍占完整 8 字节 |

MBP 条目里的偏移从该条目自身起算。`MDEntryType='0'` 是字符字节 `0x30`，而 `MDUpdateAction=0` 是数值字节 `0x00`。

| 条目偏移 | 长度 | 字段 | 类型、单位 | 第一个条目的解码值 | 更新含义 |
| --- | --- | --- | --- | --- | --- |
| 0 | 8 | `MDEntryPx` | `PRICENULL9`，小端 | `5000250000000 × 10^-9 = 5000.25` | 新价格 |
| 8 | 4 | `MDEntrySize` | int32，可空 | 6 | 该档新数量 |
| 12 | 4 | `SecurityID` | int32 | 合约 S | 标的 |
| 16 | 4 | `RptSeq` | uint32 | 51 | 标的更新位置 |
| 20 | 4 | `NumberOfOrders` | int32，可空 | 2 | 档位订单数 |
| 24 | 1 | `MDPriceLevel` | uint8 | 1 | 买一 |
| 25 | 1 | `MDUpdateAction` | uint8 枚举 | 0 | 插入 New |
| 26 | 1 | `MDEntryType` | char | `'0'` | 买盘 |
| 27 | 5 | 条目块剩余字节 | 按组长跳过 | — | 下条从 32 开始 |

价格空值尾数为 `9223372036854775807`，数量的 `Int32NULL` 空值为 `2147483647`。先判断空值，随后按动作解释字段。尤其删除动作应按侧别和档位删除，不把空价格变成一个天文数字插进索引。即使只消费 MBP，也要正确越过 MBO 组，才能找到后面的消息；只把已经认识的字段长度相加，会漏掉 schema 规定的块尾空间。[10]

这条消息的两个条目与随后另一条消息共同构成一个事件：

| 包/消息/条目 | 原始关键字段 | 应用后的买盘前三档 | 发布条件 |
| --- | --- | --- | --- |
| 包 700，消息 1，entry 1 | `RptSeq=51, New, Level=1, Px=5000.25, Qty=6` | `5000.25×6, 5000×10, 4999.75×12` | 消息未结束，暂存 |
| 包 700，消息 1，entry 2 | `RptSeq=52, Change, Level=2, Px=5000, Qty=7` | `5000.25×6, 5000×7, 4999.75×12` | `EndOfEvent=0`，暂存 |
| 包 701，消息 1，entry 1 | `RptSeq=53, Delete, Level=1` | `5000×7, 4999.75×12, 4999.50×8` | 收齐消息，且 `EndOfEvent=1`，发布 |

New 在指定档位插入，并把后面的档位下移；越过该合约公开深度的末档被移出本地可见范围。Change 修改现有档位的数量或订单数，不移动其他价格；价格变化由删除和新增表达。Delete 删除指定档位，其后的档位上移。如果最末档出现空缺，后续 New 条目会在有数据时补入。[11]

表中最后一行仍能看见 `4999.50×8`，是因为本例只展示前三档，它在完整簿里曾移动到第四档。如果演算的是只有三档的完整可见簿，它在第一次 New 时就已越界移出，删除后第三档要等待交易所补入。这种差别决定了固定长度数组怎样处理尾部。

`MatchEventIndicator` 的最高位，即 bit 7，标记一个事件的最后一条消息。一个事件可以跨消息、跨包；应用程序可以逐条修改候选状态，却等到完整事件结束才通知策略。其他位描述成交、直接报价、隐含报价等类别的结束，类别结束不等于整个事件结束。事件中若还包含 Trade Summary，成交记录进入成交序列，簿只按相应 Change/Delete 修改，否则同一数量会扣两次。[6][11]

快照 `SnapshotFullRefresh52` 的根字段包括 `LastMsgSeqNumProcessed`、`TotNumReports`、`SecurityID`、`RptSeq` 和 `TransactTime`，条目组再给价格、数量、订单数、档位与条目类型。该快照还可以带最近成交、统计等条目，解码器按 `MDEntryType` 将它们送往对应状态，不把每个 entry 都塞进买卖盘。[12]

频道出现包序号缺口时，先检查 A/B 两路冗余是否已经收到对应包；两路都有的包只应用一次。较小缺口可由 TCP Recovery 补回原包；超出补发服务范围或缺口较大时，进入 Market Recovery。补发恢复连续事件历史，快照恢复当前状态，两种完成结果分别记录。[13]

CME 的快照在独立通道循环发送。程序缓存实时增量，依次收到标的 S、T、U 的快照，它们各有水位。对标的 S，先比较快照中的 `LastMsgSeqNumProcessed` 与缓存增量包序号；小于水位的包已在快照之前。等于水位的包需要检查：若 S 同时出现在该包与快照中，比较双方的 `TransactTime`；时间不匹配时，等待 S 的下一轮快照或执行规范定义的自然恢复。衔接成功后用 `RptSeq` 检查标的更新位置，再处理后续完整事件。[14]

```text
实时增量缓存：包 100 ─ 包 101 ─ 包 102 ─ 包 103 ─ 包 104 ─ 包 105 ...
恢复循环：                           快照 S（水位104）
                                          快照 T（水位108）
                                                快照 U（水位110）

每个标的：核对水位包与事件时间 → 安装快照 → 衔接该标的增量 → 发布
整个频道：核对完整循环/报告数量及标的集合 → 所需标的全部恢复
```

`TotNumReports` 用于判断恢复循环中的报告覆盖，S 恢复完成不表示同频道的 T 已经恢复。频道整体恢复从恢复流包序号 1 开始核验完整一轮：各轮的快照顺序可能变化，因此按本轮 `TotNumReports` 与已接收集合核对，不能跨轮累计条目来凑齐报告数。若 `TotNumReports` 增大，新增快照可能插在循环中间，还要从包序号 1 收齐新的一轮。[14]

快照服务自己的网络包序号不替代增量水位。MBO Full Depth，即逐笔全深度的 MBOFD 恢复还有分块机制；其中 `NoChunks/CurrentChunk` 配合专门的快照模板使用，按该模板组装完整逐笔状态。

### 3.3 Nasdaq：订单 ID 的生存期

Nasdaq TotalView-ITCH 是公开行情协议，按事件描述显示订单进入、成交、撤销和离开订单簿。下面的 `A` 是不附参与方标识的 Add Order，恰好 36 字节。偏移从 ITCH 消息类型开始，不包含 Mold 的长度字段。[5]

| 偏移 | 长度 | 字段 | 类型/大小端、单位 | 解码值 | 用途 |
| --- | --- | --- | --- | --- | --- |
| 0 | 1 | `MessageType` | ASCII | `A` | 新增显示订单 |
| 1 | 2 | `StockLocate` | uint16，大端 | 7 | 当日标的定位码 |
| 3 | 2 | `TrackingNumber` | uint16，大端 | 0 | 交易所内部跟踪信息 |
| 5 | 6 | `Timestamp` | uint48，大端，午夜后纳秒 | 1,000,000 | 当天午夜后 1 毫秒 |
| 11 | 8 | `OrderReferenceNumber` | uint64，大端 | 1001 | 当日订单 ID |
| 19 | 1 | `BuySellIndicator` | ASCII | `B` | 买单 |
| 20 | 4 | `Shares` | uint32，大端，股 | 100 | 显示数量 |
| 24 | 8 | `Stock` | ASCII，右侧空格填充 | `TEST    ` | 股票代码 |
| 32 | 4 | `Price` | uint32，大端，`10^-4` | 1,002,500 → 100.25 | 显示价格 |

对应十六进制按字段拆开，读者可以直接核对数量和价格：

```text
41 | 00 07 | 00 00 | 00 00 00 0f 42 40 | 00 00 00 00 00 00 03 e9
42 | 00 00 00 64 | 54 45 53 54 20 20 20 20 | 00 0f 4c 04
 A      7       0          1,000,000                   1001
 B          100                 TEST                      1,002,500
```

接收方维护 `order_id → {side, price, remaining}`，同时维护 `(side, price) → aggregate_qty`。价格表从订单表派生，两个索引在处理同一事件时一起更新。初始化用 GLIMPSE：在独立连接中接收现存订单等状态，等末尾 `G` 后再交接到缓存的实时 ITCH。`G` 后面是二十字节 ASCII 数字，数值 N 指示从实时 ITCH 的第 N 条开始处理，包含第 N 条；GLIMPSE 自己的 Soup 序号属于另一条流。[15]

假定 GLIMPSE 安装了买单 900：`100.00×50`，随后 `G` 给出 `00000000000000000700`。实时流从 700 开始：

| ITCH 序号/消息 | 解码动作 | 订单 ID 表 | 聚合买盘 |
| --- | --- | --- | --- |
| `700 / A` | 新增 1001，买 100 股，价 100.25 | `900:50@100; 1001:100@100.25` | `100.25×100, 100×50` |
| `701 / X` | 1001 撤销 20 股 | `900:50@100; 1001:80@100.25` | `100.25×80, 100×50` |
| `702 / E` | 1001 成交 30 股，Match Number=77 | `900:50@100; 1001:50@100.25` | `100.25×50, 100×50` |
| `703 / U` | 1001 替代为 1002，40 股，价 100.00 | `900:50@100; 1002:40@100` | `100×90` |
| `704 / D` | 删除 1002 的全部剩余量 | `900:50@100` | `100×50` |

`X` 的 Cancelled Shares 是减少量，`E` 的 Executed Shares 也是减少量；`D` 删除整笔剩余订单。`U` 则带旧 ID、新 ID、新数量和新价格，标的与买卖方向等未变属性沿用旧单。旧 ID 立即退出索引，新 ID 表示替代后的订单；处理价格改变的替代时，把它放到新价格对应的位置，重新确定它在新价位的排队位置。ITCH 中 “Modify Order Messages” 是这几种修改消息的分类名称，没有一条可随意增改所有字段的通用 Modify 消息。[5]

普通 Trade 消息 `P` 用于报告非显示订单等成交，其订单并不通过对应 Add 进入这份显示簿。成交量统计按成交消息定义累加，可见簿按订单修改消息更新。一个没有被展示过的非显示买单成交 30 股，不会让任意可见买单少 30 股。

Mold 首序号与消息数量给出缺口范围。收到 `[700,701]` 后直接收到 `[704,705]`，要请求 702、703，并缓存后续消息；重复到达的消息按 session 与消息序号丢弃。服务端可补发的区间取决于开通的重传服务和保留范围。若无法补齐，重新建立 GLIMPSE 当前状态，并在历史记录中保留缺口。基于当前簿继续交易与基于连续逐笔历史[估计队列位置](https://github.com/Obscurepawn/TradingSystemLearning/blob/26f74936c47b06b0fd0e85e37020ed7a01484946/hft/HFT_Engineering_09_%E9%AB%98%E9%A2%91%E5%9B%9E%E6%B5%8B.md)，是两项不同的能力。

## 4. 自己的订单经历了什么

### 4.1 业务状态与“我是否知道结果”

一笔买 10 手的限价单，正常过程是提交、接受、成交。真实系统还会收到拒绝、部分成交、撤单、改单和过期。OMS 保存订单状态时，最好同时保存“已经确认的业务状态”和“仍未决的请求”，否则一个单独的枚举很难表达“已经成交 4 手，剩余 6 手正在撤销，但撤单结果未知”。

```text
待发送 ──发出请求──→ 待确认 ──接受──→ 已接受 ──成交一部分──→ 部分成交
                         │              │                      │
                         └─拒绝→ 已拒绝 ├─全部成交─────────────→ 全部成交
                                        ├─撤单请求→ 待撤 ──确认→ 已撤
                                        ├─改单请求→ 待改 ──确认→ 修改后状态
                                        └─到期/规则终止────────→ 已过期

“结果未知”附着于新单、撤单或改单请求：断线/超时 → 查询与回报核对 → 解除未知
```

发送接口返回成功，只推进[本地请求阶段](https://github.com/Obscurepawn/TradingSystemLearning/blob/26f74936c47b06b0fd0e85e37020ed7a01484946/hft/HFT_Engineering_06_%E7%BD%91%E7%BB%9C%E9%80%9A%E4%BF%A1.md#completion-points)；交易所的接受回报才说明订单进入了相应业务处理状态。成交回报给出成交身份、数量、价格，并推进持仓与现金。某些接口把接受和成交信息直接放进同步响应，另一些接口的 ack 只表示请求受理，接入程序按该消息的具体含义推进状态。

终态也要保存累计成交。例如一笔原始数量 10 的订单先成交 4，再撤掉 6，订单状态为已撤，但持仓已经增加 4。过期可能来自当日有效订单到期，也可能来自立即成交否则取消（Immediate Or Cancel，IOC）的剩余量、全部成交否则取消（Fill Or Kill，FOK）无法一次成交、或场所主动终止。只允许被动挂单的 post-only 请求若会立即成交，场所会按其规则拒绝或终止，适配器保留原始原因，再映射成内部结果。

自成交防范（Self-Trade Prevention，STP）用于阻止指定身份范围内的订单互相成交。Binance 用 `EXPIRED_IN_MATCH` 表示由 STP 导致的订单过期，用 `EXPIRED` 表示按订单类型规则或由交易所取消的情况。STP 事件带来的减少量要进入“被阻止成交/终止数量”，不能算成实际成交。CTP 还区分“部分成交还在队列中”和“部分成交不在队列中”，后者保留已成交数量，剩余量已经不再排队。[16][17]

改单在途期间，旧单仍可成交。若只改剩余量，确认后按照场所定义的新总量或新剩余量更新；若协议执行替代，维护旧、新订单身份之间的关联。改单被拒，撤销的是本次修改意图，旧单仍保持交易所确认的有效状态。把“改单拒绝”映射成“整笔订单拒绝”，会让策略忘记仍在市场上的风险。

### 4.2 撤单与成交交叉

下面一笔买 10 手的订单已经成交 4 手，策略对剩余 6 手发起撤单。请求路上又成交 2 手：

| 顺序 | 发生位置与事件 | 本地累计成交 | 尚未确认的内容 |
| --- | --- | --- | --- |
| 1 | 已收到成交 T1，数量 4 | 4 | 无 |
| 2 | 客户发出撤单 C1 | 4 | 剩余 6 是否撤销 |
| 3 | 撮合系统先成交 T2，数量 2 | 暂仍为 4 | 本地尚未收到 T2 |
| 4 | 撮合系统撤销剩余 4 | 暂仍为 4 | 撤单确认尚未到达 |
| 5 | 私有流回报 T2 | 6 | C1 待确认 |
| 6 | 收到已撤回报，累计成交 6 | 6 | 无；剩余可成交量为 0 |

收到撤单确认时，本地把未成交的 4 手从在途风险中释放；6 手成交已经进入持仓。若第 3 步把最后 6 手全部成交，后续撤单会被拒绝或报告来不及撤，最终状态是全部成交。撤单请求自身有结果，目标订单也有状态，二者分别存储。

价格、订单 ID 和成交 ID 的有效范围也要完整。`request_id=42` 只用于关联一次调用与响应；`client_order_id=buy-42` 关联业务意图；场所分配的 `order_id=9001` 定位已接收订单；`trade_id=77` 标识某次成交。进程重启后重新从 request ID 1 计数可以被连接范围隔离，但客户订单 ID 如果按交易日唯一，就需要跨进程持久保存分配进度。

### 4.3 发出之后，没有回报

新单发出后连接断开，本地应标记“请求结果未知”，保留原始意图及其客户订单 ID。使用新 ID 再发一遍，会产生第二笔合法订单。原 ID 重发也未必是永久幂等操作：有的接口只要求当前活动订单的 ID 不重复，第一笔已经成交后该 ID 又能被使用。

恢复顺序由此确定：先恢复可续传的私有回报；再按客户 ID、场所订单 ID 查询未决订单；补查成交和终态订单，确认真实敞口；最后才决定尚未满足的策略意图是否仍值得执行。“查询未完成订单为空”只说明当前无挂单，第一笔订单可能已经全部成交，成交查询因此不可省略。

重放还会与新回报交错。例如本地已处理私有序号 50，重连后先收到实时 54，再收到补发 51—53。具有可恢复序列的接口按序号暂存 54，补齐后依次应用；只有事件 ID、没有可续传总序列的接口，则用成交去重、订单累计字段和查询窗口共同对账。查询结果是在服务端某一时刻形成的，旧查询不能覆盖已经处理的新回报。若查询没有快照水位，恢复器持续收流并重复核对受影响订单，直到未决请求全部可解释、成交集合与订单累计量一致。

![订单结果未知时的恢复流程](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/26f74936c47b06b0fd0e85e37020ed7a01484946/hft/assets/hft-08/order-recovery.png)

成交台账适合以成交身份为键保存明细，持仓从已确认成交派生。订单回报中的累计成交量用于核对完整性，不能与成交明细各加一次仓。遇到成交撤销或更正，保存引用原成交的更正事件，再调整派生余额；这样回放同一组事件仍能得到相同的台账。

## 5. CME 与 Nasdaq 的原生订单会话

CME iLink 3 使用 TCP 承载 FIXP 会话和 SBE 业务消息。建立会话时通过 Negotiate 协商，再通过 Establish 绑定连接。通用唯一标识符（Universally Unique Identifier，UUID）在 iLink 中是客户端按规范设置的 64 位会话标识，配合双向序号保存会话进度；它不是任意库默认生成的 128 位 UUID 字符串。一个逻辑会话可以经历多次连接建立，接入程序要把连接对象与可恢复会话记录分开。[18]

交易所发往客户的业务流可恢复。假设 UUID=U 下最后连续处理到 50，后续发现 54，就用 U、起始 51 和请求数量申请补发，缓存实时消息。补发业务消息保留原序号，并通过 `PossRetransFlag` 标识重传。CME 的会话管理消息本身不参与这种业务重放；建立确认里的前一 UUID、序号帮助发现重连前遗漏的回报。[19]

客户发往 CME 的方向使用幂等流语义：交易所发现客户序号跳跃时返回 `NotApplied`，由客户根据尚未执行的请求选择处理方式。恢复器既保存发送序号，也保存“此序号曾对应什么请求”，才有依据解释缺口。当前 iLink 文档的业务回报补发限定在当周可恢复范围内，跨周的历史台账依赖自己的记录与场所提供的对账服务。

行情恢复和订单恢复在这里各用一套身份：MDP 按频道包序号与标的更新恢复市场状态；iLink 按 UUID 和业务序号恢复私有回报。

Nasdaq 的订单协议 OUCH 5.0 可运行在 SoupBinTCP 上。服务端的 Sequenced Data Packet 属于带序号流，双方按收到的包计数，序号没有显式放在每个数据包里。重新登录指定会话和期望序号后，服务器可以从相应位置继续发送回报。客户端发给服务器的 Unsequenced Data Packet 则不属于这条服务器序列，待确认请求由订单身份与 OUCH 业务规则处理。[4][20]

具体到“买单发出但未收到接受回报”，Soup 续接会补回可恢复的服务端响应；如果该请求根本没有被服务端处理，响应流自然找不到它。OMS 核对订单身份之后再决定是否重发。

## 6. 国内接入：交易所网关与 CTP 柜台

### 6.1 深交所 Binary 的三种行情消息

国内证券机构的 OMS 可通过交易网关协议接入交易所。上交所的证券交易数据交换协议（Securities Trading Exchange Protocol，STEP）沿用 FIX 风格字段与业务规则；深交所 Binary 规定固定宽度的二进制字段。两所都提供不同接口规范，选择 STEP 或 Binary 取决于实际开通的网关与业务。券商提供的 XTP、QMT 等客户端产品还封装账户、查询、风控与柜台连接，客户程序可以在这一层接入。

下面固定使用深交所 Binary 行情 1.17 和交易 1.32 的字段定义。现货集中竞价快照为 `300111`，逐笔委托为 `300192`，逐笔成交为 `300191`。业务类别由 `MDStreamID` 进一步区分，快照中的 `010` 和逐笔中的 `011` 对应现货集中竞价的两种行情表达。[21]

```text
标准头：[MsgType:uint32][BodyLength:uint32]

300111 快照公共字段：OrigTime、ChannelNo、MDStreamID、SecurityID、市场状态等
  NoMDEntries
    ├─ MDEntryType、MDEntryPx、MDEntrySize、MDPriceLevel、NumberOfOrders
    │    └─ NoOrders
    │         ├─ OrderQty[0]
    │         └─ OrderQty[1] ...
    └─ 下一个行情条目 ...

300192 逐笔委托：ChannelNo、ApplSeqNum、MDStreamID、SecurityID、SecurityIDSource、
                Price、OrderQty、Side、TransactTime、OrdType

300191 逐笔成交：ChannelNo、ApplSeqNum、MDStreamID、BidApplSeqNum、OfferApplSeqNum、
                SecurityID、SecurityIDSource、LastPx、LastQty、ExecType、TransactTime

标准尾：[Checksum:uint32]
```

所有整数使用大端。逐笔委托的固定布局可以从头部一直算到尾部：

| 从消息起点的偏移 | 长度 | 字段 | 类型、单位 | 解码例子与用途 |
| --- | --- | --- | --- | --- |
| 0 | 4 | `MsgType` | uint32，大端 | 300192，选逐笔委托模板 |
| 4 | 4 | `BodyLength` | uint32，大端，字节 | 51，体长度 |
| 8 | 2 | `ChannelNo` | uint16，大端 | 频道 1 |
| 10 | 8 | `ApplSeqNum` | int64，大端 | 100，频道内记录号 |
| 18 | 3 | `MDStreamID` | char[3] | `011` |
| 21 | 8 | `SecurityID` | char[8]，空格补齐 | `000001  ` |
| 29 | 4 | `SecurityIDSource` | char[4] | `102 `，深交所 |
| 33 | 8 | `Price` | int64，大端，四位小数 | 186400 → 18.6400 |
| 41 | 8 | `OrderQty` | int64，大端，两位小数 | 10000 → 100.00 股 |
| 49 | 1 | `Side` | char | `1` 买、`2` 卖 |
| 50 | 8 | `TransactTime` | int64，大端，本地日期时间 | `YYYYMMDDHHMMSSsss` |
| 58 | 1 | `OrdType` | char | `2` 限价 |
| 59 | 4 | `Checksum` | uint32，大端 | 按规范验证校验值 |

快照的 `MDEntryPx` 使用六位小数，同一个 18.64 在快照里写成 18,640,000，在逐笔 `Price` 里写成 186,400。数量 `Qty` 使用两位小数；本地若直接把线上 10000 当成 10000 股，就会把该委托放大一百倍。`LocalTimeStamp` 的 `20261019093000123` 表示本地日期和 09:30:00.123，按 Unix 毫秒解释会得到完全错误的日期。[21]

快照重复组里还嵌套每档揭示委托量。`NumberOfOrders` 是档位总委托笔数，`NoOrders` 是实际揭示了多少笔明细。在所选规范中，Level 1 最多揭示五档且不揭示这些委托明细；Level 2 最多揭示十档，相关明细最多五十笔。每个内层 `OrderQty` 只有数量，没有逐笔委托的身份，因此这些量不能直接作为一组可跟踪的 MBO 订单 ID。

逐笔委托与逐笔成交在同一个频道内共用连续的 `ApplSeqNum`，可按下面顺序理解：

| 频道内记录 | 解码事件 | 本地处理 |
| --- | --- | --- |
| `100 / 300192` | 买委托，100 股，价 18.64 | 记录委托索引 100 |
| `101 / 300192` | 卖委托，60 股，价 18.64 | 记录委托索引 101 |
| `102 / 300191, ExecType=F` | 成交 40，`BidApplSeqNum=100, OfferApplSeqNum=101` | 两个被引用委托各减 40；成交序列记 40 |
| `103 / 300191, ExecType=4` | 撤销余量 20，引用卖方 101 | 按撤销事件处理对应剩余量 |

成交中的某一方引用为零，表示没有对应委托，接收方不应创建“订单 0”再扣量。记录身份以交易日、频道和记录号共同限定；单独用 `ApplSeqNum` 做全市场唯一键会发生碰撞。

周期快照与逐笔流没有因为时间接近就自动共享一个初始化游标。如果所开通服务没有明确给出快照到逐笔的交接机制，可以分别维护周期 MBP 状态与从完整逐笔区间重建的 MBO。把最近一份十档快照与某个时间戳后的逐笔消息直接拼接，会丢掉订单身份及同一时间刻度内的顺序。

### 6.2 CTP 回调里的行情

CTP 提供行情 API 与交易 API。API 是应用程序编程接口（Application Programming Interface），供程序发起调用；SPI 是服务提供者接口（Service Provider Interface），在 CTP 中由应用实现回调方法，接收异步响应。底层期货交易数据交换协议（Futures Trading Data Exchange Protocol，FTD）处理柜台与客户端的数据交换，常用客户端库将它封装在 TCP 连接之上。

行情的典型过程是创建 `CThostFtdcMdApi`、注册 `CThostFtdcMdSpi`、注册行情前置地址、初始化连接、登录、订阅合约，随后在 `OnRtnDepthMarketData(CThostFtdcDepthMarketDataField*)` 收到状态。交易前置和行情前置是不同服务，交易登录成功后仍要建立相应的行情订阅。

```text
交易所行情 → 期货公司 CTP 行情前置 → CTP SDK 解码 → OnRtnDepthMarketData
                                                    │
                          合约状态更新 ← 字段校验与单位转换
```

回调中，`InstrumentID/ExchangeID` 定位合约与交易所，`TradingDay` 是交易日，`ActionDay` 是业务日期，`UpdateTime/UpdateMillisec` 给出更新时间。夜盘的自然日期与交易日可以不同，记录时保留两个日期及交易日历，而不是直接拼成本机当天日期。`LastPrice/Volume/Turnover` 描述最近价、累计成交量和累计成交金额；`BidPrice1/BidVolume1/AskPrice1/AskVolume1` 描述一档买卖盘。

这些字段是 SDK 解码后的 C++ 回调结构。结构里预留的五档字段按市场和权限填写；未提供或无效值经适配层转换后再交给策略。累计成交量的变化可以用于统计区间内成交数量，却无法还原这段时间里每笔成交的价格、方向和订单顺序。

普通 CTP 深度行情按快照推送，更新粒度由上游市场和服务决定。上期技术客户端指南描述的常见节奏约为每秒两次，只在数据更新时推送，毫秒位置也不固定。标准行情接口没有逐段申请历史增量的回补能力；断线后重新订阅能取得后续状态，断线期间的逐笔历史则来自独立记录、多路接收或另行授权的数据服务。[17]

### 6.3 认证、报单身份与私有流

交易接口启动时，为账户创建独立流文件目录，注册回调，设置 `SubscribePrivateTopic` 与公有流订阅模式，注册交易前置并初始化。连接建立后，按柜台要求调用 `ReqAuthenticate`，使用期货公司提供的 `AppID/AuthCode` 完成客户端认证，再登录。认证失败时停在认证阶段；登录成功后取得交易日、`FrontID/SessionID/MaxOrderRef`，按账户要求完成结算单查询与确认，再开启交易。

看穿式监管还涉及客户端信息采集库，采集库与柜台版本按接入要求匹配。`AppID` 标识接入软件，交易软件名称、版本、账户和程序化交易报告之间建立可追溯映射；认证成功本身不替代监管报告。

报单刚发出时，客户掌握的是 `FrontID + SessionID + OrderRef`。`OrderRef` 在本次登录会话中递增，可从登录回报的 `MaxOrderRef` 继续分配。重新登录后会话身份变化，旧会话的 `OrderRef=1` 与新会话的 `OrderRef=1` 是两笔不同的客户报单。不要只把 `OrderRef` 存成订单主键。

交易所接受后，回报提供 `ExchangeID + OrderSysID`；OMS 把它挂到原来的客户订单记录上，跨会话查询和撤单也能以这一组身份定位。两个标识组是同一笔订单在不同阶段的入口，收到新的场所 ID 时执行“补全映射”。字符字段按 API 定义保存，原样保留 `OrderSysID` 的填充与格式。[17]

请求函数的 `0` 返回值表示请求已成功发出，后续错误响应、`OnRtnOrder` 报单回报和 `OnRtnTrade` 成交回报才补全业务结果。`OnRtnOrder` 可多次更新同一订单；`OnRtnTrade` 提供每笔成交，用于成交台账与持仓计算，报单中的累计字段用来核对。对于正在等待交易所确认的报单，CTP 的 Unknown 状态也不应被当作拒绝重发。

私有流负责报单与成交回报。它在交易日内支持重传，三种订阅模式决定进程重启后从哪里接收：

| 模式 | 起始位置 | 与本地台账的配合 |
| --- | --- | --- |
| `THOST_TERT_RESTART` | 当前交易日开始 | 重放当天回报；已有订单和成交按身份去重 |
| `THOST_TERT_RESUME` | SDK 本地流文件记录的位置 | 同时核对业务台账是否至少覆盖该位置 |
| `THOST_TERT_QUICK` | 本次登录之后 | 补做未决订单、成交和账户查询，恢复登录前的状态 |

流文件和业务台账的进度有一个容易遗漏的竞争：SDK 已经把接收位置写到流文件，应用尚未把对应成交写入持久台账，进程此时崩溃。用 RESUME 再登录可能越过这笔业务尚未保存的成交。因此 SDK 续传位置只负责传输进度；业务恢复还要有自己的持久记录与对账，或用 RESTART 重放后按身份去重。不同账户、不同并发实例使用隔离的流文件目录，避免互相覆盖游标。[17]

对话和查询在断线后重新发起，并重新收齐本次请求的完整响应。跨交易日也会切换私有流范围，长期历史从自己的订单、成交日志恢复，不把“当日可靠私有流”当成无限期档案。

### 6.4 查询节奏与证券回报同步

CTP 查询是异步过程：发出 `ReqQryOrder` 后接收零条或多条响应，直到本次请求的 `bIsLast=true` 才结束。没有订单时也要等结束标志。一个“每秒一次、同时只有一个查询在途”的调度器，既限制发起间隔，也等待前一查询完整结束；仅仅每秒起一个线程仍会在查询耗时较长时超限。

上期技术 2015 版《CTP 客户端开发指南》明确给出这组查询限制，以及 `-2` 未处理请求超限、`-3` 每秒请求超限等返回值。它还列出未配置时每会话每秒六笔交易相关指令、每账户六会话的历史默认，且说明期货公司可配置报单流量。实际接入把柜台确认的限额作为配置，不把旧默认写死在 SDK 适配器中。报撤指令与 `ReqQryXXX` 的查询限制分别计数，仍共同受服务提供方设置的其他限制约束。[17]

恢复时优先查未决订单与成交，再查持仓、资金核对敞口。若所用柜台按上述查询模型配置，四次查询的最早发起时刻可为 0、1、2、3 秒；最后完成时间还要加第四次响应耗时，前面某次查询耗时超过一秒也会推迟后续请求。订单很多、查询返回慢时，恢复时间会比连接重建长得多。查询排队期间，私有回报继续进入同一去重台账。

证券交易网关可以提供更明确的业务恢复游标。深交所 Binary 交易 1.32 的业务回报按平台分区保存 `PartitionNo/ReportIndex`；回报记录号在每个分区内连续递增。重新连接后，OMS 先读平台信息，再为各分区请求下一条尚未处理的记录。已完整处理 300，就从 301 请求；若误填 1001 而服务端目前只有 300，网关会等待记录到 1001 才继续发，301—1000 被客户端自己跳过。[22]

这套业务游标独立于 TCP 重建，也独立于会话心跳。上交所 STEP 接入同样要按相应交易网关服务规范恢复业务回报。接口规范版本与生产启用通知一起绑定到接入配置，避免看到发布了新文档就替换正在使用的协议。

## 7. Binance：按消息流选择接口

Binance Spot 的公开 WebSocket 行情服务负责深度和成交推送；WebSocket API 负责交易请求、查询和用户数据流订阅；REST 提供按需请求与查询；FIX 则提供独立的 Order Entry、Drop Copy 和 Market Data 会话。TLS（Transport Layer Security，传输层安全协议）保护连接，WSS 表示运行在 TLS 上的 WebSocket。接入程序根据消息职责选择连接，再考虑编码与解析成本。

私有流的主流程可以写得很短：用 Ed25519 密钥完成 WebSocket API 的 `session.logon`，再发送：

```json
{"id":"subscribe-orders-1","method":"userDataStream.subscribe"}
```

成功响应给出 `subscriptionId`，后续账户事件带这个订阅标识。当前接口还提供 `userDataStream.subscribe.signature`，由请求携带 API key、时间戳和签名进行订阅，不应把“已登录会话的 subscribe 要求 Ed25519”推广为所有用户数据流订阅方式都只有这一种认证。事件输出支持 JSON 和 SBE。SBE 解码器使用连接协商的 schema，并核对该版本仍处于服务端支持期；功能首次出现时的最低版本，未必仍可建立当前连接。[23][24]

```json
{
  "subscriptionId": 0,
  "event": {
    "e": "executionReport", "s": "BTCUSDT",
    "c": "buy-42", "i": 9001,
    "x": "TRADE", "X": "PARTIALLY_FILLED",
    "l": "0.10", "z": "0.40", "L": "100.50", "t": 77
  }
}
```

这是只保留相关字段的事件片段：`c/i` 分别是客户订单 ID 和场所订单 ID；`x` 是本次执行事件类型，`X` 是当前订单状态；`l` 是本次成交量，`z` 是累计成交量，`L` 是本次成交价格，`t` 是交易 ID。本次只新增成交 0.10，累计值 0.40 用于核对，不再加一次持仓。相同成交在重连查询与私有推送中各出现一次时，仍只入账一次。[24]

旧客户端通过 REST 取得 `listenKey`、再连接市场行情域名接收私有事件。Binance 在 2025 年 4 月宣布弃用这种用户数据流接法，2025 年 10 月移除了相应文档，相关创建、续期、关闭接口自 2026 年 2 月 20 日停止提供；新接入按 WebSocket API 的订阅方式组织生命周期。[24]公开行情 `stream` 与用户数据流 `subscriptionId` 是不同层面的订阅标识。

| Spot 接口 | 请求与输出 | 密钥/权限要点 | 恢复时使用什么 |
| --- | --- | --- | --- |
| JSON 公开行情 | WebSocket JSON 深度、成交 | 公开市场流 | 深度快照和增量衔接 |
| WebSocket API 用户数据流 | 订阅请求；JSON/SBE 账户事件 | 按 subscribe 或签名订阅认证方式配置 | 重新订阅、订单与成交查询 |
| FIX Order Entry | 下单、撤单、执行报告 | Ed25519，`FIX_API` | 回报与查询对账 |
| FIX Drop Copy | 账户执行报告、订单列表状态 | Ed25519，`FIX_API` 或只读 FIX 权限 | 独立回报证据；文档规定延迟一秒 |
| FIX Market Data | 行情订阅及标的信息 | Ed25519，相应 FIX 权限 | 按具体行情消息的恢复规则 |
| SBE 公开行情 | JSON 控制消息，二进制市场事件 | Ed25519 API key 放连接头；无额外行情权限要求 | 按所订阅 SBE 流的序号及快照规则 |

SBE 公开行情的所有时间字段使用微秒，控制响应与 `serverShutdown` 等通知仍可使用 JSON 文本帧。接收分发先识别帧类型，再选择 JSON 或 SBE 解码器。FIX SBE 又是 FIX 接口的二进制表达，不能拿公开行情 `stream_1_0.xml` 去解其业务消息。[25][26]

Binance FIX 明确不支持 `ResendRequest`。如果连接断开，重新登录取得的是新的通信会话，遗漏订单结果靠用户数据流、独立副本与查询补全。Bybit Spot FIX 也不支持标准 gap-fill，并在新连接重置序号；它的准入还涉及 RSA 密钥和账户/机构白名单。USDⓈ-M 合约的交易 WebSocket 则是另一产品接口，Spot 的 FIX 或 SBE 能力不自动适用于合约。[26][27]

## 8. 限频、监管标准与紧急停止

### 8.1 行情剧烈变化时，撤单会挤在哪里

假定多个策略在同一个账户上各挂着几十笔订单，行情突变后同时撤单。如果每个进程都认为自己还有一百次请求额度，合计流量就可能超过账户或 IP 的共享限额。OMS 的额度管理因此按实际计费维度聚合：连接消息数、IP 请求权重、账户订单计数、柜台报撤速率分别维护。

Binance 的请求权重与未成交订单计数是不同限额；Bybit FIX 与 REST V5 使用共享限额池。恢复查询也消耗额度。接入层从响应里的当前使用量与重置时间更新本地模型，对被限流的请求按规定退避；持续立即重试会把恢复请求排在自己的重试洪峰之后。[26][27]

在本地策略预算中，为撤单与查询保留额度，并且更早限制新增风险。保留的是自己可控制的发送预算，场所仍按它定义的共享池核算；若场所限额已经用满，本地预留计数无法让请求越过服务器限制。必要时使用已开通的批量撤单或独立风控通道。

业务监控至少保留三只计时器：最后一条合法会话消息的到达时间、相关标的最后一次可信行情更新时间、每个未决订单请求的等待时间。市场休市时没有行情更新是正常状态，连续交易中活跃标的长期不更新才触发相应处置。签名请求还要检查本地时间偏差：`timestamp` 超前服务端或已经超出 `recvWindow`，有效的业务参数也会被拒绝。扩大接收窗口只改变请求有效时间，不修复时钟本身。

### 8.2 中国市场的认定、报告与实际限额

沪、深、北交易所的程序化交易管理实施细则自 2025 年 7 月 7 日施行。就这些证券市场细则的高频认定而言，单个账户每秒申报与撤单合计最高达到 300 笔，或单日合计最高达到 20,000 笔，即落入相应认定情形，“以上”包含本数。规则还保留交易所认定的其他情形，并规定高频投资者的额外报告和差异化管理。[28][29][30]

这里的数值是高频交易认定标准，**不是所有账户一到第 300 笔就被接口统一拒绝的硬上限**。程序化交易本身也有报告要求。异常交易监控、柜台流控、交易所技术限频及收费规则分别产生约束；差异化收费的具体费率和计费范围由相应实施规则确定，账户接入配置据此记录收费口径。

期货市场适用证监会《期货市场程序化交易管理规定（试行）》，自 2025 年 10 月 9 日施行。程序化交易者在交易前报告有关信息，收到确认后开展交易；高频交易的具体标准由期货交易所制定。因此证券的 300/20,000 不应直接放到期货账户风控参数中。[31]

在工程实现上，可将账户、场所、交易日作为报撤统计的基本归属，按所适用规则聚合关联账户或实际控制关系。多个连接与策略共同增加同一份账户计数，记录成功、拒绝、重试等原始动作，再按规则口径计算。达到内部预警线后减少新增订单、合并无效改单，并保留风险处置能力；合规报告和真实的系统能力约束则由账户接入配置控制，避免用一个常量兼任所有限制。

### 8.3 Drop Copy、自动撤单和主动停止

自动撤单由交易所检测失联触发，主动紧急停止由获准主体发起，Drop Copy 负责观察。它们各自解决不同故障，部署时同时记录触发条件、范围和最后确认结果。

| 能力 | 触发与权限 | 作用范围 | 如何确认结果 |
| --- | --- | --- | --- |
| CME COD，断线自动撤单 | iLink 非正常失联，按已启用配置触发；新会话默认及可退出范围见官方网关规则 | 相应 Session ID/Trader ID 的适用挂单；上市期货及期权规则排除 GTC/GTD | 重连后补发撤单回报并核对订单 |
| CME Kill Switch | 有权限的清算机构或获授权执行机构主动操作 | 所选 SenderComp ID 的新单被阻止，允许撤单市场状态下取消工作订单 | 私有执行报告、风险管理状态与订单查询 |
| Bybit DCP，断线保护 | 获准机构客户配置；私有 WebSocket 订阅 `dcp`，失联超过窗口 | 已配置产品范围内的活动订单 | 查询 DCP 配置，再核对订单流和订单查询 |
| Binance Spot FIX Drop Copy | 具备相应 FIX 只读/交易权限的独立连接 | 账户执行报告与订单列表状态副本；延迟一秒 | 观察订单/成交身份和接收进度，不触发撤单 |

GTC（Good Till Cancel）是撤销前有效，GTD（Good Till Date）是指定日期前有效。CME COD 对所述上市衍生品的排除项、正常退出与故障切换条件都会影响残留挂单；处于禁止撤单的市场阶段，即便触发 COD 也可能无法撤销。Kill Switch 同样受市场状态约束，例如关闭市场中的长期有效挂单另有处理规则。配置自动撤单后，恢复器仍逐笔核对实际结果。[32][33]

Bybit DCP 面向获准机构客户，开关由客户经理流程办理，客户端还要订阅 `dcp` 主题。只调用设置接口却未建立相应私有流条件，不能期待失联时按设想触发。恢复时先查询当前配置，再验证订单终态；不要靠本地“断开已满十秒”的计时器把剩余挂单直接清零。[34]

## 9. 一次故障怎样才算恢复完成

程序同时遇到公开行情缺口与私有连接中断，可以按下面的事件表保存恢复进度；表中累计成交只计已经核实的成交。

| 时刻与输入 | 行情游标/状态 | 订单认知与累计成交 | 允许的动作 |
| --- | --- | --- | --- |
| 买单 Q 发出，尚无订单回报 | 本地簿 104，有效 | Q 待确认，已知成交 0 | 等待回报，保留未决风险 |
| 收到行情 `[107,108]` | 缺 105—106，冻结该簿 | Q 仍待确认 | 停止使用该簿增加风险 |
| 私有连接中断 | 缓存后续行情 | Q 结果未知，已知成交仍为 0 | 重连、补回报；不重复创建 Q |
| 补到接受及成交 T1=4 | 行情候选簿重建中 | Q 部分成交，累计 4 | 查询 Q、核对成交和敞口 |
| 发出撤单，补到 T2=2 与撤单确认 | 行情候选簿重建中 | Q 已撤，累计 6，剩余 0 | 完成 Q 台账对账 |
| 新快照与增量衔接，完整事件发布 | 行情恢复有效 | 台账已解释全部未决请求 | 检查账户风控与额度 |
| 两项状态均通过 | 可信行情 | 一致台账 | 恢复策略交易 |

接入验收可从下面的输入直接构造，不必先实现完整客户端。

| 注入的输入 | 预期解码/状态 | 应检查的输出 |
| --- | --- | --- |
| Binance 快照数量 5，更新绝对量 7 | 覆盖为 7 | 发布数量 7，非 12 |
| ITCH 100 股订单收到 `X=20` | 剩余 80 | 订单仍在，聚合量减 20 |
| CME Trade Summary 后跟 Change | 成交进入成交序列；Change 更新簿 | 订单簿只变化一次 |
| CME 组头计数为 2，消息实际只有 1 个完整条目 | 长度校验失败 | 整条消息不作为有效事件发布 |
| CME 第一条消息 `EndOfEvent=0` | 暂存中间簿 | 后续结束标志前无策略通知 |
| 私有成交 ID 重复出现 | 识别已入账成交 | 持仓不再增加 |
| 重放 51—53 与实时 54 交错 | 缓存并填补缺口 | 有序台账，没有旧状态覆盖新状态 |
| CTP 查询未结束又请求下一查询 | 调度器继续等待 | 无并发查询；按返回码重试 |
| 会话重置或跨交易日后序号再次为 1 | 新的身份范围 | 不误判为旧消息，也不清空持仓 |
| 多策略同时逼近账户报撤预算 | 聚合计数触发内部控制 | 新增风险降频，查询与撤单仍受可用额度管理 |

文中 Binance 小簿、ITCH 字节、CME v9 条目步长与跨包发布例子可运行 [verify_examples.py](https://github.com/Obscurepawn/TradingSystemLearning/blob/26f74936c47b06b0fd0e85e37020ed7a01484946/hft/examples/protocols_08/verify_examples.py) 核对。测试只在内存中处理构造的报文与事件；会话认证、真实流控、断线自动撤单和交易日切换，放到相应认证环境、[SimNow](https://www.simnow.com.cn/) 或 [Binance Spot Testnet](https://testnet.binance.vision/) 按上表验证，并保存输入、回报、最终台账和恢复耗时。

## 参考资料

1. [Binance Spot WebSocket Streams](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md)：JSON 深度、快照衔接算法、时间单位与 partial depth。
2. [Nasdaq MoldUDP64 1.00](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/moldudp64.pdf)：包头、消息序号与补发。
3. CME [SBE Message Header](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457325932)、[Packet Structure](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457326496)：包与消息边界。
4. [Nasdaq SoupBinTCP 3.00](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/soupbintcp.pdf)：长度、方向、带序号流与登录续接。
5. [Nasdaq TotalView-ITCH 5.0](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQTVITCHSpecification.pdf)：字段布局、显示簿事件、替代订单与成交消息。
6. [CME Market Data Incremental Refresh — MBP and MBOFD](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457227795)：条目语义、`RptSeq` 与事件标志。
7. [FIX Session](https://fixtrading.org/standards/fixsession/) 与 [FIX Protocol](https://fixtrading.org/standards/fix-protocol/)：会话、消息身份和应用语义。
8. [FIX Trading Community — A Basic Guide to FAST](https://www.fixtrading.org/wp-content/uploads/download-manager-files/A-Basic-Guide-to-FAST-v1.0.pdf)：模板、字典与字段算子。
9. [Bybit V5 Orderbook](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook)：snapshot/delta、重置与序号字段。
10. [EPAM 行情处理器保留的 CME XML](https://github.com/epam/java-cme-mdp3-handler/blob/master/mbp-with-mbo/src/test/resources/templates_FixBinary.xml)：文件头 `package=mktdata, id=1, version=9, description=20180308`，用于固定本篇字节演算；业务语义另与 CME 官方规范核对。
11. [CME Market by Price — Multiple Depth Book](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457574047)：档位插入、移动、删除和成交的独立处理。
12. [CME Market Data Snapshot — Full Recovery](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457736274)：快照根字段和重复组。
13. [CME TCP Recovery](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457574209)：增量包补发。
14. [CME MBP and MBOFD Market Recovery](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457672425)：快照循环、水位包和事件时间比较。
15. [Nasdaq GLIMPSE 5.0](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQGlimpseSpecification.pdf)：快照结束与 ITCH 起始序号。
16. [Binance Spot 枚举定义](https://github.com/binance/binance-spot-api-docs/blob/master/enums.md)：订单状态、执行类型与 STP 终止。
17. [上期技术《CTP 客户端开发指南》2015-10-16 版，期货公司转载](https://www.eastmoneyfutures.com/software/9809db08-c8cb-4cc4-b631-8a16f0c2dfb9/0f757a06-10a5-48c2-817e-362b42202b51/f3ec4355-a448-45d7-bd51-3124d046092a/CTPcdg_ch.pdf)，以及 [SimNow 官方 API 下载目录](https://www.simnow.com.cn/static/apiDownload.action)：标准 API 的通信模式、订单标识、流文件、认证和历史流控定义；API 包、采集库和前置按期货公司当前接入要求匹配。
18. CME [iLink Functional Specification](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/714539039)、[Session Layer](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/714145834)：FIXP、UUID 与会话建立。
19. [CME Session Layer — Transferring](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/714604582)：双向序列、NotApplied、业务重传与范围。
20. [Nasdaq OUCH 5.0](https://www.nasdaqtrader.com/content/technicalsupport/specifications/TradingProducts/OUCH5.0.pdf)：原生订单请求和回报。
21. [深交所 Binary 行情数据接口规范 1.17](https://www.szse.cn/marketServices/technicalservice/interface/P020250328368568358456.pdf)：第 4.5、5 章，快照、逐笔、重复组与数据类型。
22. [深交所 Binary 交易数据接口规范 1.32](https://www.szse.cn/marketServices/technicalservice/interface/P020250328367803673293.pdf)：第 3.15 节及回报同步消息；[上交所技术资料目录](https://www.sse.com.cn/services/tradingtech/home/)用于查询对应 STEP 规范与启用通知。
23. [Binance WebSocket API — User Data Stream](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-api/user-data-stream)：订阅、签名订阅与 SBE 输出。
24. [Binance User Data Stream](https://github.com/binance/binance-spot-api-docs/blob/master/user-data-stream.md) 与 [CHANGELOG](https://github.com/binance/binance-spot-api-docs/blob/master/CHANGELOG.md)：执行报告字段与 listenKey 接法迁移。
25. [Binance SBE Market Data Streams](https://github.com/binance/binance-spot-api-docs/blob/master/sbe-market-data-streams.md)：密钥、帧类型、schema 与时间单位。
26. [Binance Spot FIX API](https://github.com/binance/binance-spot-api-docs/blob/master/fix-api.md)：权限、Drop Copy 延迟、重传限制、FIX SBE 和限频。
27. [Bybit FIX Guide](https://bybit-exchange.github.io/docs/fix-api/guide)；[Binance USDⓈ-M WebSocket API](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-api-general-info)：各产品的接口范围。
28. [上海证券交易所程序化交易管理实施细则](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20250612_10781696.shtml)：第 33—34 条高频认定与报告，第 46—48 条范围和施行。
29. [深圳证券交易所程序化交易管理实施细则](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020250403603802169453.pdf)：程序化及高频管理。
30. [北京证券交易所程序化交易管理实施细则](https://www.bse.cn/jygl_list/200025383.html)：高频认定、报告及施行。
31. [证监会《期货市场程序化交易管理规定（试行）》](https://www.csrc.gov.cn/csrc/c101954/c7564346/content.shtml)：交易前报告、高频标准制定权限与生效时间。
32. [CME Cancel on Disconnect](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457216487)：触发条件、订单范围、例外与重连确认。
33. [CME Kill Switch](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457088324)：权限、SenderComp ID 范围和市场状态。
34. [Bybit Disconnect Cancel All](https://bybit-exchange.github.io/docs/v5/order/dcp)：机构准入、`dcp` 订阅和产品范围。
