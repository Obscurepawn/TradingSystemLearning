# 高性能订单簿与撮合引擎研究

面向低延迟 C++ 的数据结构、市场适配与验证方案

**建议以“每个分片单写者、连续价位数组、分层非空位图、可复用订单池”作为 A 股常规交易日的首个实现，同时保留分块数组或有序稀疏索引后端。** A 股 0.01 元最小价格变动单位与每日涨跌停价，能把价格域缩成一个可预分配区间。先用它与价位树做相同条件的对照测试。真正决定尾延迟的，还包括同价位撤单、最优档清空、跨档扫单、成交输出，以及确认前的复制和持久化。

最值得读的材料有不同用途：QuantCup 冠军代码展示受约束价格域的直接索引；SimpleOrderbook 提供 C++ 连续价位实现；exchange-core 展示池化订单与稀疏价格索引的组合；Jane Street JX 和 LMAX 提供生产系统的顺序、复制与线程通信经验。HRT 的 Bookbuilder 则适合研究行情重建。它们的工作量和测量边界不同，选型依据应是目标市场下的同一套命令、同一套成交结果和尾延迟曲线。

下文引用的性能均为来源方报告，未在同一硬件上复现；容量数字是按明确布局计算的算例。源码分析使用文中固定提交，设计方案和复杂度推导属于工程建议。

## 1　研究对象与性能边界

“OrderBook 很快”至少可能指四种工作。先确认系统负责哪一种，再选择基准。

| 对象 | 输入与职责 | 合理的性能单位 | 关键限制 |
|---|---|---|---|
| 行情订单簿重建 | 接收交易所已经产生的新增、撤销、成交、替换事件，维护本地盘口 | feed messages/s、每事件更新时间、BBO 查询时间 | 成交对手与结果由交易所决定 |
| 连续竞价撮合核心 | 接收新单、撤单、改单，决定成交对象、价格、数量和顺序 | commands/s、fills/s、每命令服务时间 | 一条主动单可产生大量成交 |
| 回测撮合器 | 根据历史行情模拟自己的订单能否成交 | 模拟事件吞吐、队列位置模型误差 | 公开行情未必包含完整隐藏量和自己的反事实市场影响 |
| 可运行交易场所 | 加上会话、风控、排序、交易阶段、复制、持久化、恢复、行情发布 | 指定负载下的确认延迟、可持续吞吐、恢复时间 | 确认语义和故障模型决定成本 |

Nasdaq ITCH 明确是出站行情协议，包含订单生命周期事件，也有非显示订单成交和集合成交消息；OUCH 则接收订单并返回状态。因此，用 ITCH 成交事件驱动本地减量，测到的是重建成本。重放这些事件时自行重新撮合，反而会破坏它们的原始语义。[Nasdaq ITCH 5.0](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQTVITCHSpecification.pdf)、[Nasdaq OUCH](https://nasdaqtrader.com/Trader.aspx?id=ouch)。

这一区别也关系到 A 股数据源：只有逐价汇总行情时，能维护的是 L2 数量；要复原单笔订单与排队顺序，还取决于逐笔委托、逐笔成交、序号及丢包补齐规则。选定重建基准时，将数据完整性与撮合语义分别验证。

## 2　交易公司分享与竞赛材料

| 材料 | 可确认的内容 | 对实现的价值 |
|---|---|---|
| Jane Street，Brian Nigito 的 JX 分享 | 自建 crossing engine；约 500k 消息/秒与个位数微秒描述的是组件能力 | 内存状态机、全序输入、确定性复制、持久性与客户可见结果的关系 |
| HRT 官方 Bookbuilder workshop | 真实加密货币交易所更新数据；C++17 骨架与 measure/check/profile | 用真实访问模式比较 ID 索引、BBO 维护和布局 |
| Optiver 工程师的 C++ 性能分享 | 从算法、分配、布局、开放寻址到硬件计数器的优化过程 | 建立可测量的优化顺序，而非猜测公司内部 OrderBook |
| 2011 年 QuantCup | 冠军公开了 C 撮合实现；另有 C++ 移植和评分程序留存 | 有界数组、连续内存、懒撤单如何利用比赛约束 |
| Optiver Ready Trader Go、IMC Prosperity | 参赛者在给定环境中提交交易策略 | 可借鉴模拟环境，比赛排名衡量策略表现 |
| LMAX Disruptor | 交易场所公开的线程间通信库和设计论文 | 单写者、预分配 ring、序号与消费者依赖 |

表中来源：[Jane Street JX](https://www.janestreet.com/tech-talks/building-an-exchange/)、[HRT 官方仓库](https://github.com/hudson-trading/wwhrt-bookbuilder-workshop)、[David Gross 讲义](https://david-grs.github.io/low_latency_presentation/)、[Optiver 官方技术文章](https://prod-www.optiver.com/insights/technology-blog/designing-low-latency-cpp-systems/)、[QuantCup 冠军主页](https://www.ermolinskiy.net/)、[Ready Trader Go](https://readytradergo.optiver.com/how-to-play/)、[IMC Prosperity](https://prosperity.imc.com/)、[LMAX 原始论文](https://lmax-exchange.github.io/disruptor/disruptor.html)。

**QuantCup 与 A 股数组方案最直接相关。** 冠军实现按价格访问 `pricePoints`，每档维护单向 FIFO，缓存 `askMin/bidMax`，订单放入静态连续 arena，递增订单 ID 直接索引节点。撤单只把数量置零，后续撮合跳过墓碑。作者注释说明，比赛数据的扫档多数不超过两档，最多五档；线性扫描的表现依赖这一分布。[冠军原始 engine.c](https://www.ermolinskiy.net/engine.c)。

这个实现的约束同样值得保留：arena 有固定总容量且不回收节点；大量墓碑会把成本留给以后扫单；成交价使用主动订单传入的价格，移植时要重写成目标市场语义。文件头为 `All Rights Reserved`，应视作公开可读的竞赛源码，复用前另行确认许可。冠军本人确认获胜，并在履历中将该奖项列于 Tower Research Capital。[作者主页](https://www.ermolinskiy.net/)、[作者履历](https://www.linkedin.com/in/andreyermolinskiy)。

留存的 C++ 版评分程序标注为非官方参考评分：每批十条操作、重复 200 次，评分为批耗时均值与标准差的平均值，成交回调为空。历史得分不能直接标成单笔纳秒延迟。[score.cpp](https://github.com/ajtulloch/quantcup-orderbook/blob/a2a015185a272e0e558ac2495cbcfdf2940fedb9/score.cpp)。

HRT 练习的原始实现则故意很朴素：vector 保存订单，按 ID 更新时线性搜索，BBO 查询扫描订单。测量还在每次更新后查询双边最优价并构造结果。它适合做行情端优化练习，其成本与撮合核心不同。[BookBuilder.cc](https://github.com/hudson-trading/wwhrt-bookbuilder-workshop/blob/master/BookBuilder.cc)、[Measuring.h](https://github.com/hudson-trading/wwhrt-bookbuilder-workshop/blob/master/Measuring.h)。

## 3　开源实现与源码差异

这里优先比较组织数据的方式。`Nₒ` 表示活跃订单数，`M` 表示非空价格层数，`Qₚ` 表示某价位订单数。

| 项目与核对提交 | 价格与订单组织 | 撤单、内存及并发特征 | 适合借鉴的部分 |
|---|---|---|---|
| Liquibook，C++，`2427613` | 买卖两侧 `std::multimap<ComparablePrice, Tracker>`，树内条目是订单 | 按价格定位后扫描同价位对象；嵌入式撮合库 | 订单条件、回调接口和功能基线 |
| SimpleOrderbook，C++，`3411ceb` | `vector<level>`；档内 `std::list`；ID 到链表迭代器的哈希索引 | 连续价位、动态订单节点；dispatcher/execution 线程和有锁入口 | bounded ladder、价位扩容与高级订单条件 |
| exchange-core，Java，`2f85487` | 价位 ART；订单 ID 也用 ART；侵入式订单链、bucket、best 缓存 | 订单与 bucket 池化；外围含风控、日志、快照、重放 | 稀疏索引、池化以及完整处理流水线 |
| brprojects/Limit-Order-Book，C++，`af6e534` | AVL 价位树、档内双链、订单与价位哈希 | `new/delete`；该版本递归重算子树高度 | 教学结构、细分操作成本；适合改成优化实验基线 |
| OpenBook/Serum 系，Rust，v1 `c85e56d` | slab 内 crit-bit tree；价格与时间组成排序键；整数节点句柄 | 预分配存储、空闲链；链上执行约束 | 稀疏有序索引与连续节点池的组合 |
| OrderBook-rs，Rust，`54df8eb` | `SkipMap` 价位、`DashMap` 订单定位、best 缓存 | 跨价位语义使用整簿读写 gate，另有价位分条锁 | 检查并发容器与命令原子性的区别 |

**Liquibook 的深队列撤单值得单独测。** 源码 `find_on_market()` 从对应价格开始逐单比较对象。因此查找成本可达 `O(log Nₒ + Qₚ)`；外层增加 ID→订单对象映射以后，库内部仍会扫描。A 股封板同价位聚集大量委托时，这个差异会直接放大。[容器定义](https://github.com/enewhuis/liquibook/blob/2427613b32f1667abae68a01df6af9ba8270f8e7/src/book/order_book.h#L59)、[查找实现](https://github.com/enewhuis/liquibook/blob/2427613b32f1667abae68a01df6af9ba8270f8e7/src/book/order_book.h#L642)。

SimpleOrderbook 的价位数组与 A 股思路吻合，但订单链表节点仍会分配，ID 索引是普通 `unordered_map`。其 README 的常数时间表述要按平均哈希成本理解；手动扩容涉及内部指针修正，AON 也有额外遍历成本。用于低延迟实现时，进一步池化订单并固定容量。[价位结构及成员](https://github.com/jeog/SimpleOrderbook/blob/3411cebb9756b80fd2cb3b442cfb109ca853068b/include/simpleorderbook.hpp#L506)、[架构与限制](https://github.com/jeog/SimpleOrderbook/blob/3411cebb9756b80fd2cb3b442cfb109ca853068b/README.md)。

exchange-core 的 `OrderBookDirectImpl` 对买卖价位和订单 ID 都使用自适应基数树 ART。订单内嵌 `next/prev/parent`，bucket 保存聚合量等信息，最优订单沿链直接推进。它说明价格索引、订单存储、遍历顺序可以分别优化：树负责定位，成交沿已建立的链推进。[Direct 实现](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/src/main/java/exchange/core2/core/orderbook/OrderBookDirectImpl.java#L43)、[插入删除逻辑](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/src/main/java/exchange/core2/core/orderbook/OrderBookDirectImpl.java#L446)。

brprojects 的 README 给出了 AVL 再平衡的耗时观察，但该提交 `getLimitHeight()` 会递归遍历左右子树，计算平衡因子时重新求高度。这项成本包含递归高度计算，引用其数值时须与经典缓存高度的 AVL 更新区分。[高度计算](https://github.com/brprojects/Limit-Order-Book/blob/af6e5349874649fe196bd6c26653d357f5a751f2/Limit_Order_Book/Book.cpp#L288)、[平衡因子](https://github.com/brprojects/Limit-Order-Book/blob/af6e5349874649fe196bd6c26653d357f5a751f2/Limit_Order_Book/Book.cpp#L1028)。

OpenBook v1 将价格放在 128 位 key 的高位，低位编码顺序，使 min/max 查询同时表达价格与时间优先；节点在 slab 中，以整数句柄连接。它的连续存储是节点池，不是为每个 tick 分配价位。C++ 可以借鉴这种句柄布局，而链上计算预算和账户模型不适合参加普通服务器的纳秒排名。[crit-bit slab](https://github.com/openbook-dex/program/blob/c85e56deeaead43abbc33b7301058838b9c5136d/dex/src/critbit.rs#L254)、[排序键生成](https://github.com/openbook-dex/program/blob/c85e56deeaead43abbc33b7301058838b9c5136d/dex/src/state.rs#L908)。

OrderBook-rs 的核对版本保留 `std::sync::RwLock` 的整簿 gate，FOK、STP 等路径会取得独占访问，价位创建和删除还有分条锁。并发跳表和哈希表解决的是局部存取；一次扫多个价位的撮合还要保证整体语义。[gate 与价位锁说明](https://github.com/joaquinbejar/OrderBook-rs/blob/54df8ebcd99b4b3fb7373cf848b76e89da1b1361/src/orderbook/book.rs#L426)。

## 4　公开性能数字怎样使用

| 来源 | 作者公布的数字 | 实验或测量边界 | 可用于什么判断 |
|---|---|---|---|
| Liquibook | 约 206 万 inserts/s，含五档深度；约 249 万，仅 book | 2.4 GHz i7 笔记本；预先构造订单；循环调用 add，价格区间重叠会成交 | 老牌 C++ 库的特定插单基线；缺少混合撤单压力 |
| brprojects | 平均约 713 ns；作者据此折算约 140 万请求/s | i5-12450H；500 万条合成请求，含限价、市场与止损类操作 | 分析成交数量和树更新对该实现的影响 |
| exchange-core | 5M ops/s 时 p50 1.5 μs、p99 42 μs、p99.99 170 μs | X5690，隔离一个 socket；单品种；风控＋撮合；不含网络、IPC、journal | 观察负载逼近容量时尾延迟放大 |
| Jane Street JX | 组件约 500k messages/s，个位数 μs | 讲座给出的组件能力 | 复制架构的可行性，不是端到端 SLA |
| LMAX 生产技术页 | 内部撮合延迟 <50 μs；峰值 800k orders/s，持续 100k | 交易场所自报口径，公开页未给完整分位曲线 | 生产系统测量范围与纯容器基准的差别 |

出处：[Liquibook 性能表](https://github.com/enewhuis/liquibook/blob/2427613b32f1667abae68a01df6af9ba8270f8e7/PERFORMANCE.md)与[测试代码](https://github.com/enewhuis/liquibook/blob/2427613b32f1667abae68a01df6af9ba8270f8e7/test/perf/pt_order_book.cpp)、[brprojects 实验](https://github.com/brprojects/Limit-Order-Book/blob/af6e5349874649fe196bd6c26653d357f5a751f2/README.md)、[exchange-core 基准说明](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/README.md)、[JX 分享](https://www.janestreet.com/tech-talks/building-an-exchange/)、[LMAX 技术页](https://www.lmax.com/exchange/technology/)。

exchange-core 的 5M 实验里，82% 命令是 move，9% GTC、3% IOC、6% cancel；约 6% 命令触发交易，平均约 1,000 个活跃订单和 750 个价位。均匀到达、小幅 BBO 变化，与 A 股涨停队列、集合竞价和撤单风暴差别很大。同页约 150 ns 的“大单中单次 matching”数字，也不是一条完整主动单的确认延迟。[实验负载与延迟分布](https://github.com/exchange-core/exchange-core/blob/2f8548749839e9095c8dc597e4b61521d259fa5d/README.md)。

2026 年公开的 FlashOne 比较框架提供统一事件流、适配器、结果校验和补丁记录，适合借用其对照方法。它由参与比较的实现方发布，工作负载是围绕单一美股构造的合成流；QuantCup 适配版修改了成交价等语义，exchange-core 比较的是直接 order book 路径。排名解释要连同适配器和补丁阅读。[方法说明](https://github.com/flash1-dev/matching-engine-benchmark/blob/main/docs/METHODOLOGY.md)、[补丁清单](https://github.com/flash1-dev/matching-engine-benchmark/blob/main/docs/PATCHES.md)。

该方法文档还把 0.005 美元 tick 与旧合规日期联系起来；SEC 在 2026 年 6 月已将相关豁免延至 2027 年 11 月首个工作日。作为合成测试的半美分网格仍可使用，但它不表示适用股票在当时已经切换到半美分 tick。[SEC 正式豁免令 34-105656](https://www.sec.gov/files/rules/exorders/2026/34-105656.pdf)。

## 5　A 股规则对存储边界的约束

下面按 **2026 年 7 月 6 日生效的沪深规则**及北交所规则讨论。回放历史日期时，要切回当时有效的规则版本，尤其是主板风险警示股票的限制变化。

| 品种 | 股票价格 tick | 常规日涨跌幅限制 | 常见例外 |
|---|---:|---:|---|
| 沪深主板 A 股 | 0.01 元 | ±10%；当前主板 ST/*ST 也为 ±10% | IPO 前五个交易日；重新上市首日、退市整理首日等 |
| 科创板 | 0.01 元 | ±20% | IPO 前五日、退市整理首日等 |
| 创业板 | 0.01 元 | ±20% | IPO 前五日、重新上市首日、退市整理首日等 |
| 北交所股票 | 0.01 元 | ±30% | 公开发行上市首日、退市整理首日等 |

规则出处：[上交所交易规则及正式附件](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)，重点 3.3、3.5、6.6、6.8；[深交所交易规则 PDF](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)，重点 3.3、3.4、4.4；[北交所交易规则](https://www.bse.cn/jygl_list/200028217.html)，重点 3.3.10—3.3.12。例外列表用于说明后端选择，完整接入仍以品种当日状态为准。沪深基金的 tick 可为 0.001 元，股票与基金分别配置价格精度。

对实行日涨跌幅限制的品种，数组边界取**当日权威参考数据给出的跌停价、涨停价**；订单是否可接受，再检查价格笼子与交易阶段规则。自行从前收盘价乘百分比计算时，要遵守取整、最低一 tick、上下界与基准价距离不足一 tick 的处理；除权除息日还涉及参考价。用十进制定点或整数有理运算，避免二进制浮点在边界处改变合法性。[沪市规则 3.3.17](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)、[深市规则 3.3.19、4.4.3](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)。

**价格笼子不能替代整日价域。** 沪主板和深市连续竞价阶段的限价申报，对买入使用基准价 102% 与高十个 tick 的较高者，对卖出使用 98% 与低十个 tick 的较低者。科创板 6.8 采用买入基准价 102%、卖出基准价 98%，条文不含上述十个 tick 扩展。它们限制新申报的单侧价格，基准还随盘口变化，既不是固定 ±2% 内存窗口，也不能据此删除已经存在的远端挂单。[沪市 3.3.14、6.8](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)、[深市 3.3.16](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)。

集合竞价要求独立的定价与分配路径。可以先计算买价 ≥p 的累计买量 `B(p)`、卖价 ≤p 的累计卖量 `S(p)` 和可成交量 `V(p)=min(B(p),S(p))`，再按交易所条件筛选价格；最大成交量只是第一步。沪市在规则所列条件下比较未成交量，仍并列取中间价并按 tick 规则取整；深市比较规定的买卖累计量差，仍并列时，开盘参考前收盘价，盘中及收盘参考最近成交价。期间买卖队列可交叉，甚至同价同时留存；两侧队头、数量和优先级仍须分别维护。[沪市 3.5.2](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)、[深市 3.4.3](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)。

完整 A 股系统还涉及盘中临停、禁撤阶段、订单类型与数量约束、盘后固定价格等模式。2026 年沪深规则扩大了盘后固定价格交易适用品种，连续竞价 FIFO 核心只覆盖其中一部分。[上交所修订说明](https://star.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20260424_10816474.shtml)。

## 6　连续价位数组与内存布局

设当日静态跌停价、涨停价已经转换成整数 tick，分别为 `L` 和 `U`，价格 `p` 的下标与价位数为：

```text
i = p - L
N = U - L + 1
合法条件：L <= p <= U，且原始价格满足 tick 对齐
```

先在有符号域验证上下界，再转换成数组下标。`vector` 的价值是连续存储；盘中固定容量时，`unique_ptr<Level[]>`、预分配 arena 或已定长的 vector 都可实现相同布局。只调用 `reserve()` 不会创建可按下标访问的元素；使用 vector 时还要建立相应大小。

按每侧每档 32 B、买卖两侧独立统计计算，价格层内存为 `64N` B；双侧基础位图另占 `2 × ceil(N/64) × 8` B。

| 参考价格与涨跌幅 | 价格端点 | 每侧价位数 | 双侧 Level 内存 |
|---|---:|---:|---:|
| 10 元，±10% | 9—11 元 | 201 | 12.56 KiB |
| 100 元，±10% | 90—110 元 | 2,001 | 125.06 KiB |
| 1,000 元，±10% | 900—1,100 元 | 20,001 | 1.221 MiB |
| 100 元，±20% | 80—120 元 | 4,001 | 250.06 KiB |
| 100 元，±30% | 70—130 元 | 6,001 | 375.06 KiB |

这是依据公开市场规则提出的布局方案。表中也只算价格层：100 万个、每个 64 B 的订单节点就另占约 61 MiB，还要加 ID 索引、输出和日志缓冲。5000 个品种若都按 100 元、±10% 预分配，两侧价格层合计约 611 MiB；该预算还要对照处理核的缓存容量与活跃工作集。

**非空档位图负责找下一个价格。** 每侧用一位表示该档是否存在订单；每 64 位形成一个机器字，上层再用一位表示下层字是否非零，直到根层。卖侧找最小置位，买侧找最大置位。新增首单和删除末单时同步更新摘要。固定容量下层数很少；一般表达为 `O(log₆₄ N)`，比跨越长空区间的 `O(N)` 扫描更可控。

最优价缓存在常见路径直接使用。新订单建立更优价格层时直接更新 best，原最优档清空后才通过位图寻找下一档；普通订单数量变化无需更新整条摘要。实现时处理空簿、末尾未用 bit、位移 64 和零值位扫描。C++20 的 `std::countr_zero(0)` 有定义，但返回位宽，仍不能把结果当作有效下标；部分编译器 builtin 对零输入没有同样保证。

![连续价位数组、非空位图、外部订单 ID 索引与池内双向 FIFO](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/orderbook-matching-engine/dense-ladder-order-pool.png)

档内 FIFO 建议用**订单池内的索引双向链**。知道订单 handle 后，撤销中间订单只修改前后节点；涨停价的大队列不会引入按队列长度扫描。节点池虽然连续，链的逻辑顺序仍可能离散，因此接近同一品种分配、紧凑字段和热点布局依然重要。双向链多占一个索引，却避免懒撤单把大量工作集中到后续主动单。

容量策略放在开盘前完成：按每个品种的 `N`、历史活跃价位数、峰值订单数、全分片内存预算选择后端并预触页。无静态日界限、高价细 tick 或长期稀疏品种，采用分块价位数组或 ART/B-tree；盘中遇到容量边界时走预先定义的扩容、拒绝或暂停接单路径，避免在一次撮合中临时搬迁整本簿。

## 7　数据结构与跨市场选择

价格索引、订单节点池和外部 ID 索引是三个独立选择。`flat_hash_map` 很适合 ID 查找，但本身不提供最优价与相邻价；连续节点池也可以服务于树。

| 价格索引 | 定位与结构更新 | 找下一非空价 | 内存与适用条件 |
|---|---|---|---|
| dense array＋裸扫描 | 价位定位 O(1) | 最坏 O(N) | 价格域小且密；作为基础对照 |
| dense array＋分层 bitmap | 定位 O(1)；首末单变更涉及摘要层 | O(log₆₄N) | A 股固定日价格区间的优先候选 |
| paged array＋有序块目录 | 页内直接定位；新页涉及目录和分配 | 由目录及页内位图决定 | 大价格域、局部密集；用预分配页控制新页延迟 |
| 红黑树/缓存高度 AVL | 新价位增删 O(log M)；已有价位可用稳定 handle | 缓存 best；后继操作成本依实现 | 稀疏、通用；节点布局与分配是主要成本 |
| B-tree | O(log M)，每节点更多键 | 节点内紧凑遍历 | 稀疏且重视缓存；注意元素移动与句柄稳定性 |
| ART/radix/crit-bit | 由 key 宽度、前缀分布与路径决定 | 有序导航或另维护链 | 大整数价格域、预分配节点；实现复杂度更高 |
| 排序 vector/flat map | 查找 O(log M)，插删移动 O(M) | 邻接访问便宜 | 活跃档极少或批处理；新增档频繁时测尾延迟 |

表中树的复杂度描述正确维护元数据的典型实现；具体项目还可能引入递归求高度等额外成本。Abseil B-tree 官方说明了更紧凑节点的缓存收益，也明确其插删会移动值、使指针和迭代器失效。因此订单可以保存稳定的 `LevelId`，树只保存“价格→LevelId”，不要让订单指向可能搬迁的树内对象。[Abseil B-tree 设计](https://abseil.io/about/design/btree)。ID 哈希可用开放寻址实现，但仍要预留容量、监控负载与删除后的探测链；“期望 O(1)”不包含任意时刻 rehash 的保证。[Swiss Tables 设计](https://abseil.io/about/design/swisstables)。

| 市场特性 | 对实现的影响 | 一手来源 |
|---|---|---|
| A 股固定 tick 与常规日涨跌停 | 每日设置 base/upper，直接下标；IPO 等日期切换后端 | [上交所规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml) |
| 港股 tick 随价格段变化，价差表也会更新 | 分段 tick ordinal，或统一最小金额单位＋合法性表；跨段更新要保持排序 | [HKEX 最小价差调整](https://www.hkex.com.hk/Services/Trading/Securities/Overview/Trading-Mechanism/Reduction-of-Minimum-Spreads?sc_lang=en) |
| 美股价格网格与市场状态按规则变化 | tick 与适用日期配置化，价格保护单独建模 | [SEC 规则延期](https://www.sec.gov/newsroom/speeches-statements/atkins-statement-minimum-pricing-increments-access-fee-caps-061126) |
| CME 不同产品采用 FIFO、pro-rata、LMM 等分配方式 | 价格索引与档内分配解耦；比例分配增加同档遍历、舍入与剩余量分配 | [支持的分配算法](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457218479/Supported+Matching+Algorithms)、[分配步骤](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457218521/CME+Globex+Matching+Algorithm+Steps) |
| 部分期货可出现零或负价格 | 通用价格类型用 signed；市价单用独立类型标记 | [CME 负价格支持通知](https://www.cmegroup.com/notices/clearing/2020/04/Chadv20-160.html) |
| 加密资产按产品设置报价和数量精度，连续交易 | 读产品 metadata；宽价格域优先稀疏或分页结构；长期运行要求节点回收 | [Coinbase 产品字段](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-single-product) |
| Coinbase 正式规则含 Price-Display-Time 与 STP | 显示/隐藏量的优先级和自成交防护改变普通 FIFO 扫描 | [交易规则 1.7](https://www.coinbase.com/legal/trading_rules)、[撮合与 STP](https://docs.cdp.coinbase.com/exchange/concepts/matching-engine) |

可以把这些差异收敛为三类策略接口：价格合法性与网格映射、价格索引后端、档内成交分配。交易阶段是另外一层状态机，负责决定当前能接哪些命令、能否撤单、何时执行拍卖。若策略在交易日内固定，可在创建 book 时选定具体类型，让编译器优化热路径；无需对每个订单做长串市场名称判断。

## 8　低延迟 C++ 的处理架构

首版选择每个 shard 一个固定 CPU 上的写线程，shard 内管理一组品种。网关解析报文、做会话校验，排序点生成可回放的命令序号；撮合线程按该序号修改簿并产生事件。行情和查询消费事件或快照，避免直接并发读写可变订单链。

![每个分片单写者的撮合、日志复制与提交发布流程](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/orderbook-matching-engine/matching-pipeline.png)

跨线程通信采用预分配的有界队列。单生产者/单消费者通道可使用 SPSC；多个网关进入同一 shard 时，由明确的合并与排序规则决定顺序。轮询顺序、公平性和拥塞行为都是产品语义的一部分，时间戳相同的命令仍按唯一接收序号排列。LMAX 用序号表达线程间依赖，为这种流水线提供了参考。[LMAX Disruptor 用户指南](https://lmax-exchange.github.io/disruptor/user-guide/)。

单写者先消除簿内锁、CAS 重试和复杂的跨档原子性。扩展吞吐时优先跨品种分片，再按流量把热点品种分给独立核。一个热点品种仍受其顺序处理能力约束；想并行处理同一簿，必须先定义哪些操作可交换、哪个点形成对外全序，以及 FOK、STP、批量撤单如何串行化。

**确认时刻决定故障后的承诺。** 可以让输入日志复制与内存执行并行，再用提交水位限制对外 ACK/成交发布：只有执行结果和所要求的持久化或复制条件同时满足，才越过发布闸门。副本“收到内存”与“持久落盘”的故障保证不同。若 ACK 允许先于日志，就明确它只代表哪一步完成。[JX 对可靠性与复制的讨论](https://www.janestreet.com/tech-talks/building-an-exchange/)。

恢复记录至少关联命令序号、规则版本、状态快照、事件 ID 与发布水位。重启重放同一规则，产生同一成交序列；传输可以重发，下游按稳定事件 ID 去重。日切、停复牌、临时规则变更作为有序输入处理，避免按机器墙钟或实时全局配置改变历史重放结果。主备切换使用任期/epoch 与 fencing，撤销旧主接单和发布权限；新主从已提交前缀恢复。

按品种分片还留下跨账户风险问题：同一账户同时向多个 shard 下单，如果各自读余额后批准，会重复使用额度。可由账户服务先预留并签发唯一 reservation token，或向 shard 分配可独立消费的预算；成交、部分撤单、拒单和费用分别定义额度转换。涉及跨品种原子撮合的组合单，需要另行协调。JX 讲者也明确指出跨产品操作会破坏简单的按品种独立拆分。[JX 关于跨产品操作](https://www.janestreet.com/tech-talks/building-an-exchange/)。

## 9　节点结构与命令处理建议

下面的 C++20 草案用于审查字段与内存布局。真实字段宽度按最大数量、价格域和会话规模确定；涉及金额乘积时检查溢出，必要时使用更宽的中间类型。

```cpp
using Tick = std::int64_t;
using Qty = std::uint64_t;
using Index = std::uint32_t;
constexpr Index nil = UINT32_MAX;

struct Handle {
    Index slot;
    std::uint32_t generation;
};

struct Level {                 // 每侧一个 Level，目标布局 32 B
    Index head = nil;
    Index tail = nil;
    Qty total_qty = 0;
    std::uint32_t order_count = 0;
    std::uint32_t flags = 0;
    std::uint64_t reserved = 0;
};

struct OrderHot {              // 目标布局 64 B；外部身份另存冷数据
    std::uint64_t internal_id;
    std::uint64_t priority_seq;
    Tick price;
    Qty leaves_qty;
    Index prev, next;
    std::uint32_t generation;
    std::uint32_t level_index;
    std::uint32_t instrument_id;
    std::uint32_t owner_ref;
    std::uint16_t flags;
    std::uint8_t side, state;
    std::uint32_t reserved;
};
static_assert(sizeof(Level) == 32);
static_assert(sizeof(OrderHot) == 64);
```

32 位索引与 `nil` 哨兵限定了池容量；配置时校验槽位、价位下标以及 generation/epoch 的表示范围。

`internal_id` 与外部客户订单号分开。ID 索引键包含实际唯一域，例如交易日/会话/账户和客户 ID；值是 `{slot,generation}`。节点复用后 generation 变化，使旧 handle 失效；溢出与日切还要有 epoch 策略。重复请求的幂等判断在协议层处理，与节点生命周期校验共同工作。

Nasdaq OUCH 5.0 要求 UserRefNum 在相应域内唯一且严格递增；严格递增允许缺口，各端口也有各自的唯一域。只有协议与容量边界都满足时才直接数组索引；否则使用预分配哈希或分页映射。[OUCH 5.0 数据类型](https://www.nasdaqtrader.com/content/technicalsupport/specifications/TradingProducts/OUCH5.0.pdf)。

新限价单的常见路径是：检查阶段、数量、价格和风险额度；读取对侧 best；按价格条件逐单成交；输出确定顺序的成交事件；删除耗尽订单并维护档位统计；剩余量按 TIF 入队或取消。修改数量是否保留优先级、改价是否重新排队由市场规则决定。对于简单价格时间优先的连续竞价，成交价格由对应被动订单和市场规则确定，不直接取主动订单限价。

撤单路径是：外部 ID 定位有效 handle，验证身份和状态，摘链、扣减聚合量、更新末档位图、移除 ID 映射并回收槽位。**已知 handle 的摘链是 O(1)**；哈希查找、最后一单导致的 best 重定位和输出是另外的工作。跨多个订单成交，至少有 `Ω(k)` 的订单级处理或输出成本，`k` 为实际生成的成交数量。

FOK 若被目标市场支持，预检与执行使用同一组价格保护、STP、隐藏量和分配条件。单写者可在一次命令内完成两遍遍历，中间不插入其他命令；复杂规则也可生成有界执行计划再提交。检查输出容量及风险预留，避免成交到一半因资源耗尽退出。把已成交部分“补偿回滚”会改变外部可见事实。

热路径工程优先级是：预分配和回收正确、减少指针跳转、控制对象尺寸、固定线程与 NUMA 内存归属、隔离跨核共享计数器，之后才比较 LTO、PGO、分支提示和预取。单写者拥有的每个订单无需都按缓存行对齐，盲目 padding 会扩大工作集；生产者和消费者频繁写的独立计数器则值得避免 false sharing。CPU 隔离、busy polling、大页和网络旁路的收益，分别测量后再纳入部署配置。

## 10　统一基准与正确性验证

建议先实现清晰的参考撮合器，例如 `map<price, FIFO>` 加 ID 索引，再让优化实现处理同一命令序列。参考实现主要作为语义判定器，不参与“最佳性能”宣传。比较完整结果：接收与拒绝、订单终态、成交对手、价格、数量、先后顺序和剩余盘口；只比较总成交量会漏掉优先级错误。

| 测试场景 | 特意制造的结构压力 | 记录的指标 |
|---|---|---|
| A 股常规日、多价格与板块 | 不同 N、活跃档密度及多品种工作集 | 内存、缓存失效、每类命令分位延迟 |
| 涨停/跌停深队列 | 同价位大量订单，随机撤中部，持续补单 | 撤单随 Qₚ 的变化、池与 ID 索引成本 |
| 稀疏价格带 | 删除 best 后下个非空档很远 | 裸扫描与 bitmap/paged 的最坏延迟 |
| 大额主动扫单 | 每命令产出 1、10、100、1000 笔成交 | 按 k 分桶的服务时间、事件输出吞吐 |
| 价格层创建与销毁 | 反复新增首单、删除末单 | 树更新、位图摘要、新页和分配开销 |
| 撤单风暴与全天长跑 | 高回收率、重复 ID、延迟撤单、墓碑积累 | p99.9、最大值、探测链、内存增长 |
| 集合竞价与状态切换 | 交叉盘口、并列价格、禁撤、临停、日切 | 定价/分配正确性、转换原子性 |
| 无涨跌幅限制日与高价细 tick | 大跨度、稀疏、突发价位迁移 | 后端选择、容量边界和新页尾延迟 |
| 输出拥塞与故障恢复 | 慢消费者、日志延迟、进程崩溃、主备切换 | 队列水位、确认水位、恢复后一致性 |

把测量分成三个层次：一是纯核心从开始执行到最后事件生成的服务时间；二是含入口排队与出口消费的流水线延迟；三是带网络和指定提交保证的确认延迟。各层同时报告 commands/s 与 fills/s、p50/p90/p99/p99.9/p99.99、最大值、样本数及持续时长。拒单率和丢弃数一起展示，避免通过丢请求得到更好数字。

负载生成采用开放式到达计划，分别测试日常速率、峰值、突发与接近饱和的区域。若压测器等上一个请求完成才发送下一个，系统变慢时输入也跟着减少，会漏测排队延迟，即 coordinated omission。时间应从计划到达或真实入口时刻算起；HdrHistogram 可记录分布，但采用直方图本身不会自动修正错误的发压方式。[Gil Tene 的测量说明](https://github.com/giltene/wrk2)、[HdrHistogram 修正接口](https://github.com/HdrHistogram/HdrHistogram/blob/master/src/main/java/org/HdrHistogram/AbstractHistogram.java)。这里借鉴的是测量原则，HTTP 压测器并不适合直接测纳秒撮合。

记录 CPU 型号、频率与电源策略、核绑定、SMT、NUMA、内存、编译器版本及参数、输入种子、项目提交、池容量、活跃订单与价位分布。预热与冷启动分开；吞吐批量测量和逐命令采样分开；用可消费的事件输出或校验摘要防止编译器消去工作。高分位报告保留多个独立轮次，确保样本量能支持所展示的尾部。

持续校验的状态不变量包括：每个活跃订单只在一个正确方向和价位队列；ID 索引与池对象一一对应；队列总量等于价位总量；位图等价于非空状态；FIFO 序号符合当前优先级规则；已终结订单无法再次成交；已成交、已撤和剩余数量守恒。`best_bid < best_ask` 只适用于处理完成后的普通连续竞价状态，集合竞价不使用这个不变量。随机序列差分、边界用例、内存检查和崩溃重放各覆盖不同错误。

## 11　实施顺序与选型落点

| 阶段 | 具体交付 | 进入下一阶段的依据 |
|---|---|---|
| 语义基线 | 整数价格、版本化 instrument metadata、连续竞价参考器、确定性事件格式 | 小规模穷举与随机差分结果一致；明确支持的订单类型和阶段 |
| A 股性能核心 | dense ladder、双侧 bitmap、可复用索引链、ID 映射、容量预算 | 封板深队列、跨空档、首末单变更均正确；按操作分桶测量 |
| 通用后端 | paged 或 ART/B-tree；相同命令与事件接口 | 无界日、高价细 tick、稀疏与多品种负载下的内存和尾延迟对比 |
| 市场阶段 | 沪深各自的集合定价、临停/禁撤、日切和目标范围内的盘后模式 | 官方规则例子与自行构造的并列价格反例全部一致 |
| 可恢复服务 | 有序日志、快照、复制水位、发布闸门、额度预留 | 在各处理阶段注入崩溃，恢复后确认过的状态和事件满足承诺 |
| 部署优化 | 核与 NUMA 配置、编译优化、必要的网络旁路 | 改动在目标负载的端到端分位曲线上带来稳定收益 |

源码阅读顺序建议为：**SimpleOrderbook 的连续价位结构 → QuantCup 的极限约束优化 → exchange-core 的 DirectOrder 与池化 → Jane Street JX 的确认和恢复设计**。Liquibook 用于接口与语义对照；brprojects 用作容易理解、可替换布局与高度维护的 C++ 实验基线；OpenBook 用于学习 slab 与整数句柄；OrderBook-rs 用于审查跨价位并发边界。

如果第一期目标是 A 股低延迟 C++，先把 dense＋bitmap 与“按价位建树＋相同订单池”做公平 A/B：两者使用同一 ID 映射、同一节点布局、同一成交输出和同一规则实现。只有价格索引变化时，测得的差异才足以回答“数组是否值得”。最终按**符合正确性与恢复要求时的 p99.9 延迟、可持续负载和全市场内存预算**选定后端。
