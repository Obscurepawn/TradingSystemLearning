# HFT Engineering 08：交易所协议——一笔买单怎么变成订单，盘口又怎么在本地重建

策略根据当前行情上算出"现在买 1 手"，接下来要回答两个问题：这个意图怎么变成交易所认可的订单，我又怎么知道盘口现在长什么样。前者走的是账户私有的交易通道，后者走的是公开的行情通道，两条通道独立建立、独立断线、独立恢复，混在一起处理是很多事故的起点。

这笔买单发出去之后会发生什么，取决于你接的是哪一类市场。传统证券交易所和期货交易所把撮合放在中心化的撮合引擎里，客户通过专线或网关用二进制协议报单；

链上的自动做市商（Automated Market Maker，AMM）用资金池和定价规则执行兑换，无需逐笔撮合买卖订单；去中心化交易所（Decentralized Exchange，DEX）也可以采用订单簿，由节点处理签名订单，再通过链上共识确认成交结果。

这一篇按这三类市场分别讲清楚两件事：买卖订单怎么下（报单、撤单改单、执行回报、会话与断线恢复），行情怎么订阅（快照、增量、传输方式、丢包恢复、本地订单簿怎么建），最后再放在一起对比。

## 1. 先分清两条流，以及几个通用概念

接入程序同时维护交易状态和市场状态。自己的交易台账记录请求是否发出、是否被接受、成交数量和撤单结果，交易带来的持仓与现金变化从已经核实的成交派生。市场状态在订单簿市场里是公开买卖盘，在 AMM 里是池子储备和流动性分布。市场价格发生变化，仍然回答不了“我那笔买单成交没有”：中心化撮合市场靠私有执行回报和账户查询核对，链上兑换则按交易哈希检查回执、事件和资产变化。

先定义几个概念：

**会话层**位于 TCP 连接之上、业务消息之下，负责建立连接时的身份协商、心跳保活、序号同步和断线后续接，相当于在一条不可靠的网络上开出一条可恢复的逻辑会话。

**序号**是同一个方向、同一条流里逐条递增的编号，接收方靠它发现丢了哪几条、哪些是重复到达。

**ClOrdID** 是客户自己给每个订单请求分配的唯一编号，回报里会原样带回来，断线重连后对账主要靠它，而不是靠"我大概几分钟前发过一笔"。

**快照**是某一时刻的完整状态，**增量**是此后一条条修改；恢复时先拿到快照，再把快照位置之后的增量接上。二进制编码指字段按固定字节宽度、固定大小端排列，省带宽但必须按协议模板解码。

**多播**是一次发送、一组订阅者同时收到的 UDP 传输，交易所高频行情常用，但 UDP 本身不重传，丢包要靠应用层补。

## 2. 传统证券交易所：Nasdaq 原生协议与国内证券网关

### 2.1 报单与回报：OUCH 跑在 SoupBinTCP 上

具备直连资格的客户，在 Nasdaq 用 OUCH 协议报单，承载它的是 SoupBinTCP。先看承载层。SoupBinTCP 在 TCP 字节流上给每条消息加一个两字节大端长度前缀，一次 recv 可能只收到半条消息，也可能收到好几条，解析器要先攒够长度再取Payload。它把消息分成两个方向：服务器发给客户的是带序号的 Sequenced Data Packet，双方按收到的包计数；客户发给服务器的是 Unsequenced Data Packet，不计入这条服务器序列。

OUCH 的新单请求里带客户自己生成的 client order token（作用等同于 ClOrdID）、股数、限价、买卖方向和有效期类型。服务器回 Accepted 表示订单被接受，执行过程中回 Executed，撤单回 Canceled，替代改单回 Replaced。撤单请求也是带上原来的 client order token，不需要知道交易所内部的订单号。订单被替代后旧 token 失效、新 token 生效，本地台账要跟着换。

[![客户发新单并收到 Accepted 后网络断开；重新登录时带上会话名和期望序号，服务器从该序号续推，补到 Executed，客户端不重发原单](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/nasdaq-ouch-session.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/nasdaq-ouch-session.png)

这张图里最关键的是最后一步。连接断在 Accepted 和 Executed 之间时，本地只知道"请求结果未知"，不能假设没成交就再发一笔，也不能假设已成交就直接推持仓。Soup 的带序号流可以从期望位置续推回报，续不上的部分再靠查询核对。

### 2.2 行情：ITCH over MoldUDP64，用 GLIMPSE 完成快照衔接

公开行情走另一条路。Nasdaq TotalView-ITCH 是逐笔行情协议，经 MoldUDP64 封装成 UDP 数据报发出。MoldUDP64 包头依次是十字节 ASCII 会话名、八字节首条消息序号、两字节本包消息数，整数全部大端；一个数据报里可以装多条 ITCH 消息，序号连续排下去。UDP 不保证送达也不保证顺序，接收方发现序号缺口时，要么走开通的重传服务补包，要么放弃连续历史、重新取快照。

ITCH 按事件描述显示订单的进出。新增显示订单是 A，成交是 E，带价格的成交是 X，替代改单是 U（带旧订单号、新订单号、新数量和新价格），删除整笔剩余是 D，非显示订单的成交走 P。因为消息以单笔订单为单位，本地可以先按订单引用号保存剩余量，再聚合出价位总量，这是逐笔订单簿（MBO）的做法；只按价位汇总数量则是按价位订单簿（MBP）。

启动时本地没有任何历史，直接从第一条增量接会缺前面的全部挂单。Nasdaq 的做法是用 GLIMPSE：另开一条 SoupBinTCP 连接接收现存显示订单的快照，快照流末尾的 G 消息给出实时 ITCH 的起始序号 N，含义是从实时流的第 N 条开始处理（含第 N 条）。本地一边缓存实时 ITCH，一边收 GLIMPSE 快照，收到 G 后按 N 把缓存增量接上，再开始消费实时流。快照和增量的交接位置必须由这个序号钉住，不能用时间戳去猜"大概从哪条开始对得上"。

### 2.3 FIX 与国内证券网关

不是所有客户都用交易所原生协议。FIX（Financial Information eXchange）是一套跨场所通用的报单语言，原生二进制协议（OUCH、ITCH）性能更好，但只在特定场所内成立；FIX 胜在一套语义可以对接多家券商、跨境网关。FIX 不只是一套字段字典，它同时规定了会话层怎么建立、序号怎么对齐、断线后怎么补，这一节先把会话状态转换讲清楚，再把两种编码（FIX/STEP 的 tag-value 与深交所 Binary 的固定宽度）各自的 payload 摊开看。国内证券市场由机构通过交易网关接入：上交所的 STEP 沿用 FIX 风格，深交所 Binary 用固定宽度二进制字段，普通客户则落在券商提供的客户端产品里。

#### 2.3.1 FIX 会话：Logon、序号同步与断线重传

FIX 会话层跑在 TCP 之上，为每个方向各维护一对落盘的序号 NextNumOut（下一条要发的序号）和 NextNumIn（期望从对端收到的下一条序号），序号从 1 开始；把两个序号都重置为 1 才算开启一个全新会话。连接建立后第一条消息必须是 Logon（`35=A`），接收方拿 Logon 里的 `MsgSeqNum(34)` 和自己的 NextNumIn 比较，由此决定是正常确认、注销拒绝还是发起补发。

[![客户 OMS 与网关之间 Logon 的三种序号关系、心跳保活，以及断线后不重置序号重连并由 ResendRequest 补齐缺口](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/fix-step-session.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/fix-step-session.png)

三种序号关系要分清：来包序号正好等于期望值，回 Logon 确认，会话建立；低于期望值，说明序号倒退，直接 Logout（`35=5`）断开；高于期望值，说明中间有缺口，仍先确认 Logon，再发 ResendRequest（`35=2`），用 `BeginSeqNo(7)` 和 `EndSeqNo(16)` 圈出要补的区间（EndSeqNo=0 表示一直补到最新）。补发时有两类处理：仍有业务价值的消息按原序号重放，并打上 `PossDupFlag(43)=Y`，接收方据此跳过已处理的内容；心跳这类没有重放价值的消息不重发，改用 SequenceReset（`35=4`）配合 `GapFillFlag(123)=Y` 和 `NewSeqNo(36)` 把缺口直接填过去。保活靠 Heartbeat（`35=0`），空闲时按协商间隔发送，也可以用 TestRequest（`35=1`）强制对端回一条带相同 TestReqID 的心跳；任一方在 2 个心跳间隔内没收到任何消息，就判定连接异常并断开。`ResetSeqNumFlag(141)=Y` 只在显式开启新会话、双方约定把序号归零时使用，不能拿它当断线恢复的手段——正常重连必须带着持久化的下一个序号重新 Logon，再让 ResendRequest 把断线期间的执行报告补回来。

#### 2.3.2 一笔限价单在本地台账里的状态机

会话序号解决“消息有没有漏”，订单状态解决“这笔单现在到哪一步”，两者是两层东西，不能互相替代。一笔新单由 NewOrderSingle（`35=D`）发出后，交易所通过执行报告 ExecutionReport（`35=8`）驱动本地状态迁移，撤单是 OrderCancelRequest（`35=F`）。

[![一笔限价单从已发未确认到受理、部分成交、全部成交，以及撤单与成交竞争时的状态迁移](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/fix-order-state.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/fix-order-state.png)

发出 `35=D` 后本地先持久化客户订单号 ClOrdID（`11`），状态置为“已发未确认”；这个状态下超时只能查询、不能用同一 ClOrdID 重发。`35=8` 里的 OrdStatus 决定迁移：0 是新确认（已受理），1 是部分成交，2 是全部成交，4 是已撤销；剩余未成交数量看 LeavesQty，每一笔成交用成交编号 ExecID（`17`）去重，重连重放里重复出现的同一笔成交只能入账一次。撤单请求带 OrigClOrdID（`41`）指向原单，发出后进入“待撤”，此时撤单和成交可能竞争：先成交一部分就落到部分成交，撤单被拒则回到可交易状态，不能假设发了撤单就一定撤得掉。再强调一次那条主线：会话层的 `MsgSeqNum(34)` 管传输连续性，业务层的 ClOrdID/ExecID 管订单与成交对账；2.1 里“不盲目重发”的原则，在 FIX 里就是靠这两层各管一段实现的。

#### 2.3.3 tag-value 的 payload 长什么样：FIX 与 STEP

FIX 经典编码是自描述的 tag-value：一个字段就是 `标签=值` 后面跟一个不可打印的分隔符 SOH（ASCII `0x01`），整体是 ASCII 文本、变长、字段之间没有任何对齐填充。解析器只能从消息头开始顺序扫描，遇到 SOH 才知道一个字段在哪里结束，因此字段顺序必须符合规范，重复组尤其严格。消息由标准头、消息体、标准尾组成：头部固定以 BeginString（`8`，如 `FIX.4.4`）开头、BodyLength（`9`）紧随其后，尾部固定以 CheckSum（`10`）收尾。

下面构造一笔最常见的 NewOrderSingle（`35=D`）：以 186.40 买入 AAPL 100 股、限价当日有效（仅用于演示字段布局，真实场所字段更多）。BodyLength 数的是“`9=` 这个域的 SOH 之后、到 `10=` 之前最后一个 SOH（含）”之间的字节数，本例为 135；CheckSum 是从消息开头第一个字符 `8` 一直累加到 `10=` 之前那个 SOH，按 256 取模，本例为 147，写成三位 `10=147`。整条消息共 158 字节，各字段在线路上的偏移如下：

| 偏移 | 长度 | 线上字段（结尾 SOH 未显示） | 含义 |
| --- | --- | --- | --- |
| 0 | 10 | `8=FIX.4.4` | BeginString，协议起始串，固定首域 |
| 10 | 6 | `9=135` | BodyLength，消息体字节数 |
| 16 | 5 | `35=D` | MsgType，新订单单笔 |
| 21 | 9 | `49=BUYER` | SenderCompID，发送方 |
| 30 | 10 | `56=BROKER` | TargetCompID，接收方 |
| 40 | 5 | `34=7` | MsgSeqNum，本方向第 7 条 |
| 45 | 25 | `52=20261020-09:30:00.123` | SendingTime |
| 70 | 11 | `11=ORD0001` | ClOrdID，客户订单号，对账主键 |
| 81 | 5 | `21=1` | HandlInst |
| 86 | 8 | `55=AAPL` | Symbol，标的代码 |
| 94 | 5 | `54=1` | Side，1=买 |
| 99 | 25 | `60=20261020-09:30:00.123` | TransactTime |
| 124 | 5 | `40=2` | OrdType，2=限价 |
| 129 | 7 | `38=100` | OrderQty，数量 |
| 136 | 10 | `44=186.40` | Price，限价（ASCII 文本） |
| 146 | 5 | `59=0` | TimeInForce，0=当日有效 |
| 151 | 7 | `10=147` | CheckSum，固定末域，三位 |

把 SOH 换成可读的竖线，整条消息长这样（线上 `|` 处实际都是一个字节 `0x01`，没有换行）：

```text
8=FIX.4.4|9=135|35=D|49=BUYER|56=BROKER|34=7|52=20261020-09:30:00.123|11=ORD0001|21=1|55=AAPL|54=1|60=20261020-09:30:00.123|40=2|38=100|44=186.40|59=0|10=147|
```

上交所交易网关用的 STEP（证券交易数据交换协议）在编码和会话机制上与 FIX 同源：同样是 tag-value、同样用 SOH 分隔、同样靠 BodyLength 与三位 CheckSum 保证完整性、同样走 Logon/Heartbeat/ResendRequest/SequenceReset 这一套会话状态转换，所以一个 FIX 解析器骨架可以直接复用。差异主要在取值和业务字段：起始串用 `FIXT.1.1`；Logon 里要带 `98`（EncryptMethod=0）、`108`（HeartBtInt）、`141`（ResetSeqNumFlag）、`789`（NextExpectedMsgSeqNum）、`1137`（DefaultApplVerID）和自定义版本 `1408`（形如 `STEP1.20_SH_n.xy`），心跳间隔以网关登录确认里回的 HeartBtInt 为准（OMS 取 5～60 秒）；新单仍用 `35=D`，但方向、账户、交易单元要通过 `453` NoPartyIDs 重复组里的 PartyID(448)/PartyRole(452) 表达，债券价格精度为 N13(5)、数量为 N15(3)。这些字段长度和取值随接入版本变化，落地时以对应的上交所/券商接口规格为准，不能照搬国际 FIX 的字段宽度。

#### 2.3.4 深交所 Binary：固定宽度的内存布局

深交所 Binary 走另一条路：所有整数用固定字节宽度、高字节序（大端，BIG-ENDIAN）排列，字符串是定长 char 数组（不足右侧补空格、UTF-8），字段之间没有分隔符也没有对齐填充。一条消息由消息头、消息体、消息尾三段组成，头部是 MsgType（uInt32）+ BodyLength（uInt32），尾部是 Checksum（uInt32），Checksum 从消息头第一个字节起逐字节求和再对 256 取模。因为每个字段的宽度和偏移在协议里写死，接收方不必扫描分隔符，按偏移直接切、按大端直接转整数即可，这也是它比 tag-value 更省字节、解析更快的原因。

以现货逐笔委托（300192）为例：频道 1、本频道记录号 ApplSeqNum=100、证券 000001、限价 18.6400 元买 100 股。价格和数量都不是用浮点线上传输，而是带定标的整数——Price 类型是 N13(4)，即整数 186400 表示 18.6400；Qty 类型是 N15(2)，即整数 10000 表示 100.00。消息体 51 字节、整条 63 字节，内存布局如下：

| 偏移 | 字节 | 字段 | 类型 | demo 值 |
| --- | --- | --- | --- | --- |
| 0–3 | 4 | MsgType | uInt32 大端 | 300192（`00 04 94 a0`） |
| 4–7 | 4 | BodyLength | uInt32 大端 | 51（`00 00 00 33`） |
| 8–9 | 2 | ChannelNo | uInt16 | 1 |
| 10–17 | 8 | ApplSeqNum | Int64 | 100，频道内消息记录号 |
| 18–20 | 3 | MDStreamID | char[3] | `011`（现货集中竞价逐笔） |
| 21–28 | 8 | SecurityID | char[8] | `000001  `（右补空格） |
| 29–32 | 4 | SecurityIDSource | char[4] | `102 `（深交所） |
| 33–40 | 8 | Price | Int64，N13(4) | 186400 → 18.6400 |
| 41–48 | 8 | OrderQty | Int64，N15(2) | 10000 → 100.00 |
| 49 | 1 | Side | char | `1`=买（`2`=卖） |
| 50–57 | 8 | TransactTime | Int64 | 20261019093000123（年月日时分秒毫秒） |
| 58 | 1 | OrdType（扩展域） | char | `2`=限价（`1`=市价） |
| 59–62 | 4 | Checksum | uInt32 | 160（`00 00 00 a0`） |

对应的裸字节（竖线只用来分隔头、体、尾，线上不存在）：

```text
00 04 94 a0 00 00 00 33 | 00 01 00 00 00 00 00 00 00 64 30 31 31 30 30 30 30 30 31 20 20 31 30 32 20 00 00 00 00 00 02 d8 20 00 00 00 00 00 00 27 10 31 00 47 fb 4a 1d d3 5f bb 32 | 00 00 00 a0
```

逐笔成交（300191）用同一套定长规则，消息体 66 字节、整条 78 字节；字段顺序为 ChannelNo、ApplSeqNum、MDStreamID、BidApplSeqNum、OfferApplSeqNum、SecurityID、SecurityIDSource、LastPx、LastQty、ExecType（`F`=成交、`4`=撤销）、TransactTime。它没有自己独立的委托号，而是用买方委托索引 BidApplSeqNum 和卖方委托索引 OfferApplSeqNum 指回本频道里对应的逐笔委托；逐笔委托和逐笔成交在同一频道共用一套从 1 连续递增的 ApplSeqNum，这正是本地把成交关联回原始委托的依据。

逐笔流的接收是一个明确的状态机，游标就是“频道 + ApplSeqNum”：

[![登录后按频道 ApplSeqNum 实时同步；新序号小于等于已收则去重，大于期望则挂起并通过 390094 申请重传，补不上再重建快照](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/szse-binary-receive-state.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/szse-binary-receive-state.png)

收到的记录号小于等于本频道已收最大值，说明是重复包，直接忽略；正好等于期望值（已收最大值+1）才应用并推进游标；大于期望值+1（如已收 10 却来了 12）就判定丢包，向行情网关发重传消息 390094（ResendType=1，填 ChannelNo 和 ApplBegSeqNum/ApplEndSeqNum，ApplEndSeqNum=0 表示补到当前最大值），缺口期间的实时包先缓存，补齐后再按序接上，重传返回无权限或数据不可用则放弃连续历史、重新取快照。网关每 3 秒还发频道心跳 390095，带 ApplLastSeqNum 和频道结束标志，2 个心跳间隔收不到任何消息就按断线处理。最后保留两条此前的提醒：其一，周期快照（如 300111 十档）和逐笔流没有天然的交接游标，没开通明确衔接机制时，不要把一份快照和某个时间点之后的逐笔消息直接拼接；其二，交易侧的私有回报另按平台分区保存 PartitionNo/ReportIndex，每个分区内记录号连续递增，重连后按分区逐条请求尚未处理的下一条，这个业务游标独立于 TCP 重建、频道心跳和行情 ApplSeqNum。

## 3. 期货交易所：CME iLink 3 / MDP 3.0 与国内 CTP 柜台

### 3.1 报单：iLink 3，FIXP 会话层加 SBE 业务消息

CME 的订单接入是 iLink 3，它在 TCP 上跑 FIXP 会话层，业务消息用 SBE（Simple Binary Encoding）二进制编码。FIXP 负责会话：连接建立时先 Negotiate 协商，再 Establish 绑定；一个逻辑会话用一个 64 位 UUID 标识，配合双向序号保存会话进度。这里的 UUID 是按规范设置的会话标识，不是随便生成的随机字符串，一个逻辑会话可以经历多次连接建立，接入程序要把"连接对象"和"可恢复会话记录"分开存。

服务器发给客户的业务流可恢复：假设本地连续处理到序号 50，接下来直接收到 54，就用会话 UUID、起始 51 和请求数量申请补发，同时缓存实时消息，补发消息保留原序号并带 PossRetransFlag 标识。客户发给 CME 的方向是幂等流语义，交易所发现客户序号跳跃时回 NotApplied，由本地根据这条序号曾经对应什么请求来决定怎么处理。行情恢复和订单恢复各用一套身份：MDP 按频道包序号和标的更新号恢复市场状态，iLink 按 UUID 和业务序号恢复私有回报，不要混用。

### 3.2 行情：MDP 3.0 多播 A/B、增量缺口与快照恢复

CME 行情是 MDP 3.0，SBE 编码后走多播 UDP，并且发 A、B 两路冗余。两路内容相同同时送达，本地对同一个包只应用一次，一路丢包时另一路可能已经补到。每条多播数据报带包序号 MsgSeqNum，接收方按它检测缺口。

[![包序号连续则直接应用；小缺口靠 A/B 互补或 TCP Recovery 补包；缺口过大进入 Market Recovery 循环收快照，水位覆盖缓存增量后安装快照并按 RptSeq 衔接](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/cme-mdp-recovery.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/cme-mdp-recovery.png)

恢复分两层。较小缺口由 TCP Recovery 按原包补发，这是增量历史的补全；缺口超出补发范围时进入 Market Recovery，独立通道循环发送各标的的完整快照。快照里带 LastMsgSeqNumProcessed 水位和该标的的 RptSeq（标的自身的更新序号），本地先把实时增量缓存起来，收到某个标的的快照后比较水位：快照之前的包已被快照覆盖，水位之后的缓存增量逐条接上，再用 RptSeq 核对位置。一个撮合事件可能跨多条消息、跨多个包，MatchEventIndicator 的结束位标记事件最后一条，本地可以先改候选状态，等事件收齐才通知策略。

### 3.3 国内期货：CTP 柜台接入

国内散户和绝大多数程序化客户并不直连交易所，而是连期货公司部署的 CTP（综合交易平台）柜台。CTP 分交易 API 和行情 API，应用实现 SPI 回调接收异步响应。行情侧创建 MdApi、注册行情前置、登录、订阅合约后，在 OnRtnDepthMarketData 回调里拿到快照式深度行情，字段给最近价、累计成交量和一档买卖盘；标准行情接口没有按段申请历史增量的能力，断线期间的逐笔历史要靠独立记录或另行授权的数据服务。

交易侧先 ReqAuthenticate，用期货公司提供的 AppID/AuthCode 完成客户端认证再登录。报单刚发出时，客户手里只有本次登录会话内递增的 OrderRef；交易所接受后回报提供 ExchangeID 加 OrderSysID，本地把两组身份挂到同一条订单记录上，跨会话撤单和查询都靠这组映射。重新登录后会话身份变了，旧会话的 OrderRef=1 和新会话的 OrderRef=1 是两笔不同的单，不能只拿 OrderRef 当订单主键。私有流负责报单和成交回报，交易日内支持重传，三种订阅模式决定重启后从哪里接：RESTART 从当日开始重放、RESUME 从流文件记录位置续接、QUICK 只收本次登录之后。要注意流文件的传输进度和本地持久台账之间有竞争，SDK 已写游标而应用还没落账时崩溃，RESUME 会越过这笔成交，所以业务恢复仍要以自己的成交台账和查询对账为准。

## 4. Web3：中心化交易所、链上 AMM 与订单簿型 DEX

### 4.1 CEX 与 DEX 的职责和资金控制

中心化交易所（Centralized Exchange，CEX）与 DEX 的核心差别，在于谁控制资金、谁执行交易，以及成交结果记在哪里。本节的 CEX 指 Coinbase、Binance 这类加密资产交易平台：平台托管客户资产，运行撮合系统，并在内部账本中记录客户余额。前面的证券、期货市场还涉及券商、期货公司和清算机构，不能直接套用“交易所统一保管客户资金”的组织方式。

DEX 把交易规则交给链上协议执行，用户通过钱包签名授权资产操作。“去中心化”与“使用订单簿”描述的是不同维度：Uniswap 的 AMM 资金池用公式执行兑换，dYdX Chain 这类订单簿型 DEX 仍然撮合买卖订单。以 dYdX Chain 为例，节点在内存中维护订单簿，区块提议者提交撮合结果，再由验证器共识确认；并非每条挂单都先落到链上，再等待另一笔链上交易来撮合。[dYdX 节点、订单簿与成交确认](https://docs.dydx.exchange/introduction-onboarding_faqs)

区块链提供转账、程序执行和状态确认能力，但一个可交易的市场还需要对手盘资金、定价规则和交割机制。AMM 的流动性提供者（Liquidity Provider，LP）提前把资产存入资金池，并取得相应的份额或头寸。交易者向池子支付一种资产，从池子取走另一种资产；经济上的对手方是提供库存的 LP，DEX 协议提供共同使用的交易规则。[Uniswap 资金池与流动性](https://developers.uniswap.org/docs/get-started/concepts/how-uniswap-works)

| 职责 | 中心化现货订单簿交易所 | AMM 型 DEX 协议 |
| --- | --- | --- |
| 组织流动性 | 汇集客户和做市商的挂单，维护买卖价位及排队顺序 | 管理 LP 存入的资产、份额或头寸，处理流动性的加入与退出 |
| 形成成交价格 | 按撮合规则选择可成交的对手订单 | 按池子状态、定价曲线、交易量和费用计算兑换数量 |
| 检查交易条件 | 检查账户余额、订单价格、数量和有效期等约束 | 池子合约检查资金与定价约束，路由合约检查最大支付量、最低到账量和期限等条件 |
| 执行资产交割 | 增减买卖双方的内部账户余额 | 在链上交易中执行代币转移、更新池子状态，并向指定地址交付资产 |
| 记录费用 | 从客户账户扣取交易费，按平台规则处理 | 记录 LP 应得交易费；启用协议收费时，按合约规则分配相应部分 |

资金池里的资产已经进入合约，由合约规则约束其使用；LP 并非仍把这些资产留在自己的钱包里等待别人付款。交易者则可以在兑换前把资产留在自己的地址，通过授权让合约在交易中转移所需数量。对于使用保证金账户、专用链或跨链桥的 DEX，资金还可能先进入协议账户或桥合约，所以“所有 DEX 都无需充值、资产始终在个人钱包里”也不成立。

### 4.2 同一笔交易：用 USDC 买入 1 ETH

对照一笔具体交易：你的钱包有 3,000 USDC，另有支付网络费所需的少量 ETH，准备最多花 2,020 USDC 买入 1 ETH，实际成交花费 2,000 USDC。USDC 是用于支付的美元稳定币，ETH 是以太坊的原生资产。数字仅用于演算，余额表暂不计交易费和网络费；中心化一侧按 Coinbase 公布的撮合与结算规则说明，去中心化一侧采用以太坊上的 Uniswap v2 路由与资金池。

| 阶段 | 中心化现货订单簿交易 | Uniswap v2 资金池兑换 |
| --- | --- | --- |
| 进入交易入口 | 登录平台账户，满足平台开户和身份验证要求 | 连接自己控制的钱包，客户端读取地址与余额 |
| 准备资金 | 把 3,000 USDC 转入平台充值地址，平台确认链上到账后记入交易账户 | USDC 保留在自己的链上地址；授权指定路由合约使用足够的 USDC |
| 提交交易条件 | 提交买入 1 ETH、限价 2,020 USDC 的订单，平台检查并占用相应余额 | 设置目标输出 1 ETH、最大输入 2,020 USDC、接收地址和期限，签名后提交链上交易 |
| 执行成交 | 订单簿已有卖方以 2,000 USDC 出售 1 ETH，撮合引擎按该挂单价格成交 | 交易被纳入区块并执行时，合约按池子当时的状态计算所需输入，检查通过后完成兑换 |
| 记录结果 | 内部账户扣除 2,000 USDC、增加 1 ETH，释放未使用的占用余额 | 从你的地址转出 2,000 USDC，通过路由向指定地址交付 1 ETH，相关资产状态一起更新 |
| 使用所得资产 | 可以在平台内继续交易；转到自己的钱包还需申请提现，由平台发起链上转账 | 所得 ETH 已到达指定地址，无需再向网站运营者申请提现 |

中心化交易所接受订单、完成撮合和处理提现，是三个不同的完成点。充值时，你先签名把 USDC 转到平台控制的地址，平台再把到账金额映射到你的客户账户。下单时发给平台的是账户交易指令；平台检查余额、冻结订单所需资金，再进入撮合。它可以处理很多笔内部买卖，而不为每笔成交生成一笔链上资产转账。[Coinbase 充值流程](https://help.coinbase.com/en/exchange/crypto-transfers/depositing-to-coinbase-exchange)、[撮合与订单状态](https://docs.cdp.coinbase.com/exchange/concepts/matching-engine)

本例成交后，平台内部账本的变化如下。卖方也已把待售资产放在平台账户中：

| 内部账户 | USDC 变化 | ETH 变化 |
| --- | --- | --- |
| 你的账户 | −2,000 | ＋1 |
| 卖方账户 | ＋2,000 | −1 |

你的平台余额因此变成 1,000 USDC 和 1 ETH，但这 1 ETH 仍由平台托管。平台控制链上地址的签名权限，你通过账户请求提现，平台再签署链上转账。Coinbase 的规则明确将成交结算描述为增减双方账户余额；“平台内可以买卖”和“资产已到自己控制的链上地址”发生在不同阶段。[Coinbase 成交与结算规则](https://www.coinbase.com/legal/trading_rules)

DEX 一侧的授权也有明确边界。普通钱包首次使用 v2 路由时，先向 USDC 代币合约发送 `approve(spender, amount)`，记录指定路由可以动用的额度；已经存在足够额度时可以跳过。这一步改变的是使用权限，USDC 还没有进入资金池，ETH 也没有买到。随后签署的兑换交易才调用路由，由它在授权范围内转移 USDC 并执行兑换。[ERC-20 的 approve 与 transferFrom](https://eips.ethereum.org/EIPS/eip-20)

v2 池子实际交易的是 USDC 与封装以太币（Wrapped Ether，WETH）。WETH 可与 ETH 按 1:1 封装、解封，路由可以在同一笔兑换交易中将取得的 WETH 解封，并把 ETH 发给你的地址。因此界面可以显示“买入 ETH”，而合约调用内部仍使用符合代币接口的 WETH。[Uniswap v2 路由与 WETH](https://developers.uniswap.org/docs/protocols/v2/concepts/architecture)

这笔兑换成功后，你的链上地址剩余 1,000 USDC，并增加 1 ETH；资金池增加相应 USDC、减少相应 WETH。这里的**原子性**是指本次兑换涉及的资产转移和合约状态更新一起成功，或者一起回滚。若执行时购买 1 ETH 已需 2,030 USDC，超过设置的 2,020 上限，v2 路由会让兑换失败；已消耗的网络执行费仍需支付。先前单独成功的授权交易也不会随这次兑换失败而消失。[兑换输入上限](https://developers.uniswap.org/docs/get-started/concepts/traders/swaps)、[REVERT 的回滚语义](https://eips.ethereum.org/EIPS/eip-140)

**原子性约束一次执行内部是否完整交换资产，最终确认约束记录这次执行的区块是否已经获得共识上的确定性。** 客户端拿到交易哈希，只能追踪这笔已签名交易；拿到成功回执，才能看到它在某个区块中的执行结果；该区块进一步最终确认后，才达到更强的结算确定性。确认前若发生链重组，本地记录还要跟随新的规范链修正。[以太坊交易生命周期](https://ethereum.org/developers/docs/transactions/)

从交易系统接入和延迟统计看，两种机制会带来以下差别：

| 观察点 | 中心化现货订单簿交易 | 上述链上 AMM 兑换 |
| --- | --- | --- |
| 谁提供对手盘 | 提交卖单的客户或做市商 | 提前存入库存的 LP |
| 价格如何变化 | 对手订单的新增、撤销和成交改变可交易深度 | 交易及流动性变化改变池子状态，下一笔兑换重新计算数量 |
| 每笔成交是否等待区块 | 内部撮合和账户结算无需逐笔等待区块 | 兑换执行结果取决于交易收录；最终确认还需观察链的共识状态 |
| 价格条件暂时不满足 | 普通限价单可继续挂在订单簿中，也可按订单有效期规则结束 | 这次调用失败并回滚，不留下等待未来成交的普通限价挂单 |
| 哪些信息可独立核验 | 链上可看充值和提现；客户内部余额、成交明细依赖平台账本与回报 | 可按区块、交易回执、事件及合约状态核验这笔兑换 |
| 继续操作依赖什么 | 平台账户、撮合和提现服务正常工作 | 钱包签名权限、相关合约和底层链正常工作；网站可由其他兼容客户端替代 |

因此，测量“下单延迟”时要写清终点：中心化平台的请求受理、撮合成交、账户入账和提现到账分别计时；链上交易的提交响应、成功回执和最终确认也分别计时。把中心化平台返回“已受理”的耗时与链上最终确认耗时放在同一列，会把不同的业务阶段混在一起。

### 4.3 AMM 的兑换请求与执行条件

Uniswap 的基础 AMM 池子不维护逐笔挂单价位表，客户端提交的是签名兑换交易。通过 JSON-RPC 的 `eth_sendRawTransaction` 可以把交易广播到节点的待处理交易池（mempool，节点内存里等待打包的签名交易集合），再由区块收录执行；也可以使用私有提交通道。合约按执行时的池子状态计算兑换数量：v2 在忽略费用时遵循恒定乘积 `x·y=k`，v3 把流动性集中在价格区间内，跨越 tick 时切换有效流动性。

[![计算滑点下限并签名后，交易经公开 mempool 或私有 relay 进入区块；输出低于下限则 revert，否则 Swap 与 Sync 事件上链，本地经 eth_subscribe 更新池子状态](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/amm-swap-flow.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/amm-swap-flow.png)

固定输入数量的兑换带 `amountOutMin`（v2 路由）或 `amountOutMinimum`（v3 路由），表示最低可接受输出；固定输出数量的兑换则用 `amountInMax` 或 `amountInMaximum` 限制最高输入，前面购买 1 ETH 的交易属于后一种。超出价格条件时兑换回滚，带 `deadline` 的路由调用还会拒绝过期执行。网络执行资源用 gas 计量，费用上限和优先费会影响交易的收录机会；提高费用也无法保证它一定进入指定区块。[Uniswap 路由参数](https://developers.uniswap.org/docs/protocols/v2/guides/swapping)、[以太坊 gas 与费用](https://ethereum.org/developers/docs/gas/)

基础 AMM 的兑换请求没有订单簿式的撤单回报。以太坊普通账户的交易还在等待收录时，可以提交使用**相同 nonce**、费用更高的替换交易来加速，也可以尝试用一笔向自己转账 0 ETH 的交易占用该 nonce，从而取消原兑换。nonce 在这里是账户交易序号；替换交易和原交易竞争收录，最终以链上实际执行的那笔为准。已经执行并最终确认的兑换无法通过这种方式撤回。[钱包的加速与取消机制](https://support.metamask.io/manage-crypto/transactions/how-to-speed-up-or-cancel-a-pending-transaction/)

交易顺序还会影响报价和收益。最大可提取价值（Maximal Extractable Value，MEV）指通过选择、排除或调整区块内交易顺序，在常规区块奖励和执行费之外获取的价值。公开 mempool 暴露待执行交易的内容，机器人可以尝试在一笔大额兑换前后安排交易，利用这笔兑换对池子价格的影响获利，这就是三明治攻击。交易者的输入上限或输出下限约束了可接受的最差成交结果，但上限之内仍可能出现劣于提交前报价的成交。[以太坊 MEV 与三明治交易](https://ethereum.org/developers/docs/mev/)

私有交易通道可以减少交易在执行前的公开暴露。例如 Flashbots 的 `eth_sendPrivateTransaction` 经私有服务把交易交给区块构建者，避免直接进入公开 mempool；具体保护取决于服务的转发和构块规则。提交成功后仍需跟踪收录与执行结果，滑点约束、费用预算和最终确认照常生效。[Flashbots Protect](https://docs.flashbots.net/flashbots-protect/overview)

### 4.4 链上行情：事件订阅代替盘口推送

AMM 没有盘口可推，行情来自合约事件日志。v2 池子每次兑换发 Swap 事件（amount0In、amount1In、amount0Out、amount1Out），每次余额变动发 Sync(reserve0, reserve1)，储备量就是最新汇率的来源；v3 池子的 Swap 事件带 amount0、amount1、sqrtPriceX96（兑换后价格平方根的 Q64.96 定点数）、liquidity 和 tick（以 1.0001 为底的价格对数）。接入程序通过 WebSocket 上的 eth_subscribe 订阅 logs，按池子地址和事件主题过滤，自己维护池子状态、自己算价格。想观察待打包交易，可以订阅 newPendingTransactions 拿到进入 mempool 的交易哈希，再按需取交易内容。

本地保存的是池子储备、当前价格和 tick 上的流动性分布，恢复游标来自区块及日志位置。WebSocket 订阅只推实时事件，断线后重新订阅不会自动补回历史；接入程序保存最后处理的区块高度、区块哈希和日志位置，再通过 `eth_getLogs` 按区块范围补齐，并与实时流去重衔接。链重组（reorg）时，订阅可能把旧分支日志以 `removed=true` 重发，本地要回滚旧分支并应用新分支；断线期间错过这些通知时，还要根据区块哈希检查分支变化。交易在某个区块内执行成功与该区块最终确认是不同状态。[Geth 订阅与重组通知](https://geth.ethereum.org/docs/interacting-with-geth/rpc/pubsub)、[按区块查询日志](https://ethereum.org/developers/docs/apis/json-rpc/#eth_getlogs)

### 4.5 订单簿型 DEX：dYdX v4 与 Hyperliquid

订单簿型 DEX 通过节点处理签名订单，并把成交纳入链上状态。订单传播、内存撮合和共识确认可以处在不同阶段；接口使用 HTTP、gRPC 或 WebSocket，并不能直接说明资产由谁托管，也不能说明返回值对应哪一个完成点。

dYdX v4 运行在自己的 dYdX Chain 上，底层是 CosmosSDK 加 CometBFT 共识，验证器在内存里维护订单簿，按价格时间优先撮合，proposer 约每秒一个区块。客户端把签名订单通过公开的 gRPC 端点（OEGS 网关，单跳送达 proposer 集合）提交，撤单同样是签名请求，每笔订单带一个客户端自己生成的 clientId，之后用它和回报对账。行情和账户数据由 indexer 通过 REST 和 WebSocket 提供，Orders 通道先推 Initial 快照再推 Update，每条消息带 message_id。

Hyperliquid 运行在自有 L1 上，交易请求统一 POST 到 /exchange，body 里是 action（type=order 或 cancel）、nonce 和签名。nonce 在这里推荐取当前毫秒时间戳，每地址保留最近 100 个 nonce，新 nonce 必须比集合里最小的大且未用过；order 动作里 a 是资产号、b 是是否买入、p 是价格、s 是数量、c 是客户订单号 cloid，cancel 动作按 oid 撤单。行情走 wss://api.hyperliquid.xyz/ws，订阅 l2Book 收到的是整簿 levels 快照式推送（不是逐档增量叠加），私有回报走 orderUpdates、userFills 通道。

[![客户端提交带 nonce 和签名的 order 动作并收到同步响应，再通过 WebSocket 订阅 l2Book、orderUpdates、userFills，收到整簿快照和私有推送](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/dex-order-flow.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/dex-order-flow.png)

断线恢复要按具体协议核对订单和成交，不能把网关响应统一解释为“仅接受请求”：Hyperliquid 的同步订单响应就可以包含 `resting`、`filled` 或 `error`，HTTP 200 本身不足以判断成交。客户端按 `clientId`、`cloid` 或场所订单号关联请求、推送和查询结果，重复出现的成交只入账一次。Hyperliquid 的 nonce 还承担防重放功能，已使用的值会被拒绝；请求超时后先查原订单结果，只有业务上确实要提交另一笔操作时才生成新请求，直接换 nonce 重发相同买单可能造成重复成交。[Hyperliquid 响应结构](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)、[nonce 规则](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets)

### 4.6 中心化交易所的 WebSocket 行情与执行回报（Binance 现货）

作为对照，中心化交易所（以 Binance 现货为例）行情和交易都用 JSON：公开深度行情走 WebSocket 推送，完整快照走 RESTful API 拉取，私有订单回报走需要鉴权的用户数据流。payload中的 `U`、`u`、`c`、`l`、`z` 不是新协议概念，只是 JSON 里为了省字节用的单字母字段名，先看原始报文再逐个对上含义就清楚了。

先看公开行情。一条 diff depth（深度增量）推送长这样：

```json
{
  "e": "depthUpdate",
  "E": 1578965423000,
  "s": "BTCUSDT",
  "U": 157,
  "u": 160,
  "b": [ ["186.40", "1.20"], ["186.30", "0.00"] ],
  "a": [ ["186.50", "0.80"] ]
}
```

字段逐个对应：`e` 是事件类型（depthUpdate 深度变更），`E` 是事件时间，`s` 是交易对；`U` 是这批变更覆盖区间的第一个更新号，`u` 是这批变更覆盖区间的最后一个更新号；`b`（bids）是发生变化的买盘、`a`（asks）是发生变化的卖盘，每个元素是 `[价格, 数量]` 的二元数组。两个要点：价格和数量都用字符串而不是浮点数，是为了避免十进制小数在二进制浮点里的精度问题；数量给的是该价位变更后的**绝对新数量**，不是“在原来基础上加多少”，所以 `"186.30"` 这一档数量给 `"0.00"` 就表示把这个价位整档删掉，而不是减成负数。

完整簿不是推出来的，要先用 REST 拉一次快照，快照的关键字段是 `lastUpdateId`（记成 S，表示这份簿在更新号 S 时刻的状态）：

```json
{
  "lastUpdateId": 160,
  "bids": [ ["186.40", "2.00"], ["186.30", "0.50"] ],
  "asks": [ ["186.50", "1.00"], ["186.60", "0.70"] ]
}
```

本地把“快照 + 之后的增量”拼成实时簿，难点是增量从哪一条开始接才不漏不重。初始化必须**先开 WebSocket 订阅、再请求 REST 快照**：先开流并把这段时间到达的增量全部缓存起来（此时还不知道基线，不能直接往簿上改），拿到快照 S 之后再从缓存里挑出与 S 相邻的那条作为起点。官方对齐规则是：先丢弃缓存里 `u ≤ S` 的旧事件（这些变化已经包含在快照里）；要应用的第一条事件必须同时满足 `U ≤ S+1` 且 `u ≥ S+1`，即它的区间跨过快照的下一个位置；装好快照、应用这条起点事件后，把本地游标更新为它的 `u`。此后每来一条新事件，都要求它的 `U` 正好等于上一条的 `u + 1` 才应用并推进游标；一旦 `U > 上一条 u + 1`，说明中间漏了更新，本地簿已经无法靠后续增量补回，必须立刻冻结这份簿、重新“订阅—拉快照—对齐”整套重来。

[![先订阅并缓存增量、再取快照、丢弃旧事件，按 U/u 找到与快照相邻的起点；之后逐事件校验连续，发现缺口就冻结重建](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/binance-book-sync.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/binance-book-sync.png)

用一组数字串一遍：快照 S=100；缓存里先来一条 `U=98,u=99`，因为 `u ≤ 100` 直接丢弃；下一条 `U=100,u=101` 同时满足 `100 ≤ 101` 和 `101 ≥ 101`，它就是起点，应用后游标=101；再来 `U=102,u=103`，`102 = 101+1` 连续，应用后游标=103；若之后直接收到 `U=106,u=107`，期望值是 104，`106 > 104`，更新号 104、105 丢了，立即重建。这套“快照定基线、增量带区间号、连续性断了就重拍快照”的思路，和前面 Nasdaq 的 GLIMPSE、CME 的 Market Recovery 是同一个模式，只是序号从二进制字段换成了 JSON 里的 `U/u`。

再看私有执行报告。订单状态变化通过用户数据流推一条 `executionReport`，和订单相关的字段同样是短字母：

```json
{
  "e": "executionReport", "s": "BTCUSDT",
  "c": "x-LOCAL-7788", "i": 4293153,
  "x": "TRADE", "X": "PARTIALLY_FILLED",
  "S": "BUY", "p": "186.40", "q": "1.00",
  "l": "0.40", "z": "0.40", "L": "186.40", "t": 88124,
  "T": 1578965423000
}
```

对应到前面各场所的同一套概念：`c`（clientOrderId）是客户下单时自带的订单号，作用等于 FIX 的 ClOrdID；`i`（orderId）是平台分配的场所订单号；`x` 是本次执行类型、`X` 是订单当前状态（NEW/PARTIALLY_FILLED/FILLED/CANCELED 等）；`l`（last executed quantity）是**这一次**成交的数量，`L` 是本次成交价，`z`（cumulative filled）是截至现在的累计成交量，`t`（trade ID）是这笔成交的编号。入账只按本次量 `l` 累加，绝不能直接用累计量 `z`：一笔 1.00 的买单分两次成交，第一笔 `l=0.40,z=0.40`、第二笔 `l=0.60,z=1.00`，按 `l` 加总正好 1.00，若按 `z` 记就变成 0.40+1.00=1.40。`z` 只用来在重连或对账时核对“我本地累计的量和平台是否一致”；去重则以成交编号 `t` 为准——断线重连后的查询结果和实时推送可能重复返回同一笔成交，同一个 `t` 只入账一次。这与 FIX 里用 ExecID、深交所用成交记录号去重完全是一回事。

## 5. 各类市场的协议与完成点对比

把前面的细节压成一张表，差异会比较直观。

| 维度 | 传统证券（Nasdaq/国内网关） | 期货（CME/CTP） | 加密资产 CEX 现货 | 链上 AMM | 订单簿型 DEX |
| --- | --- | --- | --- | --- | --- |
| 报单通道 | OUCH over SoupBinTCP / FIX / STEP、Binary | iLink 3（FIXP+SBE over TCP）/ CTP 柜台 API | 经身份认证的交易 API | 签名交易经公开或私有通道提交 | 签名订单经 gRPC/HTTP 提交 |
| 订单身份 | client token / ClOrdID | UUID + 业务序号 / CTP OrderRef↔OrderSysID | 客户订单号与平台订单号 | 交易哈希；替换请求还关联账户 nonce | clientId / cloid；防重放机制按协议区分 |
| 交易结果 | Soup 带序号续推 / 网关分区游标 | iLink 业务重传、NotApplied / CTP 私有流三模式 | 私有执行报告与订单、成交查询 | 交易回执、事件与确认状态 | 协议响应、私有推送、查询与共识状态 |
| 行情形态 | ITCH 逐笔增量 + GLIMPSE 快照 | MDP 多播增量 A/B + 循环快照 | REST 快照 + WebSocket 深度增量 | Swap/Sync 等事件与池子状态 | 整簿快照推送 / Initial+Update |
| 丢包恢复 | 重传服务或重建快照 | TCP Recovery 补包、Market Recovery 水位衔接 | 快照与更新序号衔接 | 按区块补日志、校验分支并处理 reorg | 重订阅加查询对账 |
| 价格形成 | 按场所撮合规则匹配订单 | 按产品撮合算法匹配订单 | 内部撮合引擎匹配买卖订单 | 定价曲线与有效流动性决定兑换数量 | 节点撮合订单，成交结果纳入共识 |
| 完成点 | 订单接受、成交回报与清算交收分别跟踪 | 报单接受、成交、持仓与保证金结算分别跟踪 | 接受、成交、内部入账与提现到账分别跟踪 | 收录、执行成功与最终确认分别跟踪 | 受理、撮合与共识确认按协议区分 |
| 主要风险 | 断线后结果未知，盲目重发会重复下单 | 跨包事件未收齐就发布 | 盲目重发、重复记成交、托管与提现依赖 | 滑点回滚、MEV、gas、未收录与重组 | 请求身份、防重放与业务去重混淆 |

## 参考资料

1. [Nasdaq MoldUDP64 1.00 规范](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/moldudp64.pdf)：包头会话名、首序号、消息数与大端格式。
2. [Nasdaq SoupBinTCP 3.00 规范](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/soupbintcp.pdf)：长度帧、带序号流、登录续接。
3. [Nasdaq TotalView-ITCH 5.0 规范](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQTVITCHSpecification.pdf)：A/E/X/U/D/P 消息与订单字段。
4. [Nasdaq OUCH 5.0 规范](https://www.nasdaqtrader.com/content/technicalsupport/specifications/TradingProducts/OUCH5.0.pdf)：原生报单、client order token 与回报。
5. [Nasdaq GLIMPSE 5.0 规范](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQGlimpseSpecification.pdf)：快照末尾 G 消息与 ITCH 起始序号。
6. [FIX Protocol 标准](https://fixtrading.org/standards/fix-protocol/) 与 [FIX Session](https://fixtrading.org/standards/fixsession/)：tag-value 报单语义与会话管理。
7. [深交所 Binary 行情数据接口规范](https://www.szse.cn/marketServices/technicalservice/interface/P020250328368568358456.pdf)：300111 快照、300192 逐笔委托、300191 逐笔成交。
8. [深交所 Binary 交易数据接口规范](https://www.szse.cn/marketServices/technicalservice/interface/P020250328367803673293.pdf)：PartitionNo/ReportIndex 回报游标；[深交所 Binary 行情数据接口规范](https://www.szse.cn/marketServices/technicalservice/interface/P020250328368568358456.pdf)：消息头/尾、300192/300191 字段偏移、N13(4)/N15(2) 定标与 390094/390095 重传心跳；[上交所交易网关 STEP 接口规格说明书（债券平台）](https://www.sse.com.cn/services/tradingtech/data/c/10800115/files/d4ed0a7c66e34ac5af68709e10b29568.pdf)与[证券交易数据交换协议 JR/T 0022—2020](https://big5.sse.com.cn/site/cht/www.sse.com.cn/lawandrules/regulations/csrcannoun/c/10117169/files/62a9f84db1a14fe0a798d9cbea85a7e8.pdf)：FIXT.1.1 起始串、Logon 字段、序号重发与 35=D 报单；[上交所技术资料目录](https://www.sse.com.cn/services/tradingtech/home/)用于查其他平台 STEP 规范。
9. [CME iLink Functional Specification](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/714539039) 与 [Session Layer](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/714145834)：FIXP、UUID、Negotiate/Establish。
10. [CME Session Layer — Transferring](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/714604582)：双向序号、NotApplied 与业务重传。
11. [CME MDP 3.0 Incremental Refresh](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457227795)：New/Change/Delete 语义、RptSeq 与事件标志。
12. [CME TCP Recovery](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457574209) 与 [MBP/MBOFD Market Recovery](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457672425)：增量补包、循环快照水位衔接。
13. [CME Snapshot Full Recovery](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457736274)：快照根字段 LastMsgSeqNumProcessed 与重复组。
14. [上期技术《CTP 客户端开发指南》（期货公司转载 PDF）](https://www.eastmoneyfutures.com/software/9809db08-c8cb-4cc4-b631-8a16f0c2dfb9/0f757a06-10a5-48c2-817e-362b42202b51/f3ec4355-a448-45d7-bd51-3124d046092a/CTPcdg_ch.pdf) 与 [SimNow 官方 API 下载目录](https://www.simnow.com.cn/static/apiDownload.action)：认证、OrderRef/OrderSysID、私有流模式与回调模型，以期货公司当前接入版本为准。
15. [Uniswap v2 合约详解（ethereum.org）](https://ethereum.org/zh/developers/tutorials/uniswap-v2-annotated-code/)：Swap、Sync 事件与 amountOutMin、deadline。
16. [Uniswap v3 池子事件接口 IUniswapV3PoolEvents（GitHub）](https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/pool/IUniswapV3PoolEvents.sol)：Swap 事件的 sqrtPriceX96、liquidity、tick。
17. [Infura eth_subscribe 文档](https://docs.infura.io/reference/ethereum/json-rpc-methods/subscription-methods/eth_subscribe/)：logs 与 newPendingTransactions 订阅。
18. [Flashbots RPC 端点](https://docs.flashbots.net/flashbots-auction/advanced/rpc-endpoint) 与 [Flashbots Protect](https://docs.flashbots.net/flashbots-protect/quick-start)：私有交易不进公开 mempool。
19. [dYdX Chain Architecture Overview](https://docs.dydx.xyz/concepts/architecture/overview)、[Limit Order Book and Matching](https://docs.dydx.xyz/concepts/trading/limit-orderbook) 与 [OEGS](https://docs.dydx.xyz/concepts/architecture/oegs)：验证器内存订单簿、价格时间优先、gRPC 网关。
20. [dYdX Indexer WebSockets](https://docs.dydx.xyz/indexer-client/websockets/) 与 [Endpoints](https://docs.dydx.xyz/interaction/endpoints)：Initial/Update 通道与 clientId 对账。
21. [Hyperliquid Exchange Endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)：order/cancel action、nonce 与签名。
22. [Hyperliquid Nonces and API Wallets](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets)：每地址 nonce 窗口。
23. [Hyperliquid WebSocket Subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)：l2Book、orderUpdates、userFills。
24. [Binance Spot Diff Book Depth Streams](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams/diff-book-depth-streams)（[GitHub 版](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md)）：lastUpdateId、U/u 区间、绝对量覆盖与本地簿对齐步骤；[User Data Streams](https://developers.binance.com/docs/binance-spot-api-docs/user-data-stream)：executionReport 的 c/i/x/X/l/z/L/t 字段。
25. [Coinbase Markets Trading Rules](https://www.coinbase.com/legal/trading_rules) 与 [Exchange Matching Engine](https://docs.cdp.coinbase.com/exchange/concepts/matching-engine)：余额占用、订单撮合、账户余额结算和订单状态。
26. [Coinbase Exchange 充值流程](https://help.coinbase.com/en/exchange/crypto-transfers/depositing-to-coinbase-exchange)：链上转入平台地址与账户充值。
27. [How Uniswap Works](https://developers.uniswap.org/docs/get-started/concepts/how-uniswap-works) 与 [Understanding Swaps](https://developers.uniswap.org/docs/get-started/concepts/traders/swaps)：LP、资金池、费用、兑换执行与输入输出约束。
28. [ERC-20 Token Standard](https://eips.ethereum.org/EIPS/eip-20)、[Uniswap v2 Architecture](https://developers.uniswap.org/docs/protocols/v2/concepts/architecture) 与 [Implement a Swap](https://developers.uniswap.org/docs/protocols/v2/guides/swapping)：授权额度、代币转移、WETH、路由和兑换参数。
29. [Ethereum Transactions](https://ethereum.org/developers/docs/transactions/)、[Gas and Fees](https://ethereum.org/developers/docs/gas/) 与 [EIP-140](https://eips.ethereum.org/EIPS/eip-140)：交易生命周期、执行费用、回滚和最终确认。
30. [MetaMask：加速与取消待处理交易](https://support.metamask.io/manage-crypto/transactions/how-to-speed-up-or-cancel-a-pending-transaction/)：相同 nonce 的替换交易及其收录条件。
31. [Geth Real-time Events](https://geth.ethereum.org/docs/interacting-with-geth/rpc/pubsub) 与 [Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/#eth_getlogs)：订阅生命周期、历史日志查询及链重组通知。
32. [dYdX Chain Onboarding FAQs](https://docs.dydx.exchange/introduction-onboarding_faqs)：节点内存订单簿、区块提议与成交共识确认。
33. [Ethereum MEV](https://ethereum.org/developers/docs/mev/) 与 [Flashbots Protect Overview](https://docs.flashbots.net/flashbots-protect/overview)：交易排序、三明治交易和私有提交服务。
