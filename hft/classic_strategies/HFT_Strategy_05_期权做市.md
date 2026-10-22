# 经典高频交易策略（5）期权做市：理论价、波动率曲面、Greeks 与加密期权

股票做市对着中间价报价，期权做市对着**模型算出的理论价**报价。期权的价格同时受标的价格、时间和波动率影响，做市商要管理的风险也多出好几个维度。这篇先用最少的篇幅讲清期权是什么、理论价和隐含波动率怎么来，再讲 Greeks 对冲、交易所的做市商保护机制、机构的集中风险管理，最后看加密市场最大的期权交易所 Deribit。

建议先读[做市（一）](HFT_Strategy_03_做市一_库存与报价.md)。导读数学小抄的“导数 = 敏感度”（2.7）在这里很重要。

## 1. 期权入门：三分钟版本

**看涨期权**（call）给买方一个权利：在到期日按约定价格（**行权价** $K$）买入标的。**看跌期权**（put）则是按行权价卖出的权利。买方为这个权利支付一笔**期权费**（premium），卖方收下期权费、承担义务。

到期时看涨期权值多少钱很简单：

$$\text{到期价值}_{\text{call}} = \max(S_T - K,\ 0)$$

$S_T$ 是到期时的标的价格。如果标的涨到 110、行权价 100，按 100 买、立刻按 110 卖，赚 10；如果标的只有 95，没人会按 100 去买，权利作废，价值为 0。看跌期权对称：$\max(K - S_T, 0)$。

**到期前**期权值多少钱就复杂了：还没到期，标的可能涨也可能跌。决定它的主要因素有：

- 标的现价 $S$ 离行权价 $K$ 多远；
- 离到期还有多久（时间越长，“翻盘”的机会越多）；
- 标的波动有多大（波动越大，大涨的可能越大，而期权的亏损最多就是期权费，所以波动大对期权买方有利，期权更贵）；
- 利率和分红。

**期权定价模型**（Black–Scholes 族）就是把这些输入变成一个价格的公式。其中只有一个输入看不见：未来的波动率。

## 2. 理论价与隐含波动率曲面

既然波动率是唯一看不见的输入，市场就倒过来用：把期权的**市场价格**代入模型，反解出“要让模型价格等于市场价格，波动率得是多少”，这个数叫**隐含波动率**（IV，implied volatility）。IV 可以理解成“市场报价里蕴含的对未来波动的预期”，期权交易员之间常常直接用 IV 报价 [[1]][ref1]。

如果模型完美，同一标的所有期权的 IV 应该相同。现实中不同行权价、不同到期日的期权反解出来的 IV 各不相同，把它们画在“行权价 × 到期日”的平面上，得到一张起伏的曲面，叫**波动率曲面**：

- **偏斜**（skew）：同一到期日，不同行权价的 IV 不同，常见的是低行权价（深度下跌保护）的 IV 更高；
- **期限结构**：同一行权价，不同到期日的 IV 不同。

做市商的流程就是：维护一张自己的波动率曲面 → 对每个期权算理论价 → 在理论价两侧报价：

$$\text{报价} = \text{理论值} \pm \text{半价差}$$

理论值随曲面 IV、标的价、剩余时间滚动重算 [[1]][ref1]。

## 3. Greeks：期权的“敏感度仪表盘”

**Greeks**（希腊字母）是期权价格对各个输入的敏感度，也就是导数（导读 2.7）：

| 名称 | 含义 | 直觉 |
|---|---|---|
| Delta | 标的价格涨 1，期权价格变多少 | 看涨期权的 Delta 在 0 到 1 之间，深度实值接近 1 |
| Gamma | 标的价格涨 1，Delta 变多少 | Delta 的变化速度，临近到期、平值附近最大 |
| Vega | IV 涨 1 个百分点，期权价格变多少 | 波动率风险 |
| Theta | 过了一天，期权价格变多少 | 时间流逝带来的价值衰减 |

### 3.1 Delta 对冲

做市商卖出看涨期权后，标的涨了会亏钱，这是方向风险。办法是买入一定数量的标的抵消：

```text
示例（教学假设）：卖出 10 张看涨期权，每张对应 1 单位标的，每张 Delta = 0.5。
组合 Delta = −10 × 0.5 = −5（相当于空了 5 单位标的）
对冲：买入 5 单位标的，组合 Delta ≈ 0。

标的涨 1 元：期权空头约亏 10 × 0.5 × 1 = 5 元，标的多头赚 5 元，抵消。
```

**Gamma 让对冲不能一劳永逸。** 设每张期权 Gamma = 0.04，标的涨 1 元后每张 Delta 变成 0.54，组合 Delta = −5.4 + 5 = −0.4，又出现了敞口，需要再买 0.4 单位。标的一直在动，对冲就要反复调整（**再平衡**）。

**对冲频率与成本要权衡**：对冲越勤，残留的 Delta/Gamma 风险越小，但交易成本与滑点越高；机构常在期货上对冲标的敞口。

### 3.2 Vega 管理

组合对隐含波动率变化的敏感用 Vega 衡量。标的买卖改变不了 Vega，所以 Vega 通常通过买卖其他行权价、其他到期日的期权（价差、日历、风险反转等组合）来对冲 [[2]][ref2]。

```text
示例：某看涨期权 Vega=0.3，IV 从 10% 升到 11.5%（+1.5 个百分点），
期权价格上升 1.5 × 0.3 = 0.45 [[2]][ref2]。
```

### 3.3 成百上千个系列怎么管

一个期权做市商同时管理单一标的上成百上千个行权价、到期日的组合，如果对每个系列分别做最优控制，是一个维度极高的问题。Baldacci、Bergault、Guéant 用组合 Vega 近似把高维问题降维：在经典随机波动率模型（如 Heston）下把问题化为低维方程，用数值方法求解，即使组合很大也可处理 [[3]][ref3]。工程含义是**按 Vega 等风险因子把整个组合的敞口聚合起来，再统一报价与对冲**，而不是逐个系列独立优化。

## 4. 真做起来

**要看的信号**：隐含波动率曲面的水平、偏斜、期限结构；已实现波动率（标的实际走出来的波动）与 IV 的差；组合净 Delta、净 Vega、Gamma、Theta [[1]][ref1][[2]][ref2]。财报、议息会议等事件前后 IV 会抬升，报价与对冲都要预留空间。

**规模**：大型期权做市商要同时维护海量系列的报价，Citadel Securities 称其在美股上市期权跨逾百万系列提供连续流动性，约占美股期权成交量 30% 以上 [[4]][ref4]。报价更新、Delta/Vega 对冲腿、保证金与行权交割管理需要一体化，机构普遍自研交易与风控系统 [[5]][ref5][[6]][ref6]。

### 4.1 做市商保护机制（MMP）

想象你的系统出了 bug，几百个系列的报价同时错了，几秒内被人全部扫光，后果可能是灾难性的。所以交易所在撮合引擎里内建了**做市商保护**（MMP，Market Maker Protection）：做市商自设数量阈值或 Greeks（Delta/Gamma/Vega）阈值，一段时间内成交越过阈值时，交易所自动撤掉保护范围内仍挂在簿上的报价，留出改价窗口，避免失控时被一次性全部成交。Knight Capital 2012 年的事故后，这类机制在期权市场普及。

Optiver 的公开主张是：阈值参数应由做市商按自身风险承受力定制（可按单产品、分段或连接会话设范围），支持实时电子查询、调整、重置和 API 配置，并在越限撤单时实时通知做市商以便重置后重新报价 [[7]][ref7]。

### 4.2 中央风险账簿

机构层面还有**中央风险账簿**（central risk book）：分散在期权、期货、现货、ETP 各做市单元的库存不逐腿对冲，而是汇集到一个账簿集中管理，彼此能抵消的先抵消（比如期权部门的多头 Delta 和现货部门的空头头寸），剩下的净敞口再统一对冲。Optiver 2021 年公开介绍，其中央风险账簿汇集期权、期货、现货与 ETP 做市单元的全部风险，现货净头寸超 10 亿欧元，并经 Tradeweb 以询价（RFQ）方式向机构对手方提供报价 [[8]][ref8]。这说明在机构规模上，对冲是“先跨单元轧差，剩余净敞口才用期货或期权对冲”。

## 5. 什么时候会亏

- 裸卖期权承担尾部亏损：卖出看涨期权理论上亏损无上限；
- IV 突然跳升，卖出期权的一方亏 Vega；
- Gamma 在临近到期时放大，标的稍微一动 Delta 就大变，对冲不及时会亏损；
- 曲面套利：别处有更便宜的同风险组合，会把理论价差吃掉。

美股期权、国内期权、场外报价的逻辑相通，具体做市义务与豁免以各交易所做市商办法为准。

## 6. 加密期权：Deribit

Deribit 占据加密期权绝大部分成交量，Alexander 等记载超过 90% 的交易所期权交易在 Deribit [[9]][ref9]。做市逻辑和上面一样：双边报价赚价差，用永续或期货做 Delta 对冲，同时管理 Vega、Gamma 与时间价值。不同的是几个市场细节。

### 6.1 币本位：期权费用 BTC 计

Deribit 的 BTC 期权以 BTC 报价、以 BTC 结算，期权费与保证金都是 BTC；1 张对应 1 BTC。到期结算价取 Deribit 指数在到期前 30 分钟的时间加权平均，每 4 秒采样一次，共 450 个样本（30 × 60 ÷ 4 = 450）[[10]][ref10][[9]][ref9]。指数由 Bitstamp、Coinbase、Kraken、LMAX Digital 等多家现货交易所等权构成，偏离中位数超过 ±0.5% 的报价会被调整 [[10]][ref10]。

“币本位”意味着：你账户里的保证金是 BTC，BTC 本身的美元价值在波动，所以风险预算要按 BTC 计价单独管理。这类反向（inverse）产品的定价和对冲比普通期权多一层非线性，见 Alexander、Chen、Imeraj 的研究 [[9]][ref9]，币本位合约的非线性在[第 15 篇](HFT_Strategy_15_永续资金费率与交割基差.md)有算例。

### 6.2 费用

```text
示例：Deribit 期权费计算（官方示例改写，出自 Deribit 费率页）[[11]][ref11]

BTC 期权成交价 0.008 BTC，数量 3：
  费率 = MIN(0.0003 BTC, 0.125 × 期权价) × 数量
       = MIN(0.0003, 0.125×0.008=0.001) × 3
       = 0.0003 × 3 = 0.0009 BTC
（maker 与 taker 同费率，与股票式 maker/taker 不对称不同。）

期货/永续：BTC 期货 maker 0.00%、taker 0.05%；
周度期货 maker 为 -0.01% 返佣、taker 0.05% [[11]][ref11]。
```

公式里的 MIN 是一个封顶设计：每张期权手续费是 0.0003 BTC（即标的的 0.03%），但不超过期权价格的 12.5%，避免便宜的深度虚值期权手续费比期权本身还贵。

### 6.3 Deribit 的 MMP

Deribit 规则书定义的 MMP 是自动撤回报价机制：当 Delta 或数量计数器达到或超过阈值时触发，只撤销标记为 MMP 的订单；MMP 需要向交易所申请，由交易所决定是否开通 [[12]][ref12]。这是 4.1 节同一思想在单一加密交易所的落地。

### 6.4 看什么、怎么做、什么时候亏

- **看**：隐含波动率曲面、Delta、Vega、Gamma、Theta；永续/期货对冲腿的基差与资金费率（[第 15 篇](HFT_Strategy_15_永续资金费率与交割基差.md)）；MMP 的 Delta 阈值、数量阈值、冻结时长配置；到期结算的 30 分钟平均窗口与指数构成 [[10]][ref10]。
- **做**：Delta 对冲高频执行，对冲腿多为 taker，成本要计入报价；Gamma 大的近月仓位需要反复再平衡；MMP 在系统或模型异常时自动撤单，避免失控报价继续成交 [[12]][ref12]。
- **亏**：波动率突变使 Vega 敞口暴露；Gamma 挤压下对冲被迫追涨杀跌；到期前结算窗口内的波动；深度虚值合约流动性很薄；反向期权的非线性定价误差 [[9]][ref9]。

## 小结

- 期权做市的中心是理论价，理论价的核心输入是隐含波动率曲面。
- Delta 对冲方向风险，Gamma 决定对冲要多勤，Vega 只能用其他期权对冲。
- MMP 和中央风险账簿是机构把“上千个系列”的风险管住的两大工具。
- Deribit 的币本位结算给定价、保证金和风险预算多加了一层非线性。

---

上一篇：[做市（二）逆向选择与队列](HFT_Strategy_04_做市二_逆向选择与队列.md) ｜ 下一篇：[波动率套利与箱式价差](HFT_Strategy_06_波动率套利与箱式价差.md)

## 参考资料

1. Optiver. [*Options volatility explainer*][ref1]。IV由期权市场价反解，做市商据IV评估期权价值并报价。
2. Optiver. [*Option Greeks explainer*][ref2]。Delta/Gamma/Theta/Vega/Rho定义与敏感度算例。
3. B. Baldacci, P. Bergault, O. Guéant. [*Algorithmic market making for options（arXiv:1907.12433）*][ref3]，2020。论文。用组合 vega 近似把单标的期权做市的高维随机控制降为低维泛函方程，Heston 下 Euler+插值求解，附数值例。
4. Citadel Securities. [*Options*][ref4]。美股上市期权最大场内做市商，约30%市场份额，逾百万系列连续报价。
5. Jane Street. [*What We Do*][ref5]。全球流动性提供商，200+电子交易所，自研OCaml交易与风控系统，覆盖ETF/股票/债券/期权。
6. Citadel Securities. [*What Is a Market Maker?*][ref6]。双向报价、价差收入、承担市场风险与受监管业务。
7. Optiver. [*Market-maker protections*][ref7]。MMP 内建于撮合引擎，数量/delta/gamma/vega 阈值越限自动撤报价；建议实时可调、API 配置、越限实时通知；Knight Capital 2012 背景。
8. Optiver. [*Optiver launches cash equity offering for institutional investors*][ref8]，2021。新闻。central risk book 汇集期权/期货/现货/ETP 做市单元风险，现货净头寸超 €10 亿，经 Tradeweb RFQ 提供自然流动性。
9. C. Alexander, D. Chen, A. Imeraj. [*Inverse and Quanto Inverse Options in a Black-Scholes World（arXiv:2107.12041）*][ref9]，2021。论文。反向产品定价、对冲与市场不完全性；Deribit 占交易所期权 90%+。
10. Deribit. [*Prices of Virtual Assets（官方帮助页）*][ref10]。指数构成、到期 30 分钟 TWAP、标记价 EMA 与带宽。
11. Deribit. [*Fees（官方帮助页）*][ref11]。期货/永续/期权 maker-taker 费率与封顶规则。
12. Deribit. [*Exchange Rulebook（含 MMP、HFT 定义、co-location/proximity hosting）*][ref12]，2025。官方规则书。MMP 自动撤单机制、HFT/co-location 定义。

[ref1]: https://www.optiver.com/insights/explainers/options-volatility/
[ref2]: https://www.optiver.com/insights/explainers/option-greeks/
[ref3]: https://arxiv.org/abs/1907.12433
[ref4]: https://www.citadelsecurities.com/what-we-do/options/
[ref5]: https://www.janestreet.com/what-we-do/
[ref6]: https://www.citadelsecurities.com/what-we-do/what-is-a-market-maker/
[ref7]: https://www.optiver.com/insights/markets/market-maker-protections/
[ref8]: https://www.optiver.com/insights/news/optiver-launches-cash-equity-offering-for-institutional-investors/
[ref9]: https://arxiv.org/abs/2107.12041
[ref10]: https://deribit.zendesk.com/hc/en-us/articles/25954416110877-Prices-of-Virtual-Assets
[ref11]: https://deribit.zendesk.com/hc/en-us/articles/25944746248989-Fees
[ref12]: https://deribit.zendesk.com/hc/en-us/articles/25944555524125-Deribit-Exchange-Rulebook
