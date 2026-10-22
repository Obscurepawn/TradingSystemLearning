# 经典高频交易策略（18）链上 DeFi：AMM、LVR 与 MEV

前面所有策略都发生在有订单簿的交易所里。区块链上的去中心化交易所（DEX）换了一套机制：没有订单簿，价格由一个公式决定；交易要先进入公开的“待打包交易池”，再由区块生产者决定顺序。这带来了两个新话题：

- **AMM 与 LVR**：用公式做市的流动性池为什么会被套利者持续“薅羊毛”；
- **MEV**：在区块里重排、插入交易能额外拿到多少钱，以及以太坊和二层网络怎么组织这场竞争。

需要[做市（一）](HFT_Strategy_03_做市一_库存与报价.md)里做市的基本思路，以及导读数学小抄里的方差（2.3）。

## 1. 常数乘积池怎么报价

### 1.1 不变量 $xy=k$

**AMM**（自动做市商）不用订单簿，靠公式报价。最经典的是 Uniswap v2 的**常数乘积池**：池子里放着两种币，比如 $x$ 个 USDC（计价币）和 $y$ 个 ETH（基础币），规则只有一条：**交易前后 $x \times y$ 保持不变**（暂不考虑手续费），记为 $k$。

池子的当前价格就是两种储备的比值：$P = x/y$（多少 USDC 换 1 ETH）。

为什么这个规则能自动定价？你往池里放 USDC、拿走 ETH，$x$ 变大、$y$ 变小，为了保持乘积不变，你拿走的 ETH 必须越来越少：池里 ETH 越稀缺，ETH 就越贵。

### 1.2 一笔交易能换多少

交易者投入 $\Delta x$ 个 USDC，池子先扣掉比例为 $1-\gamma$ 的手续费，实际参与兑换的是 $\gamma\Delta x$，收到的 ETH 为：

$$\Delta y = y - \frac{k}{x + \gamma\Delta x}$$

推导：交易后池里的 USDC 变成 $x + \gamma\Delta x$，为了保持乘积为 $k$，池里的 ETH 只能剩下 $\frac{k}{x+\gamma\Delta x}$，多出来的部分就是交易者拿走的 $\Delta y$。

Uniswap v2 每笔兑换收 0.30% 手续费（即 $\gamma=0.997$），其中 0.25% 归流动性提供者（LP，往池子里存币的人）、0.05% 为可开关的协议费（ethereum.org 注解教程记载）[[1]][ref1]。

### 1.3 含费无套利区间

因为有手续费，池价和外部市场价（比如中心化交易所的价格）不必完全相等：

- 从池里买 ETH 的边际价格是 $P_{\text{pool}}/\gamma$（要多付手续费）；
- 往池里卖 ETH 的边际价格是 $\gamma P_{\text{pool}}$（要被扣手续费）。

外部参考价 $P_{\text{ext}}$ 只有满足 $P_{\text{ext}} > P_{\text{pool}}/\gamma$（外部更贵，就从池里买、到外部卖）或 $P_{\text{ext}} < \gamma P_{\text{pool}}$（外部更便宜，就从外部买、卖给池子）时，扣费后才有套利空间。换句话说，外部价格落在池价附近的 $[\gamma P_{\text{pool}},\, P_{\text{pool}}/\gamma]$ 区间里时，套利者无利可图（等价地，也可以说池价被限制在外部价附近的 $[\gamma P_{\text{ext}},\, P_{\text{ext}}/\gamma]$ 区间内）。精确边界由不变量与费用联立求解，Angeris 等给出了常数乘积市场紧跟参考价的正式条件 [[2]][ref2]。

![AMM 含费无套利带宽：外部参考价落在 γP 到 P/γ 区间内时套利者扣费后无利可图](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/fig07_amm_arb_band.png)

*图 1：常数乘积池的含费无套利带宽 $[\gamma P,\, P/\gamma]$，池价可在带内自由漂移而不触发套利。*

### 1.4 算两个例子

```text
示例 A：x·y=k 与含费无套利带宽（手算演示）

池：x = 100,000 USDC，y = 20 WETH，k = 2,000,000
池价 P0 = x/y = 5,000 USDC/ETH，γ = 0.997（0.3% 费）

带宽边界：
  下界 = γ × P0 = 0.997 × 5,000 = 4,985.0
  上界 = P0 / γ = 5,000 / 0.997 ≈ 5,015.05
外部参考价落在 (4,985.0, 5,015.05) 内时，套利者扣费后无利可图，池价可在带内漂移。

示例 B：小额换入（手算演示）
投入 Δx = 1,000 USDC，实际入池 γΔx = 997：
  Δy = 20 - 2,000,000/(100,000+997) = 20 - 19.8026 ≈ 0.1974 ETH
  含费均价 ≈ 1,000/0.1974 ≈ 5,066 USDC/ETH，劣于池价 5,000。
```

WETH 是以太坊上 ETH 的代币化形式，可以当作 ETH 看。示例 B 里均价比池价差了约 1.3%：其中 0.3% 是手续费，其余约 1% 是这笔交易相对池子规模（1,000 vs 100,000）太大造成的“滑点”。

## 2. LP 为什么结构性亏钱：LVR

### 2.1 直觉

往池子里存币的 LP，相当于一个**永远按公式被动报价、从不撤单**的做市商。外部市场 ETH 涨了，池价还停在旧位置，套利者就来池里低价买走 ETH，直到池价追上外部价；ETH 跌了，套利者就把 ETH 高价卖给池子。每一次，LP 都是在“旧价格”上和更知情的套利者成交，这正是[做市（二）](HFT_Strategy_04_做市二_逆向选择与队列.md)讲的逆向选择，只是 AMM 连撤单的机会都没有。

### 2.2 公式

Milionis、Moallemi、Roughgarden、Zhang 证明，LP 的回报可以拆成市场风险项（币价本身涨跌带来的盈亏）加微观结构项（手续费收入减去被套利者拿走的损失）。后者里的损失叫 **LVR**（loss-versus-rebalancing，损失 vs 再平衡）：把 LP 和一个“持有同样仓位、但总能按外部市价调仓”的对照组合相比，LP 系统性少赚的那部分 [[3]][ref3]。

对常数乘积池，瞬时 LVR 率为：

$$\ell(\sigma,P) = \frac{L\sigma^2}{4}\sqrt{P},\qquad \frac{\ell(\sigma,P)}{V(P)} = \frac{\sigma^2}{8}$$

- $L$：池子的流动性规模，满足 $xy = L^2$（所以 $L=\sqrt{k}$）；
- $P$：价格；
- $\sigma$：价格的波动率，这里是**收益率**（百分比）波动，和做市模型里的算术价格波动不同；
- $V(P)$：池子的总价值，常数乘积池有 $V(P) = x + Py = 2L\sqrt{P}$；
- $\ell$：单位时间被套利者拿走的价值。

第二个式子是关键：**每单位池价值的 LVR 损耗率只取决于方差 $\sigma^2$，等于方差的八分之一**。波动越大，LP 被“换走”的越多；而且这和价格最终涨还是跌无关，只要价格在动就在损耗。

论文自算示例：ETH 日波动 $\sigma=5\%$ 时，LP 每日约损失 $\sigma^2/8=3.125$ 个基点的池价值给 LVR [[3]][ref3]。

```text
示例 C：LVR 日损耗（论文示例）[[3]][ref3]
σ = 5%/日：σ²/8 = 0.0025/8 = 0.0003125 = 3.125 bp/日。
```

所以 LP 赚不赚钱，可以粗略看：对冲掉币价风险后，LP 盈亏 ≈ 手续费收入 − LVR [[3]][ref3]。手续费收入不够覆盖 $\sigma^2/8$，就是在亏钱。对冲只能转移市场风险，不能消除 LVR。

## 3. MEV：区块里的抢跑

### 3.1 什么是 MEV

链上交易发出后，不会立刻执行，而是先进入一个公开的**内存池**（mempool，待打包交易池），等区块生产者挑选、排序后打包进区块。谁能决定顺序，谁就能做文章：

- 看到一笔大额买单，抢在它前面先买（抢跑），等它把价格推高后再卖；
- 把一笔交易夹在中间，前面买、后面卖（“三明治攻击”）；
- 发现 DEX 和 CEX 之间的价差，抢先套利；
- 抢先清算借贷协议里抵押率不足的仓位。

在区块内重排、插入、抢跑交易所能额外拿到的利润，叫 **MEV**（最大可提取价值），可以理解成传统市场抢跑在链上的对应物。Daian 等的《Flash Boys 2.0》在 2019 年量化了 DEX 套利机器人的抢跑行为和**优先燃气拍卖**（PGA：大家竞相抬高交易手续费，出价高的排前面），提出了 MEV（当时称 miner extractable value）这个概念，并指出高额优先费对区块链共识层的系统性风险 [[4]][ref4]。

### 3.2 以太坊怎么组织这场竞争

![MEV/PBS 流程：公开 mempool、私有 Protect、MEV-Share 输入 → 搜索者 bundle → relay → 构建者竞价 → MEV-Boost → 验证者提议区块](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/fig08_mev_pipeline.png)

*图 2：以太坊 PBS/MEV-Boost 的区块空间竞价流程，EIP-1559 base fee 销毁、验证者仅收 priority fee。*

按图从左到右理解：

- **搜索者**（searcher）：寻找 MEV 机会的机器人，把自己的交易打包成一个“捆绑包”（bundle）；
- **构建者**（builder）：收集交易和捆绑包，排好顺序组成完整区块，并出价竞拍这个区块的上链权；
- **验证者**（validator）：以太坊的区块提议者，从构建者的出价里挑最高的那个提议上链。

把“建块”和“提议”分开的架构叫 **PBS**（提议者—构建者分离）：以太坊转为权益证明后，构建者竞价出售区块空间，验证者只挑最高出价；**MEV-Boost** 是 Flashbots 为此实现的开源链下 PBS 方案，验证者通过它向开放的构建者市场出售区块空间 [[5]][ref5][[6]][ref6]。

**私有订单流**：Flashbots Protect 把用户交易送进私有的交易池，对抢跑和三明治机器人不可见，并把一部分 MEV 与手续费返还给用户；MEV-Share 允许用户选择性地共享部分交易信息，换取分成 [[7]][ref7]。

**EIP-1559 手续费**：每个区块的**基础费**（base fee）按上一个区块的拥挤程度自动调整，并直接销毁；验证者只能拿到**优先费**（priority fee，小费）。所以抢位置的竞价发生在优先费这一层 [[8]][ref8]。

### 3.3 二层网络的排序器

二层网络（L2，在以太坊之上处理交易、再批量提交到主链的网络）通常由一个**排序器**（sequencer）负责接收和排序交易。以 Arbitrum 官方文档为例：单一排序器接收、排队并排序交易，没有公开的内存池；排序策略三选一——FCFS（先到先得，新链默认）、PGA（按优先费排序，Arbitrum One 当前采用）、Timeboost（拍卖出一条快速通道，其余先到先得）；PGA 模式每轮按优先费排序，平局按到达时间，并有防止低费交易永远排不上的加成机制 [[9]][ref9]。其他乐观 L2（Optimism、Base）采用单一排序器模式，具体规则以各自官方文档为准。

## 4. 真做起来

- **看**：CEX 最新价与多个池子的储备价、gas 价格（链上执行交易的费用）、出块时间、公开内存池里的待打包交易；LVR 与手续费收入的平衡（对冲后 LP 盈亏 ≈ 手续费 − LVR）[[3]][ref3]；清算机会看抵押率阈值、借贷池的健康因子。

## 5. 什么时候会亏

- 交易在打包前被复制或抢跑（公开内存池的交易对所有人可见）；
- 交易失败回滚，手续费照付；
- 智能合约风险：池子被操纵、价格预言机出错、清算合约漏洞；
- LVR 让被动 LP 结构性亏损，对冲只能转移市场风险、不能消除 LVR [[3]][ref3]；
- 排序器中心化与审查风险、优先费军备竞赛 [[4]][ref4][[9]][ref9]。

## 小结

- 常数乘积池按 $xy=k$ 定价，手续费给池价留出一条 $[\gamma P, P/\gamma]$ 的无套利带。
- LP 是“不撤单的做市商”，被套利者持续逆向选择，损耗率 LVR 约为 $\sigma^2/8$。
- MEV 来自对交易顺序的控制权；PBS、私有订单流、EIP-1559 和 L2 排序器规则共同决定这份利润归谁。

---

上一篇：[强平瀑布、保险基金与 ADL](HFT_Strategy_17_强平与ADL.md) ｜ 下一篇：[收尾：收益验证与策略总表](HFT_Strategy_19_收益验证与策略总表.md)

## 参考资料

1. ethereum.org. [*Uniswap v2 annotated code（官方教程）*][ref1]。v2 0.30% 费（0.25% LP + 0.05% 协议费）。
2. G. Angeris, H.-T. Kao, R. Chiang, C. Noyes, T. Chitra. [*An analysis of Uniswap markets（arXiv:1911.03380）*][ref2]，2019。论文。常数乘积市场紧跟参考价的正式条件。
3. J. Milionis, C. C. Moallemi, T. Roughgarden, A. L. Zhang. [*Automated Market Making and Loss-Versus-Rebalancing（arXiv:2208.06046）*][ref3]，2022。论文。LVR 分解、常数乘积 σ²/8 瞬时率。
4. P. Daian, S. Goldfeder, T. Kell, Y. Li, X. Zhao, I. Bentov, L. Breidenbach, A. Juels. [*Flash Boys 2.0: Frontrunning, Transaction Reordering, and Consensus Instability in Decentralized Exchanges（arXiv:1904.05234）*][ref4]，2019。论文。MEV/PGA 量化与共识风险。
5. Flashbots. [*MEV-Boost in a Nutshell（官方站点）*][ref5]。离链 PBS 实现、构建者市场。
6. ethereum.org. [*Maximal extractable value (MEV)（官方文档）*][ref6]。MEV 定义、searchers、PBS 与 commit-reveal。
7. Flashbots Docs. [*Flashbots Protect Overview / MEV-Share*][ref7]，2025。官方文档。私有 mempool、MEV/gas 返还。
8. Vitalik Buterin 等. [*EIP-1559: Fee market change for ETH 1.0 chain（官方 EIP）*][ref8]。base fee 销毁 + priority fee。
9. Arbitrum Docs. [*Sequencer architecture and transaction flow*][ref9]，2026。官方文档。FCFS/PGA/Timeboost 排序策略、队列与轮次。

[ref1]: https://ethereum.org/zh/developers/tutorials/uniswap-v2-annotated-code/
[ref2]: https://arxiv.org/abs/1911.03380
[ref3]: https://arxiv.org/abs/2208.06046
[ref4]: https://arxiv.org/abs/1904.05234
[ref5]: https://boost.flashbots.net/
[ref6]: https://ethereum.org/en/developers/docs/mev/
[ref7]: https://docs.flashbots.net/flashbots-protect/overview
[ref8]: https://eips.ethereum.org/EIPS/eip-1559
[ref9]: https://docs.arbitrum.io/how-arbitrum-works/deep-dives/sequencer-transaction-flow
