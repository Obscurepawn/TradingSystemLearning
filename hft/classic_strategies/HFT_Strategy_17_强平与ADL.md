# 经典高频交易策略（17）强平瀑布、保险基金与 ADL

加密衍生品普遍允许高杠杆，极端行情下大量仓位会被交易所强制平仓。强平单涌入变薄的盘口，把价格进一步打穿，触发更多强平，形成“瀑布”。这篇讲清楚强平是怎么触发的、交易所用哪些机制兜底（保险基金、自动减仓 ADL），以及“在瀑布里承接流动性”这类策略要注意什么。

需要[第 15 篇](HFT_Strategy_15_永续资金费率与交割基差.md)永续合约的基本概念。

## 1. 先分清三种价格

加密交易所里至少有三种“价格”，用途各不相同：

- **最新价**（last price）：最近一笔成交价，用于撮合；
- **指数价格**（index price）：多家现货交易所价格的加权或中位数，用来防止单一交易所被“插针”（瞬间异常成交）；
- **标记价格**（mark price）：在指数价基础上做资金费率调整与基差平滑，用于计算未实现盈亏和**触发强平**，不一定等于盘口最新成交价。

为什么强平不用最新价？因为最新价可以被一笔大单瞬间打出一个离谱的价格，如果按它强平，大量本来健康的仓位会被误杀。

Binance 开发者社区资料给出的标记价构造是多个值的中位数（资金调整后的指数、基差平滑参考价、合约价）[[1]][ref1]；Deribit 官方口径：标记价 = 指数价 + 30 秒 EMA（最新价 − 指数价），并对标记价与指数的偏离设上下限（BTC 默认 10%）[[2]][ref2]。EMA 是指数移动平均，越新的数据权重越大，用来平滑掉短时噪声。

## 2. 强平是怎么触发的

用 10 倍杠杆开多 1 BTC，开仓价 60,000，只需要交 6,000 USDT 保证金。价格每跌 1,000，亏 1,000 USDT。

```text
示例：强平价与瀑布的量级概念（手算演示，非具体交易所规则）

某 10x 多头，开仓 P0 = 60,000，保证金 6,000 USDT/币
粗略强平价（不计维持保证金与费用）：亏损吃光保证金时
  60,000 − 6,000 = 54,000 = 60,000 × (1 − 1/10)

更接近实际（维持保证金率 1%）：
  交易所要求账户权益不低于仓位价值的 1%，即
  6,000 − (60,000 − P) ≥ 0.01 × P
  临界点：0.99 P = 54,000 → P ≈ 54,545
  也就是价格还没跌到 54,000，在约 54,545 时就会被强平。

价格触及强平价时系统强制市价卖出；
若盘口深度不足以承接，卖单把价格继续向下一档档打穿，
触发更多账户强平，形成自我强化的瀑布。
```

**维持保证金**是交易所要求账户里至少留着的那部分钱，作为强平过程中的缓冲。杠杆越高，强平价离开仓价越近：10 倍杠杆大约跌 9%–10% 就被强平，50 倍杠杆约 2%。

## 3. 保险基金与 ADL

强平单如果能以比破产价（保证金恰好亏光的价格）更好的价格成交，多出来的钱进入**保险基金**（insurance fund）；如果成交得更差，亏空由保险基金填补。

当行情太极端、保险基金也不够时，交易所触发 **ADL**（自动减仓，auto-deleveraging）：按“盈利比例 × 有效杠杆”给对手方的盈利仓位排队，自动平掉最赚钱、杠杆最高的那些仓位，用它们来吸收亏损。也就是说，在 ADL 下，你赚钱的仓位可能被强制按某个价格平掉，不管你愿不愿意。

![强平瀑布链路：触及强平价 → 保险基金承接 → 不足则 ADL → 市价卖单打穿盘口 → 触发下一批强平](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/fig09_liquidation_cascade.png)

*图 1：强平瀑布的自我强化循环，保险基金不足时由 ADL 吸收对手方盈利仓位。*

## 4. 强平承接策略

强平承接策略在强制卖压出清之后，用被动挂单提供流动性，赚取价格回归的收益。它本质上是一种在特殊时刻的做市：别人被迫卖，你在下面接，赌的是强制卖压过去后价格会反弹一部分。

- **看什么**：全网未平仓量（OI，open interest）、各杠杆档位的分布、强平价地图（liquidation map，估算各价位上有多少仓位会被强平）、保险基金余额；标记价相对最新价的偏离、指数价构成与上下限；ADL 排序指标与本账户所处的 ADL 队列等级；
- **怎么做**：只做被动承接报价，严格限额与止损；瀑布里流动性撤得很快，报价必须能在秒级内撤销。风控依据用标记价而不是最新价，避免被单所瞬时插针误导 [[1]][ref1]。

## 5. 两次有公开出处的事件

**2021-05-19**：BTC 当日最大回撤约 33.9%（UTC 午夜至 13:09），Binance 出现交易与接口中断，散户无法平仓或追加保证金。Baumgartner & Guettler 的 IWH 讨论论文记录了这次闪崩、平台中断与保险基金余额变化（前一日 2.904 亿美元、次日 2.887 亿美元），并提出数据回填与保险基金保护方面的疑点（论文观点，非认定结论）[[3]][ref3]。

**2025-10-10 至 11**：IMF《Crypto Assets Monitor Q1 2026》记载，美国对华关税公告引发风险偏好回落，约 190 亿美元杠杆仓位被自动强平，为 CoinGlass 统计口径下加密史上最大的强平事件，自动化强平放大了下跌；报告同时指出极端波动中缺乏熔断机制、流动性持续偏薄 [[4]][ref4]。Bieganowski & Ślepaczuk 把这次闪崩当作自然实验，对比 taker 与 maker 策略在事件中的表现分化，实证了逆向选择与算法交易的系统性风险 [[5]][ref5]。

## 6. 什么时候会亏

- 自己承接的仓位在二次下杀中被套（瀑布往往不止一波）；
- ADL 可能平掉自己的盈利仓；
- 保险基金穿底后，部分交易所会把剩余损失分摊给全体盈利方（社会化损失）；
- 交易所宕机时无法撤单、无法对冲，这是 2021-05-19 事件里最直接的教训。

## 小结

- 强平按标记价触发，杠杆越高强平价越近；维持保证金让强平比“保证金亏光”更早发生。
- 强平亏空先由保险基金承担，不够再用 ADL 平掉对手方的盈利仓位。
- 强平承接是特殊时刻的做市，必须限额、止损、能快速撤单，并以标记价做风控。

---

上一篇：[加密跨所套利](HFT_Strategy_16_加密跨所套利.md) ｜ 下一篇：[链上 DeFi：AMM、LVR 与 MEV](HFT_Strategy_18_链上AMM与MEV.md)

## 参考资料

1. Binance Developer Community（dev.binance.vision）. [*Futures MarkPrice / IndexPrice Calculation 讨论帖*][ref1]，2021。资金费/标记价公式的社区讨论帖，非官方页面，数字以官方文档为准。
2. Deribit. [*Prices of Virtual Assets（官方帮助页）*][ref2]。指数构成、到期 30 分钟 TWAP、标记价 EMA 与带宽。
3. T. Baumgartner, A. Guettler. [*Bitcoin Flash Crash on May 19, 2021: What Did Really Happen on Binance?（IWH Discussion Paper 2022-25）*][ref3]，2022。论文。闪崩时间线、平台中断、保险基金（论文观点，非认定结论）。
4. IMF. [*Crypto Assets Monitor Highlights: Q1 2026*][ref4]，2026。官方报告。2025-10-10/11 约 190 亿美元强平（CoinGlass 口径）、无熔断、USDT/USDC 主导。
5. B. Bieganowski, R. Ślepaczuk. [*Explainable Patterns in Cryptocurrency Microstructure（arXiv:2602.00776）*][ref5]，2026。论文。Binance 期货 1s 数据、SHAP 特征、2025-10 闪崩自然实验。

[ref1]: https://dev.binance.vision/t/futures-markprice-indexprice-calculation-problem/870
[ref2]: https://deribit.zendesk.com/hc/en-us/articles/25954416110877-Prices-of-Virtual-Assets
[ref3]: https://www.iwh-halle.de/fileadmin/user_upload/publications/iwh_discussion_papers/iwh-discussion-paper_2022_25_Baumgartner_Guettler.pdf
[ref4]: https://www.imfconnect.org/content/dam/imf/News%20and%20Generic%20Content/GMM/Special%20Features/Crypto%20Monitor%20Q1%202026.pdf
[ref5]: https://arxiv.org/abs/2602.00776
