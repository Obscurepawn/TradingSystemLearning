# 从 NexusTrader 看个人实盘交易系统的设计

我们的第一版可以只接一家交易所、一个账户和一种合约。比支持更多交易所更重要的是，断线或重启后，系统还能回答三个问题：策略想做什么，交易所已经做了什么，账户还有多少敞口。NexusTrader 最值得借鉴的是这三个问题之间的接口边界，最值得改进的是它在结果未知时如何保留状态。

本文对应 NexusTrader **v0.3.38，提交 `b5ed599`**。走读以 OKX 实盘执行路径为主，并对照了 Bybit 的增量盘口。本次运行的五组离线测试共 73 项通过，覆盖 WS 确认、幂等、重连和成交后校准。下文的延迟指标是我们要测的项目，没有用这些测试推算实盘性能。[固定版本源码](https://github.com/Quantweb3-com/NexusTrader/tree/b5ed599cfbb62b443a55fa37241cd2fab499a28a) · [测试用例](https://github.com/Quantweb3-com/NexusTrader/tree/b5ed599cfbb62b443a55fa37241cd2fab499a28a/test)

## 一笔订单经过哪些边界

NexusTrader 位于策略与交易所之间，撮合由交易所完成。一个引擎实例运行一个策略，可以连接多家交易所。策略从回调接收行情、提交交易命令，从共享缓存读取已知状态。以 OKX 为例，四条路径传递的是不同的数据。[引擎装配](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/engine.py#L49-L132)

~~~text
公共行情    交易所 → 公共连接器解码 → 总线 → 缓存、指标、策略收到 BookL2 等对象
主动下单    策略 → EMS 按账户排队 → 账户 OMS → 交易所收到委托
订单回报    交易 WS 应答／私有订单推送／REST 查单 → OMS → 更新订单缓存 → 策略收到 Order
账户更新    私有持仓与余额推送／REST 快照 → 更新持仓和余额缓存 → 策略读取
~~~

策略接口最容易被误用的地方是“返回了什么”与“完成了什么”。我们设计自己的接口时，可以直接写下这张契约表。

| 策略观察到什么 | NexusTrader 中能确认什么 | 我们的接口约定 |
|---|---|---|
| `create_order_ws()` 返回 `oid` | 命令已交给本地执行路径，可能还在队列里 | `submit` 返回本地受理或拒绝；交易所受理、成交分别发事件 |
| OKX 下单 WS 返回成功 ACK | 适配器取得交易所订单号，将此次应答映射为 `PENDING` | 客户端订单号与交易所订单号分开保存，允许私有回报先于 ACK 到达 |
| `on_filled_order(order)` 被调用 | 订单缓存已更新；独立推送的账户持仓可能仍是上一版 | 区分本次成交量、累计成交量和账户持仓。用唯一成交记录更新本地敞口，再和账户快照核对 |
| 调用撤单后，默认挂单查询少了一笔 | 查询默认隐藏标记了撤单意图的订单；它在交易所仍可能成交 | “还可以再发撤单的集合”与“仍承担敞口的集合”分别查询 |

[策略下单与撤单入口](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/strategy.py#L611-L711) · [OKX 请求应答](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/exchange/okx/oms.py#L245-L311) · [成交量与账户推送](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/exchange/okx/oms.py#L475-L566) · [挂单查询语义](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/core/cache.py#L536-L554)

**第一版保留策略、订单管理（OMS）和交易所适配器三个边界。** 用户策略表达意图，OMS 持有本地订单状态并决定哪些状态迁移有效，适配器处理报文、账户约束和单位换算。例如 OKX 线性合约发单时要用合约面值把策略数量换成张数，成交回报再换回；这些换算集中在边界上，策略与风控才会用同一种数量单位。[发单换算](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/exchange/okx/oms.py#L883-L895)

单交易所、单账户时，OMS 内部就能完成排队和路由。NexusTrader 单列执行管理（EMS），有一项好处是按交易所替换执行规则。它把 45 张已定价、定量的 OKX 委托分成 20、20、5 张，解决的是接口单批数量上限。“十分钟买够 1 BTC”则有自己的剩余数量、截止时间和活跃子单，即使当前一张挂单也没有，任务仍然存在。出现这类需求时，再引入独立的执行任务对象。该版本的 TWAP 策略入口与调度分支均已注释，不能把文档里的示例当成现成能力。[OKX 批量分组](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/exchange/okx/ems.py#L72-L91) · [自定义 EMS](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/engine.py#L321-L365) · [实际调度表](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/base/ems.py#L741-L758)

## 订单结果未知时，谁还在跟踪它

**把命令结果与原单状态分开保存。** 走读中最能说明问题的反例是“撤单被拒，原单随后成交”。我们把 `ACCEPTED → CANCEL_FAILED → FILLED` 依次送入这个版本的 OMS。普通更新进入 `CANCEL_FAILED` 后，框架将它当作闭单，移出挂单索引并注销 `oid`；随后的成交回报因为订单号已注销而被过滤。OKX 对部分撤单错误会先补查，这个反例针对进入上述分支的订单。我们的设计应将拒绝记在撤单请求上，原单继续接收成交，剩余挂单量继续占用风险额度。[OKX 撤单拒绝分支](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/exchange/okx/oms.py#L311-L336) · [闭单定义](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/schema.py#L357-L374) · [注册检查与注销](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/base/oms.py#L442-L497)

**明确未发出时可以发，已经发出却没等到 ACK 时先查。** OKX 路径在前一种情况下可沿用原 `oid` 转 REST 下单；后一种情况下等五秒后按原 `oid` 强制 REST 查单。查不到或查询报错，结果仍然未知。若策略把两种情况都处理成“换个订单号再试一次”，就有机会把一份意图成交两次。我们应让查询接口区分“已查到、场所明确报告不存在、查询失败”，并保留查询来源和时间。单次未查到能否排除原单，取决于场所的可见性规则。[OKX 超时处置](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/exchange/okx/oms.py#L924-L977) · [OKX 主动查询](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/exchange/okx/oms.py#L1347-L1390)

**重连与重启是两种恢复。** 连接中断后，本地还知道断线前跟踪了哪些单。OKX 据已知持仓与挂单选取品种、拉取远端状态，对消失的挂单逐笔复查；结果未明时保留开放状态。进程崩溃则会丢失内存里的幂等键映射与注册表。即便数据库能按稳定的 `oid` 查到已经落库的订单，默认 60 秒的周期同步仍留下“交易所已收单，本地还未落库”的窗口。[重连对账范围](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/exchange/okx/oms.py#L1392-L1438) · [缺单确认](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/base/oms.py#L147-L189) · [幂等映射](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/core/cache.py#L476-L491) · [同步周期](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/config.py#L326-L338)

我们的恢复起点是一条可持久重放的记录：发送前记下意图 ID、稳定客户端订单号、账户、品种与委托参数，同一意图换了参数就报冲突。重启先恢复未决清单，再核对交易所订单和成交。快照加速启动，成交标识用于重放去重，恢复期间由风控限制新增敞口。发送前持久化本身会增加延迟，这一段也要纳入发单路径测量。

## 从交易所报文到策略眼中的盘口

同一个 `BookL2` 策略接口，背后可以有完全不同的工作量。OKX 的 `books5` 推送已排序的五档快照，适配器直接组装两侧列表。Bybit L50 先发快照再发价位增量，适配器用买卖两张 `价格 → 数量` 哈希表维护本地簿，零数量删档，非零数量覆盖。每次输出时，它将两侧分别排序并构造新的档位列表。策略只拿到公开价位与汇总量，无法据此还原同价订单的先后顺序。[OKX 五档处理](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/exchange/okx/connector.py#L656-L672) · [Bybit 增量与排序](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/exchange/bybit/schema.py#L337-L385)

这给我们的选择定了顺序。五档快照先用紧凑数组；五十档增量用同一份真实行情比较两种实现。更新集中在簿顶时，买价升序、卖价降序的反向数组把两侧最优价都放在尾部，删最优档只需弹出末项。价格网格紧凑且 tick 固定时，用整数 tick 直接寻址，按价改量只写一个槽，配合非空位图寻找下一最优档。五十个活跃价位可以横跨几千个 tick。预估价格窗口之外的合法更新仍要保留，分块数组配有序块目录可以处理这种情况。比较时计入构造完整策略快照的成本。[本仓库的反向数组、价位网格与分块设计](../hft/HFT_Engineering_11_高性能订单簿和撮合引擎的常见实现.md)

**同一份行情交给多个组件时，谁可以改、可以留多久？** NexusTrader 的总线在发布线程上按顺序调用各订阅者，缓存、指标和策略拿到同一个对象引用。后续行情会替换缓存里指向的快照；但当前快照的买卖列表仍可修改，策略原地改列表也会改变其他订阅者所见。我们的适配器可以独占可写的工作簿，向策略发布只读视图；如果选择在下一次更新时复用内存，就把视图的有效期写进 API，让确需保留旧帧的策略显式复制。[总线分发](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/core/nexus_core.py#L216-L264) · [盘口字段](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/schema.py#L143-L153)

第一版可以采用单进程、订单簿和订单状态各有明确写入者的架构。把订阅、命令提交、本地读取和远端刷新设计成不同接口。NexusTrader 的同步 `fetch_order()` 会把协程投回交易循环并等待结果，从同一循环内的行情回调调用时会等住自己。我们让热路径上可见的订单和行情查询只读本地视图，远端刷新走异步接口；命令队列设定容量，队列满时给调用者明确结果。对允许只取最新值的快照可以合并，对依赖连续性的增量按频道协议检测缺口、停用旧簿并重建。[策略查询入口](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/strategy.py#L172-L192) · [同步等待实现](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/core/entity.py#L145-L166)

## 第一版如何验收

先用录制行情和模拟交易所跑通“收盘口 → 策略发限价单 → 部分成交 → 撤余量 → 对账”。验收看账户最后认了几笔成交、还有多少挂单风险，以及策略读到的盘口是否可信。以下是我们项目的用例。

| 注入的情况 | 通过条件 |
|---|---|
| 发单已到交易所，ACK 丢失，或成交先于 ACK 到达 | 原意图只产生一张订单；迟到的应答不会把已成交状态退回待确认 |
| 撤单被拒，原单接着成交；同一成交经 WS 和 REST 各到一次 | 撤单失败后仍跟踪原单，后续成交只计一次，成交量、剩余量和风险额度对得上 |
| 交易所已收单，本地尚未记录回报时强杀进程 | 重启后按原 ID 找到订单或保留未决，再对账恢复；全程无重复发单 |
| 删除最优档、价格越出预留网格、增量出现缺口 | 前两种情况下盘口与参考实现逐帧一致；检测到缺口后，策略在重新同步前不使用该簿交易 |
| 以独立于消费者的速率回放突发行情，在策略回调里注入延迟 | 记录收帧、解码、策略返回、命令出队、交给网络层各阶段的 p50/p99/p99.9，并同时记录积压、拒绝次数、订单回报延迟；过载没有静默丢失 |

计时点要先于优化落地。这个版本 Binance 适配器的同一个 `BookL1.timestamp` 字段，在现货填本地毫秒，在期货填交易所事件时间。我们的行情事件应分别保留可缺省的交易所事件时间、本地单调接收时间，以及频道提供的序号。本机各段耗时用同一单调时钟计算，“交给网络层到收到 ACK”单独报告。用同一负载回放两种盘口实现，就能判断尾延迟究竟花在改价位、生成快照、运行策略，还是等待命令出队。[两种时间戳来源](https://github.com/Quantweb3-com/NexusTrader/blob/b5ed599cfbb62b443a55fa37241cd2fab499a28a/nexustrader/exchange/binance/connector.py#L687-L716)
