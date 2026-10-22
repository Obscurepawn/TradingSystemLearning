# 经典高频交易策略（4）做市（二）：逆向选择、成交质量、公允价与队列

[上一篇](HFT_Strategy_03_做市一_库存与报价.md)的 A-S/GLFT 模型只回答了“库存多了报价怎么偏”。实盘做市还有另一类更隐蔽的亏损：你一买，价格就跌；你一卖，价格就涨。这篇讲清楚这件事的原因（逆向选择），怎么逐笔度量它（markout），以及做市算法另外三个关键问题：用什么价格当公允价、队列位置值多少钱、撤单和抢单谁先到。

## 1. 为什么总在坏价格成交：逆向选择

你有没有发现，自己一买价格就往下走，一卖价格就往上走？A-S/GLFT 只补偿库存风险，解释不了这件事。真正的原因是**对手方比你知道得多**。

举个直白的例子：你挂着 100.00 的买单。某个交易者刚刚看到一条利空消息（公开但你还没反应过来），或者他从别的市场看到价格已经跌了，他会毫不犹豫地把东西卖给你。等你成交后，价格跌到 99.95，你刚买的东西立刻亏了。这类“专挑你报价过时的时候来成交”的现象叫**逆向选择**（adverse selection）。

### 1.1 经典模型怎么解释价差

**Glosten–Milgrom 模型。** 假设市场上有两类交易者：知道真实价值的知情交易者，和因为各种日常原因（调仓、赎回）随机交易的流动性交易者。做市商分不清谁是谁。模型证明：即使做市商风险中性、期望利润为零，只要存在知情交易者，买卖价差依然为正——**价差是做市商在知情交易者身上亏的钱，由流动性交易者的成交赚回来** [[1]][ref1]。

做市商的报价满足：

$$A_t\ge \mathbb{E}[V\mid \text{买入}]\ge B_t$$

- $V$：资产的真实价值（做市商不知道）；
- $\mathbb{E}[V\mid \text{买入}]$：“已知下一笔是一个买单”的条件下，对真实价值的期望（导读 2.3）。有人来买，说明真实价值偏高的可能性变大了，所以这个条件期望比无条件的估计更高；
- $A_t$、$B_t$：卖价和买价。

读法：卖价至少要高到“即使知道对方是来买的，我也不吃亏”；买价同理。均衡中每笔交易期望利润为零，而交易本身把信息带入了价格 [[1]][ref1]。

**Kyle 模型。** Kyle 把信息进入价格的过程建模成知情交易者、噪声交易者与做市商之间的博弈：知情者分批交易以隐藏信息，做市商根据总订单流 $x$ 定价：

$$p=\mu+\lambda x$$

- $\mu$：做市商原先对价值的估计；
- $x$：观察到的净订单流（买多于卖为正）；
- $\lambda$：价格冲击系数，每单位净买入把价格推高多少。

$\lambda$ 越大，同样的订单流推动价格越多，市场越“浅”；知情者的利润随市场深度与噪声交易量变化 [[2]][ref2]。

### 1.2 实用的扩展

- **Cartea–Jaimungal 一类扩展**：把“期末一次性估值”换成持续的库存惩罚 $\phi\sigma^2\int q_s^2 ds$。读法（导读 2.8）：在整个持仓期间，库存的平方乘以方差不断累加，持仓越大、拿得越久、惩罚越大，$\phi$ 控制惩罚力度。模型还可以加入方向信号 $\alpha$ 调整报价。论文证明，对“中间价有没有漂移”感到不确定并厌恶这种不确定，在数学上等价于施加这种持续库存惩罚；库存越大，越贴近中间价挂单以加速回归 [[3]][ref3][[4]][ref4]。
- **VPIN（流量毒性）**：Easley、López de Prado、O'Hara 把成交按固定成交量切成一个个“桶”（volume bucket），统计每个桶里买卖成交量的失衡程度，取最近若干桶的平均作为“毒性”。毒性高说明单边的知情交易多，做市商应加宽价差或撤回报价；VPIN 在成交量时钟上更新，不需要估计不可观测的模型参数 [[5]][ref5]。

这一节是概念框架，VPIN 与惩罚项的参数标定属于实证研究，本文不展开。

### 1.3 落到工程上

- 成交方向失衡、大单冲击、订单流不平衡（OFI，[第 10 篇](HFT_Strategy_10_订单流信号.md)）作为毒性与方向信号的输入；
- 惩罚系数 $\phi$ 控制库存回归速度，信号 $\alpha$ 让报价在库存偏移之外再叠加方向倾斜 [[4]][ref4]；
- VPIN 水平作为“是否暂停做市、加宽价差”的开关量 [[5]][ref5]；
- 毒性上升时的动作是**加宽、远离或撤单，而不是反向追单**；这需要快速的成交方向分类与桶内累计；
- 信号 $\alpha$ 与库存偏移叠加时，要避免把方向性观点伪装成做市导致裸头寸。

### 1.4 什么时候会出问题

- VPIN 与成交量、波动率机械相关，学界对其预测力存在争议（Andersen–Bondarenko 的批评），只能作辅助开关，不能单独定价；
- 把 alpha 信号写进报价后，策略从“中性做市”滑向方向交易，业绩归因必须分开；
- 模型只使用公开订单流信息。内幕交易、利用未公开重大信息属于违法。

## 2. 成交质量评估：markout

双边报价到底赚不赚钱，不能只看成交流水或价差毛额，要逐笔问两件事：这笔被动成交在那一刻拿到了多少价格优势？成交之后，价格是往有利还是不利方向走？后者正是逆向选择的逐笔度量，把上一节的“毒性”从开关量落到每一笔成交上。

### 2.1 公式

令 $z=+1$ 表示买入、$z=-1$ 表示卖出，$t_f$ 为成交时刻，$p_f$ 为成交价，$m_{t_f^-}$ 为成交前一刻的中间价，$m_{t_f+h}$ 为成交后经过时间 $h$ 的中间价。**标记收益**（markout）：

$$M_h=z\left(m_{t_f+h}-p_f\right)
=\underbrace{z\left(m_{t_f^-}-p_f\right)}_{\text{成交前价格优势}}
+\underbrace{z\left(m_{t_f+h}-m_{t_f^-}\right)}_{\text{持仓方向的后续价格变化}}$$

怎么读：

- 左边 $M_h$：如果在成交 $h$ 时间后按中间价把这笔仓位“估个值”，赚了还是亏了；
- 乘以 $z$ 是为了统一方向：买入时价格上涨算赚，卖出时价格下跌算赚；
- 右边第一项：成交价比当时中间价好多少。做市商买在买一价，通常比中间价便宜半个价差，这一项为正；
- 右边第二项：成交之后，价格沿着你持仓的方向走了多少。如果你总是在价格要跌前买到，这一项就系统性为负，这就是逆向选择的代价。

两项相加正好等于左边（中间那个 $m_{t_f^-}$ 一加一减抵消了）。中间价标记尚未扣除实际退出所付的价差与费用。

一笔报价的价值可粗略写成：成交概率 ×（$M_h$ + 返佣 − 手续费 − 退出成本）− 新增库存的风险代价。若成交后的不利价格变化与其他成本更大，交易仍然亏损。

### 2.2 算一个例子

```text
示例：以 100.00 买入 1 单位；成交前一刻中间价 m_{t_f^-}=100.01；
成交后 h 时中间价 m_{t_f+h}=99.96。

第 1 步：成交前价格优势 = 1 × (100.01 − 100.00) = +0.01
第 2 步：持仓方向后续价格变化 = 1 × (99.96 − 100.01) = −0.05
第 3 步：标记收益 M_h = 1 × (99.96 − 100.00) = −0.04
校验：+0.01 + (−0.05) = −0.04
```

最初拿到 0.01 的价格优势，被随后 −0.05 的不利变化抵消，净标记 −0.04；这笔“看起来成交在买一”的单子事后是亏的。卖出方向对称：卖在更好价位得 +0.01 优势，随后价格上行则给出 −0.05 的毒性代价。

![markout 时间线](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/fig10_markout_timeline.png)

*图 1：成交价相对成交前中间价的价格优势，与成交后沿持仓方向的后续价格变化，两段相加即 markout。*

### 2.3 怎么用

- **评价跨度 $h$**：内部常用几秒到几分钟，可以对多个 $h$ 分别统计；
- **分层聚合**：按报价距离、队列位置、品种、时段、毒性水平分组求平均。负 markout 集中在某一组，说明这组报价被毒性成交主导；
- 返佣、手续费、退出价差与滑点单独计入，不混进 $M_h$；
- 数据上要保留每笔成交前一刻的中间价快照和之后 $h$ 时刻的中间价，回测、实盘归因都要按笔对齐；交易时段边界、集合竞价、断档需特殊处理；
- 分层结果接回上一节的毒性开关，形成“哪档报价该加宽、远离”的反馈。

注意：$h$ 太短噪声大，太长会混入后续对冲与仓位管理的影响，需固定口径再比较策略；中间价标记不含退出成本，不能直接当作净盈亏。做市控制与“成交条件下的后续价格”分析见 Cartea–Jaimungal–Penalva 专著 [[3]][ref3]。

## 3. 公允价估多少

实际做市算法在“风险价差 + 库存偏移”之外，还要决定用什么价格当报价的中心。

**报价偏移回顾。** 偏移（skew）把保留价从中间价平移，等价于按库存降低买入意愿、增强卖出意愿（上一篇公式）；工程上也常用归一化库存 $q/\bar q$ 线性缩放买卖深度 [[6]][ref6]。达到 `max_position` 停止单边报价；超限时主动用相关资产（标的期货、对冲篮子）反向对冲，把单边裸露的时间压到最短 [[7]][ref7]。

### 3.1 加权中间价

简单中间价只看价格不看挂单量。可是如果买一挂了 900 股、卖一只挂了 100 股，卖一很快会被吃光，价格更可能往上走。最简单的修正是**加权中间价**：

$$p_{\mathrm{wm}}=\frac{P^a Q^b+P^b Q^a}{Q^b+Q^a}$$

- $P^a$、$P^b$：卖一价、买一价；
- $Q^a$、$Q^b$：卖一、买一的挂单量。

注意权重是“交叉”的：卖价 $P^a$ 乘的是买方挂单量 $Q^b$。买盘越厚，公允价越靠近卖价。

```text
示例：买一 100.00/900 股，卖一 100.01/100 股。
p_wm = (100.01×900 + 100.00×100)/(900+100) = 100.009，
明显偏向卖方压力小的一侧（买盘厚，公允价靠上）。
```

### 3.2 Microprice

Stoikov（2018）的 **microprice** 更进一步：它不是静态的量加权公式，而是把公允价写成“中间价 $M$ + 修正项 $g(I,S)$”，其中 $I$ 是顶部失衡（买一量占买一卖一总量的比例）、$S$ 是价差；修正项通过统计“在当前 $I$、$S$ 下，未来中间价平均会往哪走”递归估计出来。实证上它对近期价格的预测优于中间价与加权中间价 [[8]][ref8]；BBO（买一卖一）级别的 microprice 对大 tick 品种有效、对小 tick 品种作用有限 [[6]][ref6]。

## 4. 队列位置值多少钱

价格—时间优先下，同一价位排在前面的订单和排在后面的订单命运不同：

- 排在前面：价格碰到这一档就成交，而且多半是“正常”的成交；
- 排在后面：只有前面的人全被吃光才轮到你，而能把一整档吃光的往往是大单、知情单，你成交之后价格很可能继续穿过这一档——这正是逆向选择最严重的时候。

Moallemi–Yuan 把队列位置的价值拆成两部分：静态项（赚价差 vs 承担逆向选择）和动态项（占住这个位置，等于持有一个“以后可能用得上”的期权），实证中大 tick 股票的队列价值可与整个买卖价差同量级 [[9]][ref9]。Dahlström、Hagströmer、Nordén 用 OMXS30 的逐笔订单数据实证：自己前面的挂单量、后面的挂单量、对手方深度与库存共同决定撤单概率，说明队列位置是撤单决策的输入，而不是可以忽略的常数 [[10]][ref10]。

## 5. 撤单和抢单谁先到

信息跳变的那一刻，撤单和抢单在赛跑。假设一条公开消息让所有人都认为你挂的卖价太便宜了：

- 你作为做市商要发撤单；
- 另外 $N-1$ 个快速交易者同时发“按你的旧价成交”的请求。

交易所的撮合引擎**按到达顺序逐条处理消息**。如果大家速度相当、到达顺序近似随机，你的撤单排在最前面的概率只有 $1/N$，旧报价被扫中的概率为 $(N-1)/N$。这类抢单利润诱发速度竞赛（一种囚徒困境：谁都不提速就谁都不吃亏，但每个人都有动机单独提速），做市商只好把被扫的成本计入报价，使价差变宽。论文据此提出频繁批量拍卖作为市场设计的回应 [[11]][ref11]。

![队列位置、撤单与抢单的竞争时序](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/fig05_queue_position.png)

*图 2：同一信息跳变出发，撤单请求与抢单成交请求串行进入撮合引擎，先到者决定旧报价退出还是被扫。*

这场竞赛在[第 7 篇](HFT_Strategy_07_延迟套利_领先滞后与新闻驱动.md)会从抢单者的角度再讲一次。

## 6. 工程上要盯的信号与做法

- **信号**：顶部失衡 $I=Q^b/(Q^b+Q^a)$、价差 $S$、自己前后的排队量、自身队列位置（逐笔订单数据 MBO 能定位自己的订单），外加库存归一化水平、对冲腿的 $\beta$/乘数、毒性开关；
- **队列位置估计**依赖逐笔订单数据或自身回报推断；常用“不跨价差”的订单类型（如 GTX，若会立即成交就拒绝）避免被动单意外变成主动单 [[12]][ref12]；
- **撤单链路越短越好**；作为抢单方时，没抢到的订单用 IOC（立即成交否则撤销）属性自动撤回 [[11]][ref11]；
- **对冲腿独立成交**，可能只成一边，留下临时敞口。

## 7. 什么时候会亏

- 把静态加权中间价误当 microprice，会高估预测力；microprice 是基于未来价格统计的修正估计，不是量价加权公式 [[8]][ref8]；
- 队列价值模型假设先到先得；部分期货市场按成交量比例分配（不按排队先后），位置逻辑不同；
- 只靠提速不改善模型。速度竞赛是市场设计带来的内生均衡 [[11]][ref11]，个体无法单独消除。

## 小结

- 逆向选择：对手比你知道得多，价差本质上是对这部分亏损的补偿。
- markout = 成交前价格优势 + 成交后沿持仓方向的价格变化，是逐笔衡量逆向选择的标准尺子。
- 公允价可以从中间价 → 加权中间价 → microprice 逐级精细；队列靠前更值钱；撤单要赢过抢单。

---

上一篇：[做市（一）库存与报价](HFT_Strategy_03_做市一_库存与报价.md) ｜ 下一篇：[期权做市](HFT_Strategy_05_期权做市.md)

## 参考资料

1. L. Glosten, P. Milgrom. [*Bid, ask and transaction prices in a specialist market with heterogeneously informed traders（JFE）*][ref1]，1985。论文。风险中性、零利润做市商仍有价差，逆向选择是价差的纯信息来源。
2. A. S. Kyle. [*Continuous Auctions and Insider Trading（Econometrica 53(6)）*][ref2]，1985。论文。知情者—噪声交易者—做市商线性均衡，市场深度参数λ。
3. Á. Cartea, S. Jaimungal, J. Penalva. [*Algorithmic and High-Frequency Trading（Cambridge University Press）*][ref3]，2015。专著。做市、最优执行、逆向选择的随机控制教材。
4. Á. Cartea, R. Donnelly, S. Jaimungal. [*Algorithmic Trading with Model Uncertainty*][ref4]。对中间价漂移的模糊厌恶等价于运行库存惩罚φσ²∫q²ds，库存越大越贴近中间价挂单。
5. D. Easley, M. López de Prado, M. O'Hara. [*Flow Toxicity and Liquidity in a High-Frequency World（RFS 25(5)）*][ref5]，2012。论文。VPIN以体积桶买卖失衡度量流量毒性，预示毒性引致波动。
6. hftbacktest 文档. [*High-Frequency Grid Trading - Simplified from GLFT*][ref6]。非参数强度替代指数形式、大tick品种引入microprice、归一化库存skew。
7. Optiver. [*Market Making explainer*][ref7]。做市商按合同/法定义务在规定时段报最小量、最大价差，理论价加减风险信用。
8. S. Stoikov. [*The Micro-Price: A High Frequency Estimator of Future Prices（Quantitative Finance 18(12)）*][ref8]，2018。论文。P^micro=M+g(I,S)鞅修正，实证优于中间价与加权中间价。
9. C. C. Moallemi, K. Yuan. [*A Model for Queue Position Valuation in a Limit Order Book（Columbia GSB工作论文）*][ref9]，2016。队列位置价值拆为静态（价差vs逆向选择）与动态（位置期权），大tick股票可与价差同量级。
10. P. Dahlström, B. Hagströmer, L. Nordén. [*Determinants of Limit Order Cancellations*][ref10]，2018。队前量、队后量、对手深度与库存决定撤单概率（OMXS30/HFT样本）。
11. E. Budish, P. Cramton, J. Shim. [*The High-Frequency Trading Arms Race: Frequent Batch Auctions as a Market Design Response（QJE 130(4):1547–1621）*][ref11]，2015。论文（QJE官方页）。旧报价sniping、(N−1)/N扫中概率、Spread Networks光纤16→13ms、微波8.1ms、ES-SPY机会时长与盈利统计、速度竞赛囚徒困境。
12. hftbacktest 文档. [*Guéant–Lehalle–Fernandez-Tapia Market Making Model and Grid Trading*][ref12]。式4.6/4.7、c1/c2、λ=Ae^{−kδ}对数回归校准、max_position硬限。

[ref1]: https://web.stanford.edu/~milgrom/publishedarticles/Bid%20Ask%20and%20Transaction%20Prices.pdf
[ref2]: https://www.utdallas.edu/~nina.baranchuk/Fin7310/papers/Kyle1985.pdf
[ref3]: https://www.cambridge.org/us/universitypress/subjects/mathematics/mathematical-finance/algorithmic-and-high-frequency-trading
[ref4]: https://ora.ox.ac.uk/objects/uuid:32248d98-3e1a-499a-ae2f-8cddd64beb4c/files/mfad4d8e1e746ae46c316870ff219aa74
[ref5]: https://www.stern.nyu.edu/sites/default/files/assets/documents/con_035928.pdf
[ref6]: https://hftbacktest.readthedocs.io/en/latest/tutorials/High-Frequency%20Grid%20Trading%20-%20Simplified%20from%20GLFT.html
[ref7]: https://www.optiver.com/insights/explainers/market-making/
[ref8]: https://jamesstuchbery.com.au/assets/pdfs/OBI.pdf
[ref9]: https://business.columbia.edu/sites/default/files-efs/pubfiles/25461/queue-value-2016.pdf
[ref10]: http://wp.lancs.ac.uk/finec2018/files/2018/09/FINEC-2018-036-Lars-Nord%C3%A9n.pdf
[ref11]: https://academic.oup.com/qje/article/130/4/1547/1916146
[ref12]: https://hftbacktest.readthedocs.io/en/latest/tutorials/GLFT%20Market%20Making%20Model%20and%20Grid%20Trading.html
