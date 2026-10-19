# HFT Engineering 08：交易所协议——一笔买单怎么变成订单，盘口又怎么在本地重建

策略在屏幕上算出"现在买 1 手"，接下来要回答两个问题：这个意图怎么变成交易所认可的订单，我又怎么知道盘口现在长什么样。前者走的是账户私有的交易通道，后者走的是公开的行情通道，两条通道独立建立、独立断线、独立恢复，混在一起处理是很多事故的起点。

这笔买单发出去之后会发生什么，取决于你接的是哪一类市场。传统证券交易所和期货交易所把撮合放在中心化的撮合引擎里，客户通过专线或网关用二进制协议报单；链上 AMM 没有撮合引擎，也没有传统意义的挂单，交易是一笔签名兑换，价格由池子余额当场算出；订单簿型 DEX 把撮合搬到自己的链上，报单方式又回到了 API 加签名。这一篇按这三类市场分别讲清楚两件事：买卖订单怎么下（报单、撤单改单、执行回报、会话与断线恢复），行情怎么订阅（快照、增量、传输方式、丢包恢复、本地订单簿怎么建），最后再放在一起对比。

## 1. 先分清两条流，以及几个通用概念

不管接哪个市场，本地都要同时维护两套状态。一套是自己的订单台账：每个订单请求发出去没有、交易所接没接受、成交了多少、撤单确认没有，持仓和现金只从已经核实的成交派生。另一套是市场订单簿：公开行情描述的是全市场的买卖盘，它不包含你自己的订单状态。公开行情可以刷新很多次，却回答不了"我那笔买单成交没有"，这个问题只能靠私有回报。

往下读之前，先把几个反复出现的词说清楚。

会话层位于 TCP 连接之上、业务消息之下，负责建立连接时的身份协商、心跳保活、序号同步和断线后续接，相当于在一条不可靠的网络上开出一条可恢复的逻辑会话。序号是同一个方向、同一条流里逐条递增的编号，接收方靠它发现丢了哪几条、哪些是重复到达。ClOrdID 是客户自己给每个订单请求分配的唯一编号，回报里会原样带回来，断线重连后对账主要靠它，而不是靠"我大概几分钟前发过一笔"。快照是某一时刻的完整状态，增量是此后一条条修改；恢复时先拿到快照，再把快照位置之后的增量接上。二进制编码指字段按固定字节宽度、固定大小端排列，省带宽但必须按协议模板解码。多播是一次发送、一组订阅者同时收到的 UDP 传输，交易所高频行情常用，但 UDP 本身不重传，丢包要靠应用层补。

## 2. 传统证券交易所：Nasdaq 原生协议与国内证券网关

### 2.1 报单与回报：OUCH 跑在 SoupBinTCP 上

具备直连资格的客户，在 Nasdaq 用 OUCH 协议报单，承载它的是 SoupBinTCP。先看承载层。SoupBinTCP 在 TCP 字节流上给每条消息加一个两字节大端长度前缀，一次 recv 可能只收到半条消息，也可能收到好几条，解析器要先攒够长度再取载荷。它把消息分成两个方向：服务器发给客户的是带序号的 Sequenced Data Packet，双方按收到的包计数；客户发给服务器的是 Unsequenced Data Packet，不计入这条服务器序列。

OUCH 的新单请求里带客户自己生成的 client order token（作用等同于 ClOrdID）、股数、限价、买卖方向和有效期类型。服务器回 Accepted 表示订单被接受，执行过程中回 Executed，撤单回 Canceled，替代改单回 Replaced。撤单请求也是带上原来的 client order token，不需要知道交易所内部的订单号。订单被替代后旧 token 失效、新 token 生效，本地台账要跟着换。

[![客户发新单并收到 Accepted 后网络断开；重新登录时带上会话名和期望序号，服务器从该序号续推，补到 Executed，客户端不重发原单](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/nasdaq-ouch-session.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/nasdaq-ouch-session.png)

这张图里最关键的是最后一步。连接断在 Accepted 和 Executed 之间时，本地只知道"请求结果未知"，不能假设没成交就再发一笔，也不能假设已成交就直接推持仓。Soup 的带序号流可以从期望位置续推回报，续不上的部分再靠查询核对。

### 2.2 行情：ITCH over MoldUDP64，用 GLIMPSE 完成快照衔接

公开行情走另一条路。Nasdaq TotalView-ITCH 是逐笔行情协议，经 MoldUDP64 封装成 UDP 数据报发出。MoldUDP64 包头依次是十字节 ASCII 会话名、八字节首条消息序号、两字节本包消息数，整数全部大端；一个数据报里可以装多条 ITCH 消息，序号连续排下去。UDP 不保证送达也不保证顺序，接收方发现序号缺口时，要么走开通的重传服务补包，要么放弃连续历史、重新取快照。

ITCH 按事件描述显示订单的进出。新增显示订单是 A，成交是 E，带价格的成交是 X，替代改单是 U（带旧订单号、新订单号、新数量和新价格），删除整笔剩余是 D，非显示订单的成交走 P。因为消息以单笔订单为单位，本地可以先按订单引用号保存剩余量，再聚合出价位总量，这是逐笔订单簿（MBO）的做法；只按价位汇总数量则是按价位订单簿（MBP）。

启动时本地没有任何历史，直接从第一条增量接会缺前面的全部挂单。Nasdaq 的做法是用 GLIMPSE：另开一条 SoupBinTCP 连接接收现存显示订单的快照，快照流末尾的 G 消息给出实时 ITCH 的起始序号 N，含义是从实时流的第 N 条开始处理（含第 N 条）。本地一边缓存实时 ITCH，一边收 GLIMPSE 快照，收到 G 后按 N 把缓存增量接上，再开始消费实时流。快照和增量的交接位置必须由这个序号钉住，不能用时间戳去猜"大概从哪条开始对得上"。

### 2.3 FIX 与国内证券网关

不是所有客户都用交易所原生协议。FIX（Financial Information eXchange）是一套跨场所通用的报单语言，经典 tag-value 编码里 `35=D` 表示新订单单笔请求，`11=ClOrdID` 是客户订单号，`54` 是方向，`40` 是订单类型，字段之间用 SOH 分隔。FIX 同时管业务语义和会话管理，券商网关、跨境接入大量使用它。原生二进制协议（OUCH、ITCH）性能更好，但只在特定场所内成立。

国内证券市场同理。上交所的 STEP 沿用 FIX 风格的字段与业务规则，深交所 Binary 用固定宽度的二进制字段，两者都是机构通过交易网关接入，普通客户则落在券商提供的客户端产品里。以深交所 Binary 为例，现货集中竞价快照是 300111，逐笔委托是 300192，逐笔成交是 300191，整数大端排列；逐笔委托和逐笔成交在同一个频道内共用连续的 ApplSeqNum，成交消息通过买方、卖方委托的记录号引用对应委托。交易侧的私有回报按平台分区保存 PartitionNo/ReportIndex，每个分区内记录号连续递增，重连后按分区逐条请求尚未处理的下一条，这个业务游标独立于 TCP 重建和心跳。要注意周期快照和逐笔流没有天然的交接游标，没开通明确衔接机制时，不要把一份十档快照和某个时间点之后的逐笔消息直接拼接。

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

## 4. Web3：链上 AMM 与订单簿型 DEX

### 4.1 AMM：交易是一笔签名兑换，价格由池子状态算出

Uniswap 这类自动做市商（AMM）没有撮合引擎，没有挂单价位表，也没有传统的新单/撤单回报。你要做的不是挂一个限价单等别人来吃，而是签一笔兑换交易，通过 JSON-RPC 的 eth_sendRawTransaction 广播到节点的待处理交易池（mempool，节点内存里等待打包的签名交易集合），等某个区块把它收进去执行。合约按池子当前余额当场定价：v2 是恒定乘积 x·y=k，v3 把流动性集中在价格区间内，按 tick 分段。

[![计算滑点下限并签名后，交易经公开 mempool 或私有 relay 进入区块；输出低于下限则 revert，否则 Swap 与 Sync 事件上链，本地经 eth_subscribe 更新池子状态](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/amm-swap-flow.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/amm-swap-flow.png)

兑换请求里带 amountOutMin（v2 路由）或 amountOutMinimum（v3），这是你能接受的最少输出，也就是滑点保护：实际输出低于下限，交易直接 revert。还有 deadline 期限，过期同样失效。gas 决定打包优先级，链不保证你的交易一定被打包，更不保证几秒内成交，这和交易所"报上去就排队"的模型根本不同。

还要接受一个事实：链上交易发出去撤不回来。没有 Cancel 这个动作，也就没有撤单回报；等不到打包时，能做的要么提高 gas 另发一笔替换交易，要么接受它不被收录，已经 revert 的交易也不会留下挂单。

### 4.2 链上行情：事件订阅代替盘口推送

AMM 没有盘口可推，行情来自合约事件日志。v2 池子每次兑换发 Swap 事件（amount0In、amount1In、amount0Out、amount1Out），每次余额变动发 Sync(reserve0, reserve1)，储备量就是最新汇率的来源；v3 池子的 Swap 事件带 amount0、amount1、sqrtPriceX96（兑换后价格平方根的 Q64.96 定点数）、liquidity 和 tick（以 1.0001 为底的价格对数）。接入程序通过 WebSocket 上的 eth_subscribe 订阅 logs，按池子地址和事件主题过滤，自己维护池子状态、自己算价格。想观察待打包交易，可以订阅 newPendingTransactions 拿到进入 mempool 的交易哈希，再按需取交易内容。

本地"订单簿"在这里退化为池子余额或 tick 上的流动性分布，它没有逐档买卖排队，也就没有交易所那种序号缺口和增量补发；对应的一致性来源是区块本身，一笔交易上链就是确定性结果，链重组（reorg）时旧日志会被带 removed 标记重发，本地要能回滚。

### 4.3 MEV、滑点、gas 与私有交易通道

公开 mempool 里交易内容对所有人可见，套利机器人可以看到你的大额兑换，在它前后插单（三明治攻击），把你被迫接受的劣化价格变成它的利润。应对方式之一是走私有交易通道，例如 Flashbots 的 eth_sendPrivateTransaction，交易不进公开 mempool、直送区块构建者，在随后若干区块内尝试打包，失败也不在公开队列里暴露。私有通道解决的是被围观和被插单，不保证打包，也不替代上链确认的等待；滑点下限和 gas 预算的权衡仍要自己做。这种"交易在公开排队队列里被围观"的风险，传统交易所不存在。

### 4.4 订单簿型 DEX：dYdX v4 与 Hyperliquid

另一类 DEX 把订单簿和撮合搬上自己的链，报单方式又回到了 API 加签名，但语义和中心化交易所仍有差别。

dYdX v4 运行在自己的 dYdX Chain 上，底层是 CosmosSDK 加 CometBFT 共识，验证器在内存里维护订单簿，按价格时间优先撮合，proposer 约每秒一个区块。客户端把签名订单通过公开的 gRPC 端点（OEGS 网关，单跳送达 proposer 集合）提交，撤单同样是签名请求，每笔订单带一个客户端自己生成的 clientId，之后用它和回报对账。行情和账户数据由 indexer 通过 REST 和 WebSocket 提供，Orders 通道先推 Initial 快照再推 Update，每条消息带 message_id。

Hyperliquid 运行在自有 L1 上，交易请求统一 POST 到 /exchange，body 里是 action（type=order 或 cancel）、nonce 和签名。nonce 在这里推荐取当前毫秒时间戳，每地址保留最近 100 个 nonce，新 nonce 必须比集合里最小的大且未用过；order 动作里 a 是资产号、b 是是否买入、p 是价格、s 是数量、c 是客户订单号 cloid，cancel 动作按 oid 撤单。行情走 wss://api.hyperliquid.xyz/ws，订阅 l2Book 收到的是整簿 levels 快照式推送（不是逐档增量叠加），私有回报走 orderUpdates、userFills 通道。

[![客户端提交带 nonce 和签名的 order 动作并收到同步响应，再通过 WebSocket 订阅 l2Book、orderUpdates、userFills，收到整簿快照和私有推送](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/dex-order-flow.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-08/dex-order-flow.png)

这类链上订单簿 DEX 的断线恢复没有交易所原生协议那套可续传业务序号：同步响应只表示请求被接受，最终状态要靠私有推送和查询核对，nonce 用过即失效，重发必须换新 nonce。

### 4.5 对照：中心化交易所的 WebSocket 深度流

作为对照，中心化交易所（以 Binance 现货为例）用 WebSocket 推公开深度、用 REST 取快照。初始化必须先启动订阅再请求快照，否则快照形成之后、本地开始接收之前的增量会丢失。REST 快照带 lastUpdateId，随后的 diff depth 消息用 U、u 标出更新区间，里面每个价位给的是新的绝对数量（不是累加），数量为 0 表示删除该档；连续性被打断（U 越过本地游标）就冻结该簿、重新取快照衔接。私有执行报告里 c 是客户订单号、i 是场所订单号、l 是本次成交量、z 是累计成交量，本次只按 l 入账，z 用于核对，重连查询和推送里重复出现的同一笔成交只记一次。这一节只作对照，不展开。

## 5. 三类市场横向对比

把前面的细节压成一张表，差异会比较直观。

| 维度 | 传统证券（Nasdaq/国内网关） | 期货（CME/CTP） | 链上 AMM | 订单簿型 DEX |
| --- | --- | --- | --- | --- |
| 报单通道 | OUCH over SoupBinTCP / FIX / STEP、Binary | iLink 3（FIXP+SBE over TCP）/ CTP 柜台 API | 签名交易广播进 mempool | 签名 POST 到网关 gRPC/HTTP |
| 订单身份 | client token / ClOrdID | UUID + 业务序号 / CTP OrderRef↔OrderSysID | 交易哈希，无挂单概念 | clientId / cloid + nonce |
| 私有回报 | Soup 带序号续推 / 网关分区游标 | iLink 业务重传、NotApplied / CTP 私有流三模式 | 上链交易回执与事件 | 私有 WebSocket 推送加查询 |
| 行情形态 | ITCH 逐笔增量 + GLIMPSE 快照 | MDP 多播增量 A/B + 循环快照 | Swap/Sync 事件日志 | 整簿快照推送 / Initial+Update |
| 丢包恢复 | 重传服务或重建快照 | TCP Recovery 补包、Market Recovery 水位衔接 | 区块确定性，reorg 回滚 | 重订阅加查询对账 |
| 价格形成 | 撮合引擎价格时间优先 | 撮合引擎价格时间优先 | 池子余额公式即时定价 | 链上内存订单簿撮合 |
| 主要风险 | 断线后结果未知，盲目重发会重复下单 | 跨包事件未收齐就发布 | 滑点 revert、MEV、gas、不打包 | nonce 失效、状态靠推送加查询 |

最容易踩的坑，是把"请求发出去了"当成"订单有结果了"。TCP 把字节送到、服务器回了 Accepted，都只说明请求被受理，成交与否要等执行回报。2.1 里断在 Accepted 和 Executed 之间的那笔单就是典型：本地既不能假设没成交再发一笔，也不能假设已成交直接推持仓，只能把状态标成"请求结果未知"，等 Soup 带序号流续推回报，续不上再靠查询核对。iLink 遇到客户序号跳跃回 NotApplied、CTP 把等待确认的报单标成 Unknown、Hyperliquid 的 nonce 用过即失效，处理的都是同一件事：回报补齐之前，本地不替交易所下结论。

另一个容易被忽略的是，序号和事件各有各的管辖范围，不能互相替代。MDP 3.0 里一个撮合事件可以跨多条消息、跨多个包，MatchEventIndicator 的结束位没来之前，本地改的只是候选状态，提前把中间簿发给策略，就会让它看到一个短暂而错误的价位。多播包序号管频道传输，RptSeq 管单个标的更新，ClOrdID 或 clientId 管业务意图对账，三套序号各管一段；快照和增量的交接也要靠双方认得出的位置序号钉住，靠时间戳或"看起来差不多"拼接，迟早把簿建错。至于本地能重建什么，取决于价格怎么形成：撮合型市场能重建逐笔排队，AMM 只有池子状态，链重组还要能回滚，选型时先想清楚这一层。

验收时不必先写完整客户端，构造几条报文直接注入就能暴露最常见的错法：把 diff depth 的绝对量覆盖错当成累加、同一笔成交在重连查询和私有推送里二次入账、CME 跨包事件没收齐就通知策略、私有流重放和实时交错时旧状态覆盖新状态。每一种错法对应一条注入输入，先把这些边界跑通，再谈完整连接。

## 参考资料

1. [Nasdaq MoldUDP64 1.00 规范](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/moldudp64.pdf)：包头会话名、首序号、消息数与大端格式。
2. [Nasdaq SoupBinTCP 3.00 规范](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/soupbintcp.pdf)：长度帧、带序号流、登录续接。
3. [Nasdaq TotalView-ITCH 5.0 规范](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQTVITCHSpecification.pdf)：A/E/X/U/D/P 消息与订单字段。
4. [Nasdaq OUCH 5.0 规范](https://www.nasdaqtrader.com/content/technicalsupport/specifications/TradingProducts/OUCH5.0.pdf)：原生报单、client order token 与回报。
5. [Nasdaq GLIMPSE 5.0 规范](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQGlimpseSpecification.pdf)：快照末尾 G 消息与 ITCH 起始序号。
6. [FIX Protocol 标准](https://fixtrading.org/standards/fix-protocol/) 与 [FIX Session](https://fixtrading.org/standards/fixsession/)：tag-value 报单语义与会话管理。
7. [深交所 Binary 行情数据接口规范](https://www.szse.cn/marketServices/technicalservice/interface/P020250328368568358456.pdf)：300111 快照、300192 逐笔委托、300191 逐笔成交。
8. [深交所 Binary 交易数据接口规范](https://www.szse.cn/marketServices/technicalservice/interface/P020250328367803673293.pdf)：PartitionNo/ReportIndex 回报游标；[上交所技术资料目录](https://www.sse.com.cn/services/tradingtech/home/)用于查 STEP 规范。
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
24. [Binance Spot WebSocket Streams](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md)：lastUpdateId、U/u 区间与绝对量覆盖。
