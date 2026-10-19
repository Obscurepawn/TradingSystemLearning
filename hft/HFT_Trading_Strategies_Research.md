# 高频交易策略研究：套利、做市与短周期预测

高频交易（High-Frequency Trading，HFT）的收益主要来自几类机制：提供即时成交的流动性、修复相关资产的定价偏离、预测短时间内的价格变化，以及在其他报价更新前完成交易。低延迟决定机会能否成交、对冲能否及时完成、旧报价能否撤回；收益的起点仍然是定价、预测和承担风险的能力。

期现套利、跨期套利与高频做市经常共用行情、定价和执行系统，但持仓方式差别很大。期现组合可以持有到期，跨期头寸可以持续数日；只有开平仓、报价或对冲依赖短暂机会时，它们才具有高频交易的特征。判断策略时，分别看**信号持续时间、订单更新频率和持仓期限**，比只看每天交易多少笔更有用。SEC 的文献综述也按做市、套利、结构性和方向性交易区分策略，而没有把 HFT 视为单一行为。[SEC，2014][ref-sec]

下表按收益机制组织公开资料中的主要策略家族。各行会交叉：ETF 做市可以使用期货对冲，订单流预测可以改善做市，跨市场套利也可能同时参与报价更新竞速。

| 策略家族 | 收益主要来自什么 | 典型交易或动作 | 高频执行的作用 | 最容易低估的问题 |
|---|---|---|---|---|
| 期现套利 | 期货价格偏离现货复制与持有成本 | 买现货、卖偏贵期货，或反向组合 | 同步建仓、减少复制误差 | 融资、借券、分红与保证金现金需求 |
| 跨期套利 | 不同到期月份之间的相对价值变化 | 买一个月份、卖另一个月份 | 交易价差订单、管理两腿成交 | 季节性、交割条件和期限曲线重估 |
| 高频做市 | 成交价差与流动性服务收入 | 持续双边报价，按库存和信号调整 | 取得队列位置、撤回失效报价 | 逆向选择与单边库存积累 |
| ETF 套利 | 基金份额与可交付篮子的价格差 | 买篮子卖 ETF，或买 ETF 卖篮子 | 实时估值、二级市场成交和对冲 | 申赎资格、篮子成本与结算时间 |
| 跨市场、三角套利 | 同一资产或兑换链的可成交价格不一致 | 买便宜市场、卖贵市场；完成兑换闭环 | 抢占短暂价差、控制未完成交易腿 | 库存分散、额度、结算与腿风险 |
| 统计套利、领先—滞后 | 相对价格回归，或信息向相关资产传播 | 多便宜资产、空贵资产；交易滞后资产 | 在信号衰减前成交 | 关系失效和伪造的时间领先 |
| 订单流短期预测 | 订单簿状态对未来价格的预测能力 | 主动买卖，或偏移做市报价 | 缩短信号到成交的时间 | 预测幅度不足以覆盖往返成本 |
| 期权做市、波动率相对价值 | 期权报价价差、波动率定价差与组合风险管理 | 多合约报价、Delta 对冲、风险转移 | 批量更新报价与对冲 | Gamma、Vega、跳跃和并发成交 |
| 事件、竞价不平衡交易 | 新信息或集中订单对价格的影响 | 新闻发布后交易、参与开收盘竞价 | 快速更新估值、进入有效成交窗口 | 事件解读、最终竞价量和成交价不确定 |

## 1. 期现套利：把期货价格与复制成本放在一起比较

以股票指数为例，期货对应未来某个时点的指数敞口，现货组合则从现在起占用资金，并在持有期间获得分红。在利率和连续股息率恒定、现货可复制且忽略交易摩擦的模型中：

$$
F^*(t,T)=S_t e^{(r-d)(T-t)}.
$$

$S_t$ 是现货价格，$r$ 是融资利率，$d$ 是连续股息率，$T-t$ 以年计。如果使用确定的离散分红现金流，则先从现货价格中扣除分红现值，再把余额计息到到期日。CME 的合理价值材料将融资和分红作为股指期货定价的核心输入。[CME：期货合理价值][ref-carry]

这里统一把基差定义为 $B_t=F_t-S_t$。有些市场资料使用相反符号，比较数据时先核对定义。基差为正本身不构成交易信号；与融资、分红和执行成本一致的理论基差，才是比较对象。

当期货偏贵时，交易者买入现货组合、卖出匹配敞口的期货。若能够一直持有，现货的价格变化与期货空头的价格变化互相抵消，留下初始定价差、分红和资金成本。期货偏便宜时，可以买期货、卖空现货，但现货借券是否可得、借券费用和空头分红补偿会改变反向套利的边界。

**交易决策使用整组头寸的可成交价格。** 正向套利比较期货买方愿意支付的价格与买入现货篮子的实际成本；反向套利比较卖出现货所得与买入期货的成本。盘口有多档时，按目标数量计算整组成交成本。仅用指数值和期货中间价相减，会漏掉买卖价差、篮子深度与成交数量约束。

为便于核对现金流，文中的数字例子均用于机制演算。设一组匹配头寸的现货买入成本为 100,000，期货开仓价格折算到相同敞口为 100,900；持有一个月，融资采用年利率 4% 的简单计息，期间收到分红 100，全部其他交易成本为 150。若到期复制与结算完全匹配，且分红计息和保证金资金成本在本例中忽略，则净收益为：

$$
100{,}900-100{,}000
-100{,}000\times\frac{4\%}{12}
+100-150
=516.67.
$$

这个计算的关键是两腿共同结算的现金流。若提前平仓，收益还取决于退出时的基差；若股指期货按特殊结算指数现金结算，现货篮子则按另一组实际成交价格卖出，结算差异也进入损益。

资本占用是这类策略的重要约束。期货只缴保证金，现货买入仍然要融资；期货空头在价格上涨时还会被逐日收取变动保证金。即使现货账面盈利相应增加，资金也未必能在同一时刻转到期货账户。CME 区分了初始保证金和逐日盯市形成的现金支付，这两部分对应不同的流动性需求。[CME：保证金与逐日盯市][ref-margin]

在高频系统中，最难的部分往往是复制和成交：成分股停牌、盘口深度不均、不同交易时段、股息估计变化，都使“指数点位相同”与“能够买到同一组现金流”产生距离。使用 ETF 代替完整股票篮子可以减少交易腿，但会留下 ETF 相对指数的跟踪差异。

## 2. 跨期套利：交易期限结构的变化

跨期交易买卖同一标的、不同到期日的期货。例如买近月、卖远月，表达近月相对远月走强的判断。对合约乘数相同、采用一比一头寸的组合，定义：

$$
D_t=F_t(T_1)-F_t(T_2),\qquad T_1<T_2.
$$

买入这个价差，就是买近月、卖远月；价差从 $D_{\text{entry}}$ 上升到 $D_{\text{exit}}$ 时，每组头寸毛收益为：

$$
\Pi_{\text{gross}}=M\left(D_{\text{exit}}-D_{\text{entry}}\right),
$$

$M$ 是每一点对应的合约金额。例如近月 4,000、远月 4,020，价差为 −20；之后近月 4,015、远月 4,027，价差变为 −12，买价差组合赚 8 个点，之后再扣两腿开平仓费用。价差由负变得没那么负，同样是价差上涨。[CME：跨期价差交易][ref-calendar]

不同品种的期限结构有不同驱动。股指期货主要受融资和分红期限结构影响；商品期货还受库存、仓储、季节性、运输及交割约束影响；利率期货的月份差异体现对应期间的利率预期。买近月、卖远月削弱共同方向风险，同时保留了这些期限结构风险。商品的便利收益，即持有实物库存带来的使用价值，也会影响不同月份的定价。[CME：期限结构与持有成本][ref-curve]

**两个合约没有共同到期日，跨期价差不存在必然归零的条件。** 近月到期时，远月仍有剩余期限。把一个历史平均价差作为目标，交易的是统计相对价值；如果能通过现货储存和交割复制两个期限之间的现金流，才有更强的持有成本约束。对季节性商品，把不同月份组合直接混在同一个历史均值里，会把正常季节差异当作异常。

一种可研究的信号是：先估计对应月份、剩余期限和市场状态下的参考价差 $\widehat D_t$，再看偏离 $D_t-\widehat D_t$ 是否超过成本和风险预算。参考价差会随库存、资金与供需信息更新；已经不适用的均值不能继续作为退出依据。对不同乘数、不同价格敏感度的合约，还要按经济敞口确定腿比例，而非默认一手对一手。

执行有两种主要方式。使用交易所原生价差订单，可以按组合净价和规定比例成交；自己分别发送两条腿，则可以选择更灵活的报价与成交方式，却会出现只成交一腿的敞口。CME 的外汇期货日历价差订单支持在同一价差交易中买卖两个到期月。[CME：外汇期货日历价差][ref-calendar-order]

原生组合订单控制的是每次组合成交的腿比例，整张订单仍可能只成交部分数量。策略还要判断剩余量是否保留、何时撤单，以及价差盘口与单腿盘口谁提供更好的净价。若主动拆腿，第一腿成交后，第二腿待买价格上涨或待卖价格下跌，执行损失可能超过全部预期价差收益。

## 3. 高频做市：报价、库存和成交质量

### 3.1 一笔成交究竟赚了什么

做市商持续给出愿意买入和卖出的价格。假如同样数量先以 100.00 买入、再以 100.02 卖出，毛收益为每单位 0.02。但实际成交经常集中在一边：买单不断成交，卖单无人问津，库存就在价格下跌时积累。

这种成交选择构成逆向选择：对手方更愿意在报价对自己有利时成交，做市商因而在买入后遇到价格下跌，或在卖出后遇到价格上涨。评估报价时，要同时研究成交概率和**已经成交这个条件下**的后续价格变化。Cartea、Jaimungal、Ricci 的模型将短期价格信号与相互激励的订单流纳入做市控制，研究了这类信息对报价的影响。[Cartea 等，2014，期刊摘要][ref-cjr]

令 $z=+1$ 表示买入、$z=-1$ 表示卖出，成交时刻为 $t_f$，成交价为 $p_f$，中间价为 $m$。用成交前一刻的中间价 $m_{t_f^-}$ 衡量初始价格优势，成交后经过 $h$ 时间的标记收益（markout）可写为：

$$
M_h=z\left(m_{t_f+h}-p_f\right)
=z\left(m_{t_f^-}-p_f\right)
+z\left(m_{t_f+h}-m_{t_f^-}\right).
$$

第一项是相对成交前盘口获得的价格优势，第二项是从成交前到评价时点、沿持仓方向计算的价格变化。例如以 100.00 买入，成交前中间价 100.01，随后跌到 99.96，那么最初获得的 0.01 被之后的 −0.05 抵消，标记收益为 −0.04。中间价标记仍未扣实际退出所付出的价差和费用。

对一笔固定单位报价，可用下面的形式组织决策：

$$
\text{报价价值}\approx
\Pr(\text{成交}\mid x,a)
\,\mathbb E\!\left[
M_h+\text{返佣}-\text{手续费}-\text{退出成本}
\mid\text{成交},x,a\right]
-\text{新增库存风险代价}.
$$

$x$ 包含当前盘口、队列位置、库存和短期信号，$a$ 是报价价格、数量及保留时长等行动。这是用于拆解问题的表达式，部分成交、多次成交和主动对冲还需在完整策略中逐笔处理。返佣会增加净收益；若成交后的不利价格变化和其他成本更大，交易仍然亏损。

### 3.2 Avellaneda–Stoikov 与后续库存模型

Avellaneda–Stoikov（AS）模型把做市写成库存风险与成交机会之间的优化问题：中间价按 $dS_t=\sigma dW_t$ 变化，偏离参考价 $\delta$ 的单位订单成交强度为 $\lambda(\delta)=Ae^{-k\delta}$，目标是最大化终点财富的指数效用。常用的有限期限近似为：

$$
r_t=S_t-q_t\gamma\sigma^2(T-t),
$$

$$
w_t=\gamma\sigma^2(T-t)
+\frac{2}{\gamma}\ln\left(1+\frac{\gamma}{k}\right),
\qquad
p_t^b=r_t-\frac{w_t}{2},\quad
p_t^a=r_t+\frac{w_t}{2}.
$$

$r_t$ 是库存调整后的保留价格，$w_t$ 是买卖报价的总价差，$q_t$ 是库存，$\gamma$ 是风险厌恶参数，$T$ 是风险评价终点。$A$ 控制参考价处的成交强度，$k$ 控制报价远离参考价后强度衰减的速度。$q_t>0$ 时，报价中心下移：降低继续买入的意愿，并用更有竞争力的卖价减少库存。模型采用连续价格、固定交易单位、无漂移价格过程、常数波动率和对称指数成交强度。[Avellaneda–Stoikov，2008][ref-as]

使用公式前可以直接检查单位。把一份固定交易单位的现金价值作为价格单位，库存 $q_t$ 记为这种单位的数量；若时间用秒，$\sigma$ 的单位就是价格除以平方根秒，$A$ 是每秒，$k$ 和 $\gamma$ 是价格的倒数。这样 $\gamma\sigma^2(T-t)$ 才具有价格单位。期货点数先与合约乘数统一，年化收益率波动率也要换算成该模型的算术价格波动率。

Guéant、Lehalle、Fernandez-Tapia（GLFT）在有库存上下界的模型中，将最优控制问题化为线性常微分方程组，并给出长时域近似。其原文指出，上面的 AS 闭式报价是接近终点时的 Taylor 近似；长时间运行的报价行为要用相应解或近似处理。这个终点效用设定按中间价评价剩余库存，没有显式的强制清仓成本，因此 $T-t$ 缩小也不等于策略会自动清仓。[GLFT，2013][ref-glft]

这些模型适合建立基线：观察风险厌恶、库存、波动率与成交强度如何改变报价。落到实际订单簿后，最小价格变动单位（tick）、队列位置、手续费、订单大小和撤单延迟都会改变最优动作。

### 3.3 实际报价如何形成

一轮做市决策从已收到的行情和自身订单回报开始。程序更新盘口、库存及在途委托，再估计短期参考价值和成交质量，最后决定报价、数量、保留或撤销订单。成交后，新增库存又会改变下一轮报价；若风险超出预算，就用主动成交或相关工具减少敞口。

| 决策层 | 主要输入 | 直接改变的动作 |
|---|---|---|
| 参考价值 | 本品种盘口、相关市场、短期预测 | 双边报价围绕哪里展开 |
| 库存控制 | 已成交库存、待确认成交风险、组合敞口 | 报价偏移、单边数量、对冲量 |
| 成交质量 | 队列位置、订单流、条件 markout | 哪个价格值得等待成交 |
| 执行约束 | tick、撮合规则、费率、消息限制 | 价格取整、保留、改单与撤单 |
| 交易保护 | 数据新鲜度、风险限额、连接状态 | 减量、撤回报价、停止新增风险 |

在价格时间优先的市场，同一价格上排在前面的订单，能够更早获得成交机会。保留队列位置有价值，撤单重挂会失去它；但参考价值已经恶化时，继续保留又可能承受更差的成交。Moallemi–Yuan 将这种队列价值分解为成交收益与逆向选择的权衡，以及排队向前推进的动态价值。[Moallemi–Yuan，2017 修订稿][ref-queue]

因此，最简单的“中间价两侧各挂固定距离”只能作为基线。库存偏移、带短期预测的报价、按队列位置选择订单，以及多品种风险统一管理，分别补充不同信息。复杂模型最终仍要回答一个具体问题：保留这张订单，比撤掉、重挂或立即对冲，多赚了多少净收益，又多承担了多少风险。

## 4. ETF 套利：二级市场成交与一级市场申赎

交易所交易基金（Exchange-Traded Fund，ETF）的份额在交易所买卖；基金的申购赎回机制则允许符合条件的参与方按规定篮子或现金，创建或赎回一组份额。美国市场的授权参与者（Authorized Participant，AP）与基金建立申赎协议，直接参与一级市场。能买卖 ETF 的做市商与 AP 是不同角色，同一机构可以兼具两种角色，也可以通过 AP 完成申赎。[SEC：ETF 投资者公告，2023][ref-etf-sec]

当 ETF 相对可交付篮子偏贵时，套利组合是买入篮子、卖出 ETF，再通过申购获得 ETF 份额用于交付或归还借券；当 ETF 偏便宜时，买入 ETF、卖出或对冲相应篮子，再通过赎回取得底层资产。实际流程受产品的现金替代、申赎单位、截止时间和结算安排影响。

这使 ETF 套利包含两个时间尺度：交易所上的报价、成交和风险对冲可以很快，一级市场的申赎与结算按业务时点执行。Jane Street 的公开报告描述了信用 ETF 做市中申赎、暂存库存、交易债券，以及使用其他 ETF、信用指数或期货对冲的选择；只有部分二级市场成交最终引发申赎。[Jane Street，2019][ref-js-credit]

设 $N$ 是一组可申赎 ETF 份额的数量，$V_{\text{basket}}^{\text{buy}}$ 是取得该组申购所需证券篮子的买入成本加申购现金差额，$C$ 汇总尚未包含在篮子成本中的申赎、借券、融资与结算费用。溢价套利的可执行条件是：

$$
N P_{\text{ETF}}^{\text{bid}}
-V_{\text{basket}}^{\text{buy}}-C>0.
$$

折价套利则反过来比较赎回所得篮子的可卖出金额与 ETF 买入成本。两边都按目标数量和实际买卖方向计算，不能把买入篮子与卖出篮子统一成一个中间价净值。

债券 ETF 尤其能体现估值与执行的差异。底层债券成交稀疏时，净值依赖的估值可能没有立即反映最新可成交价格；跨时区 ETF 的底层市场也可能已经闭市。这时 ETF 相对账面净值的折溢价，需要结合可用对冲工具和底层资产的实际流动性解释。

资本和运营能力决定套利区间有多宽：能否借到 ETF，能否取得篮子中的稀缺资产，是否有足够头寸跨越结算时间，都会改变成本。基金份额看起来只偏离很小，整组篮子的买卖价差与现金替代费用却可能已经超过该偏离。

## 5. 跨市场、三角套利与延迟套利

同一可互换资产在市场 A 的卖价低于市场 B 的买价时，可以买 A、卖 B。对给定数量 $Q$，先把价格、汇率和合约单位统一，机会的净值为：

$$
\text{Edge}(Q)=
\text{B 市场卖出所得}(Q)
-\text{A 市场买入成本}(Q)
-\text{费用与再平衡成本}(Q).
$$

两笔主动订单不是跨交易所的原子事务。A 成交、B 拒绝或只成交一部分之后，组合就有方向敞口。有效的执行预算因此还包括：完成第二腿的最坏可接受价格、最大未对冲数量，以及超过等待时限后的处理方式。

快速跨市场交易往往依靠预先分布的现金、资产或授信，而非第一腿成交后才把资产转到另一市场。后续再平衡有单独的时间与成本。Makarov–Schoar 对加密资产市场的研究显示，跨交易场所、尤其跨国家市场的价格分割，可以与资金流动和套利资本限制并存；价差持续存在时，要找出限制资金循环的条件。[Makarov–Schoar，2020][ref-crypto-arb]

三角套利把两条腿扩展为货币兑换闭环。为避免汇率报价方向混淆，令 $k_{A\to B}$ 表示卖出一单位 A 后，实际可收到多少 B，且已经使用正确的买卖价并扣除相应交易费用。闭环条件为：

$$
k_{A\to B}\,k_{B\to C}\,k_{C\to A}>1.
$$

实际可交易量取决于三段深度、数量换算和最小下单单位。把三种中间汇率直接相乘，会把不可成交的理论偏离算成机会；完成第一段之后，后两段的报价也可能变化。外汇三角套利研究把可成交性与执行延迟作为判断机会的重要条件。[Fenn 等，2009][ref-triangle]

延迟套利关注的是报价失效与更新之间的时间差。例如相关期货已经上涨，ETF 的旧卖价尚未撤回，交易者尝试买入该报价，并用期货对冲。旧报价的提供者同时尝试撤单，双方竞争的是交易所实际处理先后。无论哪方先发出请求，只有撮合或撤单生效才决定结果。

Aquilina、Budish、O’Neill 使用伦敦证券交易所 FTSE 350 股票在 2015 年 8—10 月、剔除损坏数据后 43 个交易日的消息数据，识别了包含失败撤单和失败成交请求的竞速。其报告中的 5—10 微秒是竞速时长的众数，按交易所网络入口处的请求时间戳，计算第一条成功消息与第一条失败消息之间的间隔，不能拿来充当策略系统的端到端延迟指标。[FCA，2020][ref-race]

这类证据解释了为什么逐笔成交记录仍不足以研究速度优势：失败的参与者没有成交，普通成交数据看不到他们。对策略而言，有意义的比较是“多少机会被成功捕获、多少失效报价被成功撤回，以及这些结果如何改变净收益”。

加密资产市场还常见买现货、卖永续合约的资金费率交易。永续合约没有固定到期日，通过定期资金支付约束其与现货的价格偏离；在正资金费率下，多头向空头支付，负费率则相反。对固定数量的线性合约组合，基差定义为永续价格减现货价格，总价格损益等于匹配数量乘以“入场基差减退出基差”，再加实际收到的资金费、减融资与交易成本。未来费率与退出基差都会变化，因此单次费率的年化外推并非锁定收益。这里的高频环节是监测、执行和对冲，收取资金费的持仓可以持续多日。[Coinbase Institutional，2024][ref-funding]

## 6. 统计套利与领先—滞后：什么关系值得交易

统计套利寻找相关资产的暂时偏离。例如股票与行业组合、两只相近 ETF、相关期货合约之间，先消除共同风险，再判断剩余偏离是否会收敛。Avellaneda–Lee 用主成分和行业 ETF 提取美国股票的共同因子，对残差建立均值回归模型；其公开回测使用日频数据，因此更适合提供建模思路，而非证明同样参数在高频上有效。[Avellaneda–Lee，2010；公开稿 2009][ref-stat]

为把对冲数量讲清楚，先看一个固定比例的价格组合：

$$
X_t=P^A_t-\beta P^B_t-c_t,\qquad
Z_t=\frac{X_t}{\widehat\sigma_{X,t}}.
$$

这里价格已换算成同一货币与合约单位，$\beta$ 是组合中 B 相对 A 的持仓数量比例，$c_t$ 是参考价差，$\widehat\sigma_{X,t}$ 是过去数据估计的残差波动尺度。当 $Z_t$ 高于进入阈值，模型认为 A 相对偏贵，于是卖 A、买 $\beta$ 单位 B，等待残差下降；低于负阈值则反向交易。

这个数量比例与“对收益率回归得到的 Beta”不是同一个单位。如果回归系数表达的是资金敞口比例，买入 $q_A$ 单位 A 后，对冲数量按 $q_B=\beta q_AP_A/P_B$ 换算。直接把收益率 Beta 当作股数比例，在两只资产价格差异较大时会留下明显敞口。

高频版本最依赖两个条件：偏离足够大，可以覆盖两腿进出成本；收敛足够快，策略能在有限库存与资金预算内等待。退出规则除了残差回到目标，还包括持仓超时、共同关系改变和风险限额触发。若公司消息改变了 A 的独立价值，残差偏离可能是新均衡，此时继续加仓会把均值回归策略变成不断扩大敞口的方向交易。

领先—滞后策略则研究谁先反映信息。例如指数期货先变动，ETF 或一组成分股稍后反应，策略用先到的信息更新后者的短期合理价格。Budish–Cramton–Shim 对 E-mini S&P 500 期货与 SPY 的研究，展示了紧密相关的资产在极短时间尺度上的价格不同步，以及速度竞争对机会持续时间的影响。[Budish 等，2015][ref-bcs]

领先关系的验证必须使用策略实际可见的时间。若期货数据按交易所时间排序，ETF 数据却按本地接收时间排序，测得的领先很可能包含时钟和传输差异。可用信号应满足：相关市场的信息已经到达本地，目标市场的价格仍可成交，且订单抵达时预期收益仍然超过成本。这也把统计预测与上一节的报价竞速连接起来：预测可以告诉策略价格将如何变化，执行决定还能否在旧价格附近建仓。

## 7. 订单簿与订单流：从预测指标到交易动作

限价订单簿（Limit Order Book，LOB）保存各价格上的买卖挂单。短期策略从中提取供需状态、队列耗尽速度、撤单变化、主动买卖方向及相关市场信息，再预测未来价格或成交质量。其中几个常用指标虽然都描述盘口，预测对象却不同。

设最优买卖价分别为 $b_t,a_t$，对应显示数量为 $Q_t^b,Q_t^a$，中间价 $m_t=(a_t+b_t)/2$。队列不平衡为：

$$
I_t=\frac{Q_t^b-Q_t^a}{Q_t^b+Q_t^a}.
$$

买方显示量较多、卖方较少时，卖方队列先耗尽与下一次中间价上跳的条件概率会发生变化。Gould–Bonart 使用 2014 年 Nasdaq 的 10 只股票研究这一关系，发现相对无条件方向概率的预测改善，在大 tick 股票中更明显。这里的大 tick 指最小报价单位相对典型价差较大、价差经常被一个 tick 约束；研究标签是下一次中间价变动的方向。[Gould–Bonart，2016][ref-imbalance]

订单流不平衡（Order Flow Imbalance，OFI）则统计一段窗口内，最优买卖档的新增、撤销与成交带来的净供需变化。Cont–Kukanov–Stoikov 在 2010 年 4 月的 50 只美国股票样本中，研究了如下短窗口关系：

$$
\Delta m_k\approx\beta_k\operatorname{OFI}_k+\varepsilon_k,
\qquad \beta_k\text{ 随盘口深度增大而减小}.
$$

这描述同一窗口内的订单流与价格冲击。若要做预测，训练标签要放在特征窗口之后；否则模型只是用刚发生的订单流解释刚发生的价格变化。[Cont 等，2014][ref-ofi]

另一个常用基线是加权中间价：

$$
m_t^w=
\frac{a_tQ_t^b+b_tQ_t^a}{Q_t^b+Q_t^a}
=m_t+\frac{a_t-b_t}{2}I_t.
$$

这个式子在买方队列较厚时向卖价偏移。Stoikov 的 micro-price 则利用盘口状态转移，估计未来中间价的条件期望修正；它与上面的线性加权中间价分别定义、分别评估。微价格模型可以把不平衡和价差状态转换成参考价值，但其中的修正函数仍由数据估计。[Stoikov：作者讲义与期刊摘要][ref-micro]

这些指标要转成交易，还差一次成本比较。设模型预测未来 $h$ 时间的中间价变化为 $\widehat\alpha_t$，当前价差为 $s_t=a_t-b_t$。对一个小数量主动买单，可将入场条件组织为：

$$
\widehat\alpha_t>
\frac{s_t}{2}
+\text{预期退出成本}
+\text{进出手续费}
+\text{延迟滑点}
+\text{承担风险所要求的补偿}.
$$

这是从主动买入现金流推导的阈值；如果价格预测已经直接针对未来可卖出的买价，就不再重复扣对应的退出价差。一次上涨方向预测即使经常正确，平均涨幅若不足以覆盖往返成本，也没有交易价值。

同一个信号还可以用于做市：预期上涨时提高报价中心、减少低价卖出量，或撤掉已失效的卖单。这些动作分别改变成交量、成交价格和风险暴露。比较模型时，可以从相同固定报价策略开始，只改变一个预测输入，观察成交条件下的 markout、净收益和库存尾部如何变化，避免把执行策略变化全部归功于预测模型。

## 8. 期权做市、波动率交易与合成关系

期权做市同时面对多个执行价和到期日。一张合约成交后，改变的不只是该合约库存，还包括整个组合对标的价格和波动率的敏感度。常用的风险量包括：Delta，即期权价格对标的价格的变化率；Gamma，即 Delta 随标的价格的变化率；Vega，即期权价格对波动率的敏感度。报价系统把这些风险按合约乘数汇总，再确定买卖倾向和对冲量。

例如做市商卖出看涨期权后，组合增加负 Delta，买入标的可以抵消当前的一阶方向敞口。标的随后变化时，Gamma 会让 Delta 改变，因而要再次调整对冲。剩余风险还包括波动率变化、离散对冲误差和价格跳跃。Baldacci、Bergault、Guéant 在连续 Delta 对冲和常数 Vega 近似下，把多期权做市降为较低维的组合风险控制问题，为“用风险因子而非每张合约独立管理库存”提供了模型实例。[Baldacci 等，2019/2020 预印本][ref-options]

实际交易通常先拟合波动率曲面，即不同执行价和到期日对应的隐含波动率，再根据市场价格、组合风险、预期对冲成本与成交质量形成报价。波动率相对价值交易可以表现为买入模型认为便宜的波动率、卖出较贵的波动率，并对冲部分方向风险。其损益依赖实际价格路径与波动率变化；Delta 中性只描述当前一阶敞口，交易仍然承担其他风险。

期权还存在更直接的复制关系。对同一标的、执行价 $K$、到期日 $T$ 的欧式期权，在无分红、恒定连续复利利率 $r$ 且可融资与卖空的模型中，看涨价格 $C_t$ 与看跌价格 $P_t$ 满足：

$$
C_t-P_t=S_t-Ke^{-r(T-t)}.
$$

左边是买入看涨、卖出看跌的合成远期敞口，右边对应现货与融资的组合。偏离要用各腿正确的买卖价、借券与融资条件计算，行权方式、分红或结算规则改变后也要改写现金流。[CME：期权工作坊的平价关系][ref-parity]

箱式价差进一步把两组不同执行价的合成头寸组合起来。在现金流和行权安排匹配的欧式结构中，到期支付固定的执行价差，因此可以用来比较隐含融资利率。CME 的材料重点讨论其融资、抵押品和行权结算安排；这类头寸即使由高频系统发现和执行，经济本质也可能是一笔跨期限融资交易。[CME：期权箱式价差融资][ref-box]

期权高频做市的工程压力来自相关报价的大范围失效。标的快速变化时，大量执行价和到期日的旧报价同时变得不合适；只按单张订单控制数量，会漏掉并发成交形成的组合风险。Optiver 公开讨论了交易所撮合引擎内的做市商保护机制：累计成交量或指定风险指标触发阈值后，系统撤回保护范围内的报价。[Optiver，2023][ref-protect]

跨产品管理也有公开实践依据。Optiver 在 2021 年现金股票业务介绍中说明，其中央风险簿汇总了期权、期货、现金股票及交易所交易产品做市单元的风险。这支持按组合看待库存与对冲的做法；该材料没有披露它使用哪一种学术报价算法。[Optiver，2021][ref-central]

## 9. 新闻事件与集合竞价

新闻策略的输入包括宏观数据、公司公告和机器可读新闻。交易逻辑可以围绕“公布值相对事前预期的差异”，也可以围绕文本中的实体、事件与方向展开。Foucault、Hombert、Roşu 的《News Trading and Speed》在理论模型中分别研究信息更准确和对公共新闻反应更快的价值，说明预测质量与速度可以通过不同机制影响下单。[2013 工作稿；期刊版发表于 2016][ref-news]

一个可检验的研究流程是：记录消息真实到达时间，提取预先定义的事件字段，估计相关资产的价格响应，再在订单到达时的盘口上计算可成交收益。宏观数据的预期值要来自公布之前；新闻后的修正值不能回填成当时已知的数字。重复消息、否定句、实体重名和不同时间戳语义，会直接改变买卖方向或制造虚假的速度优势。

事件发生后，点差与深度也会变化。方向预测正确，但买入时已经支付过大的跳价，仍会亏损。对做市策略而言，同一事件信号也可以先用于减少报价数量、更新参考价或撤回旧报价，再考虑是否主动交易。

集合竞价将一段时间累积的订单集中确定成交价格。Nasdaq 的净订单不平衡指标（Net Order Imbalance Indicator，NOII）公开不平衡方向、数量和指示清算价格等信息，用于观察竞价供需。指示价格会随着新订单进入而变化。[Nasdaq：收盘集合竞价 FAQ][ref-auction]

围绕这些信息，可以研究最终竞价价的预测，比较其与连续市场的可成交价格，或在竞价中承接另一方的流动性需求。这里的约束是竞价订单类型、截止时间、撤改单规则、实际获配数量及收盘后的库存。买方不平衡大，并不自动意味着跟随买入有利可图：新增卖单会响应不平衡，最终清算价格也随之改变。

新闻与竞价策略都具有明确的事件窗口，但机会不一定全天连续发生。评价时按事件和交易日统计结果，比把一个公告周围的高成交频率外推为全天稳定能力更合理。

## 10. 执行算法在策略中的位置

时间加权平均价（Time-Weighted Average Price，TWAP）与成交量加权平均价（Volume-Weighted Average Price，VWAP）是执行基准，也常用于命名相应拆单算法。TWAP 类算法按时间安排数量；VWAP 类算法参照市场成交量曲线安排数量。它们通常接收已经确定的买卖方向和总量，再解决如何完成交易。

Almgren–Chriss 把固定持仓在给定期限内清算，写成预期成本与成本方差的权衡：

$$
\min_x\left\{\mathbb E[C(x)]
+\lambda\operatorname{Var}[C(x)]\right\},
\qquad x_0=X,\quad x_T=0.
$$

$x$ 是剩余持仓轨迹，$C$ 是相对初始参考价的执行成本，$\lambda$ 表示对成本风险的厌恶。更快卖出可能增加冲击，更慢卖出则承担更久的价格变化。该框架回答给定交易目标后的执行取舍。[Almgren–Chriss，2000/2001][ref-ac]

高频预测可以改善子单的时机和主动、被动选择；跨市场路由可以改善成交场所选择。它们属于策略实现的重要部分，但判断某套系统的收益来源时，仍要回到原始交易目标：是提供流动性、取得复制价差、预测价格，还是降低一笔已有交易的成本。机器学习和强化学习也处于这一层次——它们可以估计价格、成交或控制动作，具体收益机制仍由交易目标决定。

## 11. 如何验证策略的收益来源

一份有效的策略结果，应能从净收益追溯到成交，再从成交追溯到当时可见的信息、订单位置和执行动作。不同策略的主要验证对象不同。

| 策略 | 最小研究对象 | 关键对照 | 主要评价量 |
|---|---|---|---|
| 期现、ETF 套利 | 可成交篮子、期货或 ETF、融资与申赎现金流 | 中间价信号与完整复制成本 | 完整组合净收益、未完成腿敞口、资金占用 |
| 跨期套利 | 两个月份及原生价差簿 | 原生组合执行与拆腿执行 | 净价差收益、修复成本、期限风险 |
| 高频做市 | 逐笔盘口、队列、委托和成交回报 | 固定报价、库存偏移、加入预测 | 条件 markout、净收益、库存分布与尾部损失 |
| 短期预测 | 本地可见特征与未来标签 | 无条件预测、简单线性模型、复杂模型 | 成交后的净收益、信号衰减、容量 |
| 跨市场与延迟套利 | 多场所时间线、成功与失败请求 | 观察到的机会与真正完成的组合 | 捕获率、单腿亏损、再平衡成本 |
| 期权策略 | 合约簿、波动率曲面、风险敞口和对冲成交 | 各对冲频率和组合限额 | 对冲后损益、Greeks 暴露、跳跃损失 |

**第一处关键误差是成交模型。** 历史成交触及自己的挂单价时，前方队列可能尚未耗尽；盘口数量减少也可能是自己后方的订单撤销。只有价位聚合数据时，前后位置要靠模型估计。HftBacktest 文档提供保守和概率队列模型，并明确说明历史回放不会因自己的订单而改变后续市场，因而大规模主动成交和市场冲击需另行处理。[HftBacktest：Order Fill，py-v2.1.0][ref-fill]

**第二处是策略知道信息的时刻。** 行情送达、订单进入交易所和响应返回是不同延迟。订单已经成交、本地回报尚未到达时，真实库存与策略已知库存不同；撤单已经发出，也仍可能输给到达更早的主动订单。模拟器如果提前更新状态，会让策略使用未来信息。[HftBacktest：Latency Models][ref-latency]

**第三处是净收益的账。** 每条腿分别记录数量、实际成交价格、手续费和返佣，再加入融资、借券、申赎及资金支付。期末未平仓按一致价格估值，并另外报告实际清算成本。价差收益、逆向选择和滑点用于解释损益；若某项影响已经体现在实际成交价格中，就不能作为成本再扣一次。

评估预测时，按时间先后训练和验证，并保留波动、流动性及事件状态的差异。标签跨越验证边界时，避免同一段未来价格同时进入训练和测试。研究大量信号和参数之后，再保留独立的样本外区间，才能判断收益是否依赖反复挑选参数。

容量与风险也要从组合层面衡量。增加挂单数量可能降低成交质量，扩大套利规模可能吃掉全部价差，多个看似独立的策略可能同时消耗同一笔现金或对冲深度。报告平均净收益的同时，保留部分成交后的最大敞口、资金峰值、库存尾部、最差事件与按交易日计算的回撤。

对工程学习，可从单品种固定做市基线开始，逐步加入库存偏移、队列与信号；再研究双合约价差，加入部分成交和对冲；最后扩展到篮子、多场所与期权组合。这样每增加一项模型或基础设施，都能与具体成交变化对应。仓库中的[高频回测章节](HFT_Engineering_09_高频回测.md)给出了双时间线、队列与在途撤单的进一步说明。

## 12. 参考资料与阅读入口

优先精读 AS 与 GLFT 的库存模型、Cont 等人的订单流定义、Gould–Bonart 的预测标签，以及 Fenn 等人对套利成交失败的处理；再结合目标交易所的产品与撮合机制。以下资料分别支持正文中的模型、实验结论或业务机制；机构业务介绍只用于其公开说明的范围。

**论文与作者材料**

1. Marco Avellaneda、Sasha Stoikov，2008，*High-frequency trading in a limit order book*，Quantitative Finance 8(3)：217–224。[公开全文][ref-as]。库存保留价和近似报价公式。
2. Olivier Guéant、Charles-Albert Lehalle、Joaquin Fernandez-Tapia，2013，*Dealing with the inventory risk: A solution to the market making problem*，Mathematics and Financial Economics 7：477–507；预印本初稿 2011。[公开全文][ref-glft]。库存边界、模型求解与长时域近似。
3. Álvaro Cartea、Sebastian Jaimungal、Jason Ricci，2014，*Buy Low, Sell High: A High Frequency Trading Perspective*，SIAM Journal on Financial Mathematics 5(1)：415–444。[期刊摘要][ref-cjr]。摘要支持订单流与短期信号机制。
4. Ciamac C. Moallemi、Kai Yuan，*A Model for Queue Position Valuation in a Limit Order Book*，2017 年 6 月修订稿。[作者全文][ref-queue]。排队位置与动态订单价值。
5. Rama Cont、Arseniy Kukanov、Sasha Stoikov，2014，*The Price Impact of Order Book Events*，Journal of Financial Econometrics 12(1)：47–88。[预印本全文][ref-ofi]。OFI 的定义、深度与同窗口价格冲击。
6. Martin D. Gould、Julius Bonart，2016，*Queue Imbalance as a One-Tick-Ahead Price Predictor in a Limit Order Book*，Market Microstructure and Liquidity 2(2)。[预印本全文][ref-imbalance]。队列不平衡与下一次中间价变化。
7. Sasha Stoikov，2018，*The micro-price: a high-frequency estimator of future prices*，Quantitative Finance 18(12)：1959–1966。[期刊摘要](https://www.tandfonline.com/doi/abs/10.1080/14697688.2018.1489139)、[作者讲义][ref-micro]。定义依据作者讲义，期刊页面读取摘要。
8. Marco Avellaneda、Jeong-Hyun Lee，2010，*Statistical arbitrage in the US equities market*，Quantitative Finance 10(7)：761–782。[2009 年公开稿][ref-stat]。共同因子、残差与日频均值回归。
9. Eric Budish、Peter Cramton、John Shim，2015，*The High-Frequency Trading Arms Race: Frequent Batch Auctions as a Market Design Response*，Quarterly Journal of Economics 130(4)：1547–1621。[作者公开全文][ref-bcs]。跨产品短时不同步与竞速机制。
10. Matteo Aquilina、Eric Budish、Peter O’Neill，2020，*Quantifying the High-Frequency Trading “Arms Race”*，FCA Occasional Paper 50。[FCA 全文][ref-race]。含失败消息的竞速数据；正文数字采用此版本。
11. Daniel J. Fenn、Sam D. Howison、Mark McDonald、Stacy Williams、Neil F. Johnson，2009，*The Mirage of Triangular Arbitrage in the Spot Foreign Exchange Market*，International Journal of Theoretical and Applied Finance 12(8)。[作者公开全文][ref-triangle]。可执行汇率与多腿成交风险。
12. Igor Makarov、Antoinette Schoar，2020，*Trading and arbitrage in cryptocurrency markets*，Journal of Financial Economics 135(2)：293–319。[MIT 存档与摘要][ref-crypto-arb]。所引市场分割和资本约束结论来自已读取的摘要。
13. Bastien Baldacci、Philippe Bergault、Olivier Guéant，*Algorithmic market making for options*，2019 年预印本、2020 年修订。[公开全文][ref-options]。Delta 对冲、Vega 近似与组合维度约简。
14. Thierry Foucault、Johan Hombert、Ioanid Roşu，*News Trading and Speed*，期刊版 2016，Journal of Finance 71(1)。[2013 年工作稿][ref-news]。所引速度和信息质量模型来自该工作稿。
15. Robert Almgren、Neil Chriss，*Optimal Execution of Portfolio Transactions*，2000 年稿、2001 年发表于 Journal of Risk 3(2)：5–39。[论文全文镜像][ref-ac]。固定清算目标下的冲击与风险取舍。

**交易所与监管机构资料**

16. SEC，2014，*Equity Market Structure Literature Review, Part II: High Frequency Trading*。[文献综述全文][ref-sec]。策略分类及不同研究的识别范围。
17. CME Group，*Calculating Fair Value*。[合理价值计算][ref-carry]。股指期现的融资与分红关系。
18. CME Group，*Performance Bonds/Margins FAQ*。[保证金 FAQ][ref-margin]。履约保证金及逐日盯市。
19. CME Group，*Futures Spread Overview*。[价差概览][ref-calendar]。跨期、跨品种与展期机制。
20. CME Group，*Frequently Asked Questions: CME FX Futures Calendar Spreads*。[日历价差 FAQ][ref-calendar-order]。原生跨期订单的两腿执行。
21. CME Group，*What is Contango and Backwardation*。[期限结构说明][ref-curve]。仓储、融资与便利收益。
22. SEC，2023，*Updated Investor Bulletin: Exchange-Traded Funds (ETFs)*。[ETF 公告][ref-etf-sec]。美国注册 ETF 的 AP 与申赎机制。
23. CME Group，2023，*CME Options Workshop — Week 5: Black-Scholes*。[讲义][ref-parity]。第 11–13 页给出复制和平价关系。
24. CME Group，2024，*Index Options Box Spreads as Financing Tool*。[箱式价差说明][ref-box]。固定支付、隐含融资与行权安排。
25. Nasdaq，*Nasdaq Closing Cross Frequently Asked Questions*。[收盘竞价 FAQ][ref-auction]。NOII 和指示价格；具体下单时限以所研究日期的规则版本为输入。

**机构公开分享与开源技术资料**

26. Jane Street，2019，*Credit ETF Trading in Stressed Markets*。[机构报告，Center for Financial Stability 托管][ref-js-credit]。ETF 做市、库存、申赎与替代对冲。
27. Optiver，2023，*Market-maker protections*。[机构文章][ref-protect]。按成交量与风险敞口触发的报价保护。
28. Optiver，2021，*Optiver launches cash equity offering for institutional investors*。[业务介绍][ref-central]。跨产品中央风险簿。
29. David Han、David Duong，Coinbase Institutional，2024，*A Primer on Perpetual Futures*。[研究文章][ref-funding]。永续合约和资金支付机制。
30. HftBacktest，*Order Fill*，Python 文档版本 py-v2.1.0。[技术文档][ref-fill]。队列估计、部分成交与回放不含市场反应的限制。
31. HftBacktest，*Latency Models*。[技术文档][ref-latency]。行情、订单进入与响应延迟。

[ref-sec]: https://www.sec.gov/files/marketstructure/research/hft_lit_review_march_2014.pdf
[ref-carry]: https://www.cmegroup.com/trading/equity-index/fairvalue.html
[ref-margin]: https://www.cmegroup.com/solutions/risk-management/performance-bonds-margins/faq-performance-bonds-margins.html
[ref-calendar]: https://www.cmegroup.com/education/courses/understanding-futures-spreads/futures-spread-overview
[ref-calendar-order]: https://www.cmegroup.com/articles/faqs/frequently-asked-questions-cme-fx-futures-calendar-spreads.html
[ref-curve]: https://www.cmegroup.com/education/courses/introduction-to-precious-metals/what-is-contango-and-backwardation
[ref-as]: https://math.nyu.edu/inmemoriam/avellaneda/HighFrequencyTrading.pdf
[ref-glft]: https://arxiv.org/pdf/1105.3115
[ref-cjr]: https://epubs.siam.org/doi/abs/10.1137/130911196
[ref-queue]: https://moallemi.com/ciamac/papers/queue-value-2016.pdf
[ref-etf-sec]: https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-24
[ref-js-credit]: https://centerforfinancialstability.org/etfs/ETFAnalysis/credit-etf-trading-in-stressed-markets-jane-street-20190723.pdf
[ref-crypto-arb]: https://dspace.mit.edu/entities/publication/7f91bfb5-ba77-4d0e-9c79-ec75e104e6cc
[ref-triangle]: https://people.maths.ox.ac.uk/howison/papers/fenn_fx_arbitrage.pdf
[ref-race]: https://www.fca.org.uk/publication/occasional-papers/occasional-paper-50.pdf
[ref-funding]: https://www.coinbase.com/institutional/research-insights/research/market-intelligence/a-primer-on-perpetual-futures
[ref-stat]: https://math.nyu.edu/faculty/avellane/AvellanedaLeeStatArb20090616.pdf
[ref-bcs]: https://www.econ.umd.edu/sites/www.econ.umd.edu/files/pubs/budish-cramton-shim-hft-frequent-batch-auctions.pdf
[ref-imbalance]: https://arxiv.org/pdf/1512.03492
[ref-ofi]: https://arxiv.org/pdf/1011.6402
[ref-micro]: https://www.ma.imperial.ac.uk/~ajacquie/Gatheral60/Slides/Gatheral60%20-%20Stoikov.pdf
[ref-options]: https://arxiv.org/pdf/1907.12433
[ref-parity]: https://www.cmegroup.com/articles/files/2023/pro-workshop-series-post-webinar-recording-week5-slides.pdf
[ref-box]: https://www.cmegroup.com/articles/2024/index-options-box-spreads-as-financing-tool.html
[ref-protect]: https://www.optiver.com/insights/markets/market-maker-protections/
[ref-central]: https://www.optiver.com/insights/news/optiver-launches-cash-equity-offering-for-institutional-investors/
[ref-news]: https://blog.iese.edu/financeseminars/files/2013/04/Foucault.pdf
[ref-auction]: https://www.nasdaqtrader.com/content/productsservices/Trading/ClosingCrossfaq.pdf
[ref-ac]: https://www.smallake.kr/wp-content/uploads/2016/03/optliq.pdf
[ref-fill]: https://hftbacktest.readthedocs.io/en/py-v2.1.0/order_fill.html
[ref-latency]: https://hft.readthedocs.io/en/latest/latency_models.html
