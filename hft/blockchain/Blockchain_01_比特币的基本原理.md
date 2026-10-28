# 彻底理解加密货币系统原理（一）：Bitcoin 是怎样运行的

在银行 App 里给别人转账，最直观的结果是双方余额发生变化。支撑这件事的是银行维护的账本：谁付了多少，谁收到了多少。Bitcoin 要做的也是记账，难点在于它没有银行。

在这个场景里面，我们可以先问一个更朴素的问题：一个账本系统必须解决哪些问题？银行怎样解决它们？如果不要银行，哪些问题仍能解决，哪些会卡住？区块链正是为卡住的那部分设计的。概念讲清以后，再用伪代码搭一个Toy BlockChain来彻底理解bitcoin的核心机制。

全文的顺序如下：

1. 账本要解决的五个问题
2. 中心化账本：把问题交给一个可信机构
3. 去中心化账本：卡在交易顺序上
4. 区块链：让所有节点对同一份历史达成一致
5. 用伪代码搭一个Toy BlockChain
6. Bitcoin 的真实实现：签名与地址、UTXO、Merkle 树、候选区块、工作量证明、P2P 传播、分叉处理、SPV 和确认风险
7. 端到端：用时序图和伪代码串起花掉一个 UTXO 的完整流程

## 账本要回答的五个问题

先放下加密货币。几个朋友经常互相垫付饭钱，与其每次都转账，不如维护一份公共账本，把每笔付款记下来，月底统一结算。3Blue1Brown 讲解 Bitcoin 时就从这样一份账本讲起：账本只是一串交易记录，每个人的余额由这些记录推算出来。[3Blue1Brown：Bitcoin 是怎样运行的](https://www.3blue1brown.com/lessons/bitcoin)

```text
#1  Alice 付给 Bob    20
#2  Bob   付给 Carol  40
#3  Carol 付给 Alice  30
```

记录只有几行，可一旦谁都能往里写，问题就来了。

| 问题 | 要回答的事 | 出错时的样子 |
| --- | --- | --- |
| 授权 | 这笔付款是否经过付款人同意 | Bob 自己写下“Alice 付给 Bob 100” |
| 余额 | 付款人是否真有这么多钱 | Carol 的钱已经花光，还在继续付款 |
| 顺序 | 两笔交易冲突时，哪一笔算数 | Alice 用同一笔钱同时付给 Bob 和 Carol |
| 不可改 | 已经记下的记录能否被悄悄修改 | Bob 交货后，那笔付款记录被删掉了 |
| 发行 | 新钱从哪里来，按什么规则增加 | 有人随手给自己记一笔收入 |

前两个问题只看单笔交易；顺序问题要在多笔交易之间比较；不可改问题涉及时间，今天确认的历史明天是否仍然算数；发行问题决定账本里的总额怎样变化。

第三个问题对数字货币格外棘手。一张纸币不能同时交到两个人手里，一条数字记录却可以几乎零成本地复制、分别发给两个人。用同一笔钱付两次，叫作**双重支付（double spending）**。Michael Nielsen 在解释 Bitcoin 协议时，把它和伪造并列为“用信息当钱”首先要克服的两个问题。[How the Bitcoin protocol actually works](https://michaelnielsen.org/ddi/how-the-bitcoin-protocol-actually-works/)

## 中心化账本：把问题交给一个可信机构

银行的做法是自己维护唯一的权威账本，所有转账都要经过它。

| 问题 | 银行的做法 |
| --- | --- |
| 授权 | 账户密码、短信验证、柜面核验身份 |
| 余额 | 数据库保存每个账户的余额，扣款前检查 |
| 顺序 | 所有请求进入同一个系统，按处理先后排定；冲突的第二笔因余额不足被拒绝 |
| 不可改 | 内部审计、监管检查、多地备份 |
| 发行 | 由央行发行货币，商业银行在既定框架内记账 |

这套方案很高效，顺序问题几乎是顺带解决的：只有一份账本、一个记账人，谁先谁后由它说了算。

代价是信任。所有人都要相信银行记账准确、系统不宕机、员工不舞弊；银行还能冻结账户，包括依政府要求冻结。[Rutgers 讲义：Bitcoin](https://people.cs.rutgers.edu/~pxk/classes/419/notes/bitcoin.html) 中本聪在论文引言中写道，金融机构无法回避纠纷调解，交易因而可以撤销；调解成本推高了交易费用，商户也要为防范欺诈向顾客索取更多信息。[Bitcoin 白皮书](https://bitcoin.org/bitcoin.pdf)

早期的数字现金方案也走过这条路：设立一个可信的铸币方，由它检查每笔交易是否Double Spending，每次交易后把币交回铸币方换新。论文的评价是，整个货币系统的命运系于运营铸币方的公司，每笔交易都得经过它，和银行没有区别。[Bitcoin 白皮书](https://bitcoin.org/bitcoin.pdf)

于是问题变成：能不能不要这个中心？

## 去中心化账本：卡在交易顺序上

最直接的办法是取消中心账本，让每个参与者都保存一份完整副本。要付款时，把交易广播出去，大家各自检查，再记入自己的账本。[3Blue1Brown：Bitcoin 是怎样运行的](https://www.3blue1brown.com/lessons/bitcoin)

在 Bitcoin 中，运行 Bitcoin 软件、参与网络通信的计算机叫作**节点**。其中**全节点**按完整规则检查账本内容，维护自己认可的账本状态。节点收到新数据后会检查并转发给邻居，记录由此在网络中逐跳传播。[Bitcoin.org：Bitcoin 如何运行](https://bitcoin.org/en/how-it-works)

没有了中心，把五个问题再过一遍。

**授权仍能解决。** 银行靠密码核验身份，去中心化账本靠数字签名。每个人持有一对密钥：私钥自己保管，用来签名；公钥公开，任何人都能用它核对签名。签名同时取决于私钥和被签署的内容，挪到另一笔交易上就无法通过验证。所以不需要银行，每个节点都能独立判断一笔付款是否经过付款人同意。不过，把整笔已签名的交易原样再提交一次，签名依然有效，交易还需要带上能区分彼此的唯一信息。Bitcoin 的 UTXO 模型顺带处理了这一点，后文展开。

**余额也能解决，代价是保存全部历史。** 没有银行替你记余额，节点就从第一笔记录开始重放全部交易，自己算出每个人有多少钱。验证一笔新交易，需要知道它之前的完整历史。[3Blue1Brown：Bitcoin 是怎样运行的](https://www.3blue1brown.com/lessons/bitcoin) 这也意味着，所谓拥有比特币，就是账本里有一些金额，而你掌握着支出它们所需的私钥。

**顺序卡住了。** 假设 Alice 只有一份钱，却签了两笔交易：tx1 付给 Bob，tx2 付给 Carol。她把 tx1 发给网络的一侧，把 tx2 发给另一侧。节点 1 先收到 tx1，签名和余额都没问题，记账；随后收到 tx2，发现钱已花掉，拒绝。节点 2 的经历正好相反。

![朴素的去中心化账本：Alice 同时发出两笔花费同一份钱的交易，先收到 tx1 的节点拒绝 tx2，先收到 tx2 的节点拒绝 tx1，账本分成两个版本](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/blockchain/assets/bitcoin-ledger-models.svg)

每个节点都严格执行了规则，结果却是两个版本的账本。签名只能证明两笔交易都经过 Alice 授权；余额检查只能在某个既定顺序下进行。二者都回答不了哪一笔在先。网络延迟本来就让各节点看到的到达顺序不同，Alice 还可以故意放大这种差异。

论文把这个问题说得很直接：要确认某笔交易之前没有冲突交易，唯一的办法是知道所有交易；没有可信方时，交易必须公开广播，参与者还需要一套机制，对交易先后形成同一份历史。[Bitcoin 白皮书](https://bitcoin.org/bitcoin.pdf)

**投票也不行。** 一个自然的想法是让节点投票，多数说哪笔先到就算哪笔。问题在于网络身份几乎没有成本。Alice 可以开出十亿个傀儡身份，分别告诉 Bob 和 Carol“你那笔已经被多数确认”。[How the Bitcoin protocol actually works](https://michaelnielsen.org/ddi/how-the-bitcoin-protocol-actually-works/) 论文也指出，若按“一个 IP 地址一票”计票，任何能分配大量 IP 的人都能操纵结果。[Bitcoin 白皮书](https://bitcoin.org/bitcoin.pdf) 投票权必须绑定在某种难以伪造的稀缺资源上。

**不可改和发行也失去了依托。** 每个人手里都有一份账本，任何人都能修改自己的副本，再声称它才是真的；没有审计方，就要让篡改历史在技术上能被发现，在经济上代价高昂。没有央行，新钱的发行规则只能写进所有节点共同执行的协议。

| 问题 | 中心化账本 | 去掉中心以后 | Bitcoin 的方案 |
| --- | --- | --- | --- |
| 授权 | 银行核验身份 | 可在本地解决 | 数字签名 |
| 余额 | 银行数据库 | 可在本地解决，但需要全部历史 | 全节点维护 UTXO 集合 |
| 顺序 | 银行串行处理 | 无法在本地解决；按身份投票会被傀儡身份操纵 | 区块、工作量证明、累计工作量最大的链 |
| 不可改 | 审计与监管 | 每份副本都可能被改写 | 哈希链加工作量证明，改写历史要重做计算 |
| 发行 | 央行 | 没有发行方 | 协议规定的区块补贴，每 210,000 块减半 |

## 区块链：让所有节点对同一份历史达成一致

问题集中到了一点：一群互不信任、身份可以伪造、收到消息的次序又各不相同的节点，怎样对交易顺序达成一致？Bitcoin 的答案可以拆成四步。[3Blue1Brown：Bitcoin 是怎样运行的](https://www.3blue1brown.com/lessons/bitcoin)、[Bitcoin 白皮书](https://bitcoin.org/bitcoin.pdf)

**第一步，按批排序，不按笔排序。** 不再为每笔交易单独达成一致，而是把一批交易装进一个**区块**。块内顺序由提出区块的人决定，网络只需对“下一块是哪一块”达成一致。到达时间不再参与排序，交易的先后取决于它所在的区块和块内位置。

**第二步，用哈希把区块串起来。** 哈希是对一段数据计算出的固定长度摘要：同样的数据总得到同样的摘要，内容稍有变化，摘要就几乎完全不同，可以把它当作数据的指纹。每个区块都保存前一个区块的哈希，沿着这些引用向前追溯，最终到达第一个区块，即创世区块。这种前后引用形成的数据结构，就是**区块链（blockchain）**。

![区块链基础：区块 B 保存 A 的区块头摘要，C 保存 B 的区块头摘要；每个区块收录一批交易，链随新区块加入而增长](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/blockchain/assets/bitcoin-blockchain-basics.svg)

如果有人修改 A 中的付款，A 的哈希随之改变，B 里保存的旧哈希就对不上了；他若连 B 的引用一起改，B 的哈希又会变化，继续影响 C。哈希链让篡改能被发现。反过来，接受一个区块也就接受了它之前的全部历史，因为它的哈希已经间接固定了前面的每一块。[区块链的数据结构](https://github.com/bitcoin-dot-org/developer.bitcoin.org/blob/master/devguide/block_chain.rst)

**第三步，让提出区块变得昂贵。** 哈希链只能发现篡改，阻止不了篡改：任何人都可以改完数据，再把后面所有哈希重算一遍，成本微乎其微。Bitcoin 因而要求每个区块附带**工作量证明（Proof of Work，PoW）**：找到一个数，使区块的哈希满足一个很难碰上的条件，比如前 30 位全为 0。哈希输出无法预测，除了逐个尝试别无他法；单次尝试命中的概率是 2³⁰ 分之一，约十亿分之一。验证却只需计算一次哈希。[3Blue1Brown：Bitcoin 是怎样运行的](https://www.3blue1brown.com/lessons/bitcoin)

这一步同时解决了两件事。其一是谁来提出下一块：所有愿意计算的人同时尝试，先找到的人获得提出权，相当于一次按算力加权的抽签，开再多傀儡身份也不会增加中签机会，论文称之为“一 CPU 一票”。其二是改写历史的成本：改动某个区块，就要重做它以及之后所有区块的工作量证明。[Bitcoin 白皮书](https://bitcoin.org/bitcoin.pdf)

**第四步，冲突时跟随累计工作量最大的链。** 两名矿工几乎同时找到区块时，网络会暂时出现两个分支。规则是：在所有有效的链中，跟随累计工作量最大的一条；难度不变时，它就是最长的链。新区块会接在某一条分支上，让它领先，其他节点随之切换过去。攻击者想用另一段历史取而代之，就要让自己的分支在累计工作量上反超；算力少于一半时只能靠运气，落后越多，机会越小。

剩下的问题是：为什么有人愿意花电费去算？每个区块的第一笔交易可以凭空给出块者一笔奖励，再加上块内交易的手续费。这同时解决了发行问题：新币按协议规定的节奏，通过区块奖励进入流通，不需要发行机构。负责组装区块、寻找工作量证明的参与者，叫作**矿工**。[Bitcoin 白皮书](https://bitcoin.org/bitcoin.pdf)

回到 Alice 的双花。tx1 和 tx2 都在网络里传播，矿工各自从中挑选。若某个矿工把 tx1 打包进区块，并率先找到工作量证明，其他节点验证该块后，会发现 Alice 那份钱已经花掉，tx2 就成了无效交易。若两个区块几乎同时出现，分别包含 tx1 和 tx2，分叉会在后续区块中分出胜负。

付款所在的区块被接到链上，付款就获得一次**确认**；此后每增加一个后继区块，确认数加一。Bob 等到包含 tx1 的区块后面又接上几个区块，这笔付款被推翻的可能性就已经很小了。这就是“等待确认”的含义，最后一节会算出具体概率。

## 用伪代码搭一个Toy BlockChain

先看中心化账本。只有一个记账人时，一次转账只要几行：

```text
balance = Map<Account, Amount>     # 银行数据库里的权威余额
log     = []                       # 银行自己保存的流水

function transfer(from, to, amount, credential):
    require authenticate(from, credential)   # 授权：查银行的密码库
    require balance[from] >= amount          # 余额：查银行说了算的状态
    balance[from] -= amount
    balance[to]   += amount
    log.append({from, to, amount})           # 不可改：相信银行不改流水

# 顺序：所有调用由银行串行执行，冲突的第二笔会因余额不足被拒绝
```

每一行都依赖银行：`authenticate` 查的是银行的密码库，`balance` 由银行说了算，串行执行靠的是银行的系统，`log` 是否可信取决于银行是否诚实。去掉银行，这些隐含保证要逐一换掉。

下面的Toy Model沿用账户余额，对整个区块计算哈希，用前导零的位数表示难度。`H` 表示一个密码学哈希函数，`||` 表示拼接，`require` 表示检查失败就拒绝，`Map` 中不存在的键读作 0。先定义交易和区块：

```text
Tx    = {from_key, to_key, amount, seq, signature}   # seq：付款人的第几笔交易
Block = {prev_hash, transactions, nonce}

DIFFICULTY = 30                    # 区块哈希的前 30 位必须为 0
REWARD     = 50                    # 每块的出块奖励

function block_hash(block):
    return H(block.prev_hash || serialize(block.transactions) || block.nonce)

function has_work(block):
    return leading_zero_bits(block_hash(block)) >= DIFFICULTY
```

账户直接用公钥表示。签名覆盖除签名之外的全部字段；`seq` 使同一付款人的每笔交易都不相同，原样重放旧交易会因序号不符被拒绝。Alice 的 tx1 和 tx2 若使用同一个序号，其中一笔上链后，另一笔会因序号不符失败；即使换成不同序号，余额检查也会拦下第二笔。

全节点不保存可信的余额表，而是从创世区块开始重放整条链。任何一步检查失败，整条链都无效：

```text
State = {balance: Map<Key, Amount>, next_seq: Map<Key, Int>}

function apply_block(state, block):
    s = copy(state)
    reward = block.transactions[0]               # 第一笔：出块奖励，没有付款人
    require reward.amount == REWARD
    s.balance[reward.to_key] += REWARD

    for tx in block.transactions[1:]:
        require verify(tx.from_key, digest(tx), tx.signature)   # 授权：用签名代替密码库
        require tx.seq == s.next_seq[tx.from_key]                # 防止重放
        require s.balance[tx.from_key] >= tx.amount              # 余额：在这条链的历史上检查
        s.balance[tx.from_key] -= tx.amount
        s.balance[tx.to_key]   += tx.amount
        s.next_seq[tx.from_key] += 1
    return s

function replay(chain):
    state = empty_state()
    for i in indices(chain):
        if i > 0:
            require chain[i].prev_hash == block_hash(chain[i - 1])   # 不可改：哈希链
        require has_work(chain[i])                                   # 不可改：工作量
        state = apply_block(state, chain[i])
    return state
```

余额检查仍然存在，只是检查的对象从“银行的数据库”换成了“这条链重放出来的状态”。节点的其余工作，是收交易、挑链和挖矿：

```text
my_chain = [GENESIS]
pending  = set()                   # 收到但尚未上链的交易

function total_work(chain):        # 难度固定，每块工作量相同，累计工作量与块数成正比
    return len(chain) * 2^DIFFICULTY

function on_transaction(tx):
    pending.add(tx)
    broadcast(tx)

function on_chain(candidate):      # 顺序：只认累计工作量最大的有效链
    if total_work(candidate) <= total_work(my_chain):
        return
    if try_replay(candidate) is invalid:   # 与 replay 相同，任一 require 失败时返回 invalid
        return                     # 违反规则的链，算力再多也不接受
    my_chain = candidate
    broadcast(my_chain)

function mine_one_block(my_key):
    tip   = last(my_chain)
    state = replay(my_chain)
    block = {prev_hash: block_hash(tip),
             transactions: [reward_to(my_key)] + valid_subset(pending, state),
             nonce: 0}
    while not has_work(block):     # 没有捷径，只能逐个尝试
        if last(my_chain) != tip:
            return                 # 别人先出了块：放弃，基于新链顶重来
        block.nonce += 1
    my_chain.append(block)
    broadcast(my_chain)
```

`valid_subset` 按顺序挑出在当前状态上仍然有效的交易，tx1 和 tx2 只能选中一笔。为简洁起见，这里省略了从 `pending` 中删除已上链交易的步骤。

把前面的双花代进去：矿工甲的 `pending` 里先有 tx1，矿工乙的先有 tx2，两人各自开始计算。甲先算出来并广播，其他节点重放甲的链，验证通过后切换过去；乙发现链顶变了，放弃手里的块，基于新链重新组装时，tx2 已经通不过序号或余额检查。账本又回到了同一个版本。

这个Toy Model可以运行，但离 Bitcoin 还有不少距离。下表列出两者的主要差别，也是后文各节的顺序。

| Toy Model | Bitcoin 的实际做法 | 详见 |
| --- | --- | --- |
| 收款人直接写公钥 | 地址表达收款条件，花费时提交满足条件的证据 | 钱包、私钥、公钥与签名 |
| 账户余额加序号防重放 | UTXO：每个输出只能整体花费一次 | 一笔交易改变了什么 |
| 对整个区块计算哈希 | 交易汇总成 Merkle root，只对 80 字节的区块头做双 SHA-256 | 区块怎样固定一批交易 |
| 固定 50 的奖励 | 补贴每 210,000 块减半，另加手续费；奖励要等 100 块后才能花费 | 从交易池组装候选区块 |
| 固定的前导零位数 | 256 位目标值，每 2,016 块调整一次难度 | 工作量证明到底在算什么 |
| 广播整条链 | 逐跳通告、按需请求、紧凑区块 | 消息怎样穿过 P2P 网络 |
| 块数最多的链 | 累计工作量最大的有效链，切换时撤销并重放 UTXO 变更 | 两个新区块同时出现以后 |
| 每个参与者都重放全部历史 | 全节点完整验证；轻钱包只验证区块头和 Merkle 路径 | SPV：只下载区块头，能验证到哪一步 |

下面进入 Bitcoin 的真实实现。金额以聪为单位，**1 BTC 等于 100,000,000 聪**。

## 钱包、私钥、公钥与签名

Toy Model直接把公钥当作账户。真实的 Bitcoin 把这件事拆给了几个不同的东西：用户通过钱包收付款，**钱包**是一种管理密钥、生成收款地址、构造交易的软件。

以用户自己掌握密钥的钱包为例，钱包会管理一组密钥。**私钥**是一段秘密数值，钱包用它为付款生成签名；**公钥**可以由私钥计算出来，用于核对签名。公钥可以公开，持有公钥的人能够验证签名，却无法在可行的计算成本内据此反推出私钥，或替持有人签出新的付款。[公钥、私钥与签名](https://github.com/bitcoin/bips/blob/master/bip-0137.mediawiki#background-on-ecdsa-signatures)

前文说过，**数字签名**同时依赖私钥和被签署的内容，不是每次都贴上去的同一张签名图片。Alice 签署的内容包括 Bob 的收款条件和金额；这些内容被改动后，原签名就无法通过验证。

把签名这件事单独取出来，只有三步：

```text
# 钱包根据私钥得到公钥
public_key = derive_public_key(private_key)

# 付款时，用私钥对约定的交易内容摘要签名
signature = sign(private_key, transaction_digest)

# 其他节点用公钥核对签名，全程不需要拿到私钥
valid = verify(public_key, transaction_digest, signature)
```

签名证明的是授权，交易内容本身仍然公开。公钥在这里用于验证，不是拿来把付款内容加密成只有收款人能看的消息。

实际收款时，Bob 发给 Alice 的通常是一个**地址**。地址把收款条件编码成钱包能够识别的字符串，付款方据此指定这笔钱将来可以怎样花费。公钥负责核对签名，地址负责表达收款条件，二者并非同一个数据。不同地址格式表达条件的方式也不同。

| 名称 | 在付款中的作用 |
| --- | --- |
| 私钥 | 生成签名，授权花费自己控制的金额 |
| 公钥 | 让节点核对签名是否由对应私钥产生 |
| 地址 | 交给付款人的收款标识，指定收到的金额将来如何花费 |
| 钱包 | 管理密钥与地址，查找可花费金额，构造并签署交易 |

Alice 给 Bob 付款时，用的是 **Alice 的私钥**，因为正在花费的是 Alice 控制的钱。Bob 的地址决定新产生的收款金额归什么条件支配。等 Bob 要把这笔钱继续付出去时，才轮到 Bob 用自己的私钥签名。[交易授权与收款条件](https://github.com/bitcoin-dot-org/developer.bitcoin.org/blob/master/devguide/transactions.rst#introduction)

钱包里保存或调用的是密钥，金额的归属记录在链上。钱包显示余额时，会根据链上的记录找出自己能够花费的金额。因此，拿到收款地址可以给人付款；拿到私钥则意味着掌握相应的花费能力。[钱包的工作方式](https://github.com/bitcoin-dot-org/developer.bitcoin.org/blob/master/devguide/wallets.rst)

和前文的双花例子一样，签名不替节点决定付款的先后。两笔冲突付款的签名可以都来自 Alice，节点要结合账本中的已有记录，判断哪笔交易还有权花费那份钱。

## 一笔交易改变了什么

Toy Model给每个公钥记一个余额，再用序号防止重放。Bitcoin 不维护账户余额：钱包显示的是一个余额数字，全节点核对的则是一份份具体的可花费记录。这些记录叫作 UTXO，即 **Unspent Transaction Output，尚未花费的交易输出**。普通交易消耗一个或多个旧输出，再创建新的输出，分别指定金额和花费条件。

这样一来，防重放和防双花变成了同一条规则：每个输出只能被花费一次。一笔交易花掉某个输出后，该输出就从可花费集合中消失；把这笔交易原样再提交一次，或者另签一笔交易引用同一个输出，都会因为找不到它而失败。UTXO 是 Bitcoin 的设计选择，并非去中心化的必要条件，Ethereum 就采用账户余额模型。[Rutgers 讲义：Bitcoin](https://people.cs.rutgers.edu/~pxk/classes/419/notes/bitcoin.html)

可以把一个输出看成一张带有领取条件的票据。它有金额，也有锁定条件；下一笔交易引用它，并提供满足条件的证据。全节点维护的 UTXO 集合，记录当前链上还有哪些票据可以使用。钱包显示的余额，则是它能够花费的那些输出的金额总和。一个输出用 `(txid, 输出序号)` 唯一定位：交易标识符 `txid` 指出是哪笔交易，输出序号指出是其中的哪一个输出，从 0 开始编号。这个二元组叫作 outpoint。[交易结构与 UTXO](https://github.com/bitcoin-dot-org/developer.bitcoin.org/blob/master/devguide/transactions.rst#introduction)

假如 Alice 有一个 100,000 聪的输出，要向 Bob 支付 60,000 聪，并给矿工 1,000 聪手续费，交易会长这样。

| 位置 | 引用或去向 | 金额 |
| --- | --- | ---: |
| 输入 0 | 花费旧输出 `A:0` | 100,000 聪 |
| 输出 0 | 锁定给 Bob | 60,000 聪 |
| 输出 1 | 找零给 Alice | 39,000 聪 |
| 输入与输出的差额 | 手续费 | 1,000 聪 |

输入一次用掉整个旧输出。找零是新建的输出；手续费是总输入和总输出的金额差额。

下面的伪代码采用单密钥付款，签名摘要、脚本与锁定条件交给具名函数处理。`require` 表示检查失败就拒绝；`range(a, b)` 包含 `a`、不包含 `b`；`^` 表示乘方。

```text
Outpoint = (txid, output_index)
Output   = {value, locking_condition}
Coin     = {output, created_height, is_coinbase}  # 最后一项标记它是否来自区块奖励
UTXO     = Map<Outpoint, Coin>

tx.inputs  = [{previous_output: (A, 0)}]
tx.outputs = [
    {value: 60_000, locking_condition: pay_to(Bob)},
    {value: 39_000, locking_condition: pay_to(Alice_change_key)}
]

previous = lookup_previous_outputs(tx)
for i in indices(tx.inputs):
    # 对本次花费的内容签名，私钥不进入交易记录中
    digest = signature_digest(tx, i, previous, mode=ALL)
    tx.inputs[i].authorization = sign(Alice_private_key, digest)
```

这里的 `ALL` 表示签名承诺这笔付款的输入引用和所有输出等内容。若有人把 Bob 的收款输出换成自己的，签名验证就会失败。

全节点验证普通交易时，核心工作是检查引用、授权是否合法和金额守恒。下面的函数只计算状态变更，所有检查通过之后才把它应用到 UTXO；中途失败就不会留下“已经扣款”的半个状态。

```text
function check_regular_transaction(tx, view, context):
    require not is_coinbase(tx)          # 区块奖励交易另行处理
    require valid_structure_and_money_ranges(tx)
    require inputs_are_unique(tx)
    require locks_are_satisfied(tx, view, context)

    previous = []
    for input in tx.inputs:
        coin = view.get(input.previous_output)
        require coin exists                       # 已花费的输出不在这里
        if coin.is_coinbase:             # 区块奖励的输出要等到成熟才能花费
            require context.height - coin.created_height >= 100
        previous.append(coin)

    # 所有输入的授权均通过后，再生成账本变更
    for i in indices(tx.inputs):
        require verify_spend(tx, i, previous, context)

    input_value  = checked_sum(coin.output.value for coin in previous)
    output_value = checked_sum(output.value for output in tx.outputs)
    require input_value >= output_value

    delta = {
        remove: all input.previous_output,
        add: each (txid(tx), i) -> Coin(tx.outputs[i], context.height, false)
    }
    return {delta, fee: input_value - output_value}

result = check_regular_transaction(tx, view, context)
view.apply_atomically(result.delta)
```

`view` 是某个明确链状态的视图。检查新区块时，它从该块父节点的 UTXO 状态开始，并按区块内交易顺序变化。因此，一笔交易可以花费同一区块里排在前面的普通交易输出；同一个输出被花掉后，第二次引用就会失败。对金额范围、重复输入、coinbase 成熟期的具体检查，可对照 Core 的 [交易结构检查](https://github.com/bitcoin/bitcoin/blob/v30.0/src/consensus/tx_check.cpp) 和 [输入检查](https://github.com/bitcoin/bitcoin/blob/v30.0/src/consensus/tx_verify.cpp)。

Alice 仍然能构造另一笔交易，引用 `A:0`，把钱转给自己。孤立地验证两笔交易的签名，都会成功。只有把它们放进同一个不断更新的 UTXO 状态，冲突才会显现。这也是 Bitcoin 需要全网共同历史的原因。这笔交易从钱包签名到被区块确认的完整经过，见后文“端到端：花掉一个 UTXO 的完整流程”。

## 区块怎样固定一批交易

Toy Model把整个区块拿来算哈希。真实区块分成两部分：主体保存交易列表，**区块头**保存这批交易的摘要以及前一个区块头的哈希。挖矿和链接都只针对 80 字节的区块头，交易再多，每次哈希的输入也一样大。交易列表怎样汇总成一个摘要？Bitcoin 使用 **Merkle 树**，最后得到的根哈希称为 Merkle root。

下文用 `H(x)` 表示 `SHA256(SHA256(x))`，即 Bitcoin 用于区块头和这里的 Merkle 树的双 SHA-256；`a || b` 表示拼接字节。

Merkle 树的叶子是交易标识，父节点是左右子节点拼接后的哈希。假设区块里有四笔交易：

```text
h0 = txid(tx0)                   h1 = txid(tx1)
h2 = txid(tx2)                   h3 = txid(tx3)
h01 = H(h0 || h1)                h23 = H(h2 || h3)
root = H(h01 || h23)
```

某个叶子的 txid 变化，会沿着路径改变根哈希。区块头只用 32 字节就能承诺整个有序交易列表。计算时，如果某一层节点数为奇数，就复制最后一个哈希再配对。[Merkle 规则](https://github.com/bitcoin/bitcoin/blob/v30.0/src/consensus/merkle.cpp)

```text
function merkle_root(transactions):
    require len(transactions) > 0
    level = [txid(tx) for tx in transactions]
    while len(level) > 1:
        if len(level) is odd:
            level.append(level[-1])       # 复制最后一个，凑成一对

        # 相邻节点两两合并，直到只剩一个根
        level = [H(level[i] || level[i + 1])
                 for i in range(0, len(level), step=2)]

    return level[0]
```

区块验证还会检查重复子树，排除这套补齐规则可能造成的根哈希歧义。

Bitcoin 区块头共 80 字节，字段如下。[区块头定义](https://github.com/bitcoin/bitcoin/blob/v30.0/src/primitives/block.h)

| 字段 | 字节数 | 作用 |
| --- | ---: | --- |
| `version` | 4 | 版本与升级信号 |
| `previous_block_hash` | 32 | 引用父区块头 |
| `merkle_root` | 32 | 承诺本区块的交易列表 |
| `time` | 4 | 受共识时间规则约束的时间戳 |
| `nBits` | 4 | 工作量证明目标值的紧凑编码 |
| `nonce` | 4 | 挖矿时可反复尝试的数值 |

后一个区块通过父哈希引用前一个区块，交易由此获得了在历史中的位置。区块头里的时间戳由矿工填写，并受节点检查；交易在账本中的先后，取决于它所在的区块和块内位置，而非某台机器最先收到它的时刻。

SegWit 为交易增加了见证数据。`txid` 根据不含见证的交易序列化计算，`wtxid` 则包含见证；**区块头里的 Merkle root 仍由 txid 构造**。见证树的承诺写入 coinbase 的特定输出，再通过 coinbase 的 txid 间接进入区块头。后面的区块构造伪代码采用无见证交易，真实 SegWit 区块还会计算并验证这项承诺。[BIP 141](https://github.com/bitcoin/bips/blob/master/bip-0141.mediawiki)

## 从交易池组装候选区块

尚未确认、被本节点接纳的交易暂存在内存交易池，也就是 mempool。每个节点看到交易的时间、连接的对等节点和本地策略各不相同，因此交易池也各不相同。矿工从自己的交易池挑选交易，形成一个候选区块。

挑选交易时，费用和依赖关系一起影响收益。如果交易 B 花费了交易 A 的输出，而 A 还没上链，矿工要把 A 排在 B 前面。实际选择还受区块重量、签名操作成本等限制，可以按包含祖先交易的组合评估手续费。下面将这部分封装成选择函数，把账本变化展开。[Core 区块构造](https://github.com/bitcoin/bitcoin/blob/v30.0/src/node/miner.cpp)

```text
function build_candidate(parent, mempool, miner_key):
    height = parent.height + 1
    context = next_block_context(parent)
    view = copy(UTXO_at(parent))
    selected = []
    fees = 0

    # 已预留 coinbase 与头部空间；祖先在前，后代在后
    for tx in choose_transactions_in_dependency_order(mempool):
        if not fits_block_limits(selected, tx):
            continue
        result = try_check_regular_transaction(tx, view, context)
        if result is invalid:
            continue
        view.apply_atomically(result.delta)
        selected.append(tx)
        fees += result.fee

    coinbase = make_coinbase(
        height=height,
        extra_nonce=0,
        pay_to=miner_key,
        value=subsidy(height) + fees
    )
    txs = [coinbase] + selected
    return Block(
        header={version: next_version(parent),
                previous_block_hash: block_hash(parent),
                merkle_root: merkle_root(txs),
                time: valid_candidate_time(parent),
                nBits: expected_nBits(parent),
                nonce: 0},
        transactions=txs
    )
```

第一笔交易 coinbase 是新增货币和结算手续费的入口。它有一个特殊输入，前序输出引用为空，不从普通 UTXO 扣款；它的输出把可领取金额锁定给矿工。输入脚本的起始位置编码区块高度，矿工还可以在允许范围内附加额外数据。[coinbase 结构](https://github.com/bitcoin/bitcoin/blob/v30.0/src/primitives/transaction.h)、[BIP 34](https://github.com/bitcoin/bips/blob/master/bip-0034.mediawiki)

奖励由两部分组成：区块补贴是新发行的币，手续费是已有币的转移。全节点检查的是：

```text
sum(coinbase.outputs.value) <= subsidy(height) + sum(regular_tx_fees)
```

矿工可以少领，超领则使整个区块无效。补贴按高度计算，比特币主网从每块 50 BTC 开始，每 210,000 块减半，以整数聪向下取整；各时期的补贴相加，名义发行上限约为 2,100 万 BTC。[补贴与奖励验证](https://github.com/bitcoin/bitcoin/blob/v30.0/src/validation.cpp)、[主网参数](https://github.com/bitcoin/bitcoin/blob/v30.0/src/kernel/chainparams.cpp)

```text
function subsidy(height):
    halvings = height // 210_000
    if halvings >= 64:
        return 0
    return (50 * 100_000_000) >> halvings    # 每右移一位，整数金额减半
```

补贴逐步减少后，矿工收入会更多地依靠手续费；补贴归零后，协议内的区块收入全部来自手续费。矿工的收入取决于自己提出的区块是否被共同账本采用。

coinbase 输出还受到 100 块的成熟期约束：支出它的区块高度减去产生它的区块高度，至少为 100。若矿工挖出的块最终离开主链，对应 coinbase 输出也随该分支一起退出当前账本。算力支出已经发生，奖励却可能拿不到，这使及时传播新区块、继续扩展共同接受的分支具有直接的经济意义。[coinbase 成熟规则](https://github.com/bitcoin/bitcoin/blob/v30.0/src/consensus/tx_verify.cpp)

## 工作量证明到底在算什么

矿工反复修改区块头，计算哈希，把结果当成一个 256 位整数。满足以下不等式，就找到了这个候选区块的工作量证明：

```text
uint256(H(serialize_header(header))) <= target
```

`target` 越小，命中的范围越窄。若把哈希输出看成均匀随机数，单次成功概率是 `(target + 1) / 2^256`，平均尝试次数是其倒数。Toy Model里的“前 30 位为 0”，相当于取 `target = 2^226 - 1`；真实目标值不必是 2 的幂，难度因而可以连续调节。

这项计算没有在解一道能逐步接近答案的方程。换一个 nonce，就像重新抽一次签。成功很难预先安排，验算却只需计算区块头哈希，再检查目标值。

```text
function mine(parent):
    block = build_candidate(parent, local_mempool, miner_key)
    coinbase = block.transactions[0]
    target = decode_compact(block.header.nBits)

    while active_tip == parent:
        for nonce in range(0, 2^32):
            if active_tip != parent:
                return REBUILD_ON_NEW_TIP
            block.header.nonce = nonce
            digest = H(serialize_header(block.header))
            if uint256(digest) <= target:
                return block

        # 32 位 nonce 用完后，改变 coinbase，得到新的哈希搜索空间
        coinbase.extra_nonce += 1
        block.header.merkle_root = merkle_root(block.transactions)
        block.header.time = valid_candidate_time(parent)

    return REBUILD_ON_NEW_TIP
```

`extra_nonce` 是 coinbase 输入中矿工使用的额外数据，不是第七个区块头字段。它改变 coinbase 的 txid，进而改变 Merkle root，使同一组 32 位 nonce 又能对应一批新候选。实际挖矿设备会并行划分搜索空间，这段循环只表达搜索关系。

矿工不能给自己的块随意挑一个容易命中的 `target`。接收者既检查哈希是否达标，也检查 `nBits` 是否等于父链规则要求的值。否则，攻击者可以把目标调到最大，用几乎没有成本的块填出一条链。[Core 工作量证明与难度计算](https://github.com/bitcoin/bitcoin/blob/v30.0/src/pow.cpp)

主网以平均 10 分钟一个区块为目标，每 2,016 块调整一次难度。调整按上一周期的时间跨度计算，并把单次变化限制在四倍范围内。忽略紧凑编码和边界细节，可写成：

```text
span = timestamp(last_block) - timestamp(first_block_in_period)
span = clamp(span, TWO_WEEKS / 4, TWO_WEEKS * 4)
new_target = min(pow_limit, old_target * span / TWO_WEEKS)
```

“10 分钟”是长期目标，单个区块可能很快出现，也可能让人等很久。[主网参数](https://github.com/bitcoin/bitcoin/blob/v30.0/src/kernel/chainparams.cpp)、[难度调整实现](https://github.com/bitcoin/bitcoin/blob/v30.0/src/pow.cpp)

修改旧交易会改变 Merkle root，旧区块头的哈希随之改变，后续区块保存的父哈希也就对不上了。攻击者要从被改动的位置重建工作量证明，同时追赶仍在增长的诚实分支。哈希链让修改暴露出来，工作量证明让重建历史付出计算成本。

## 消息怎样穿过 P2P 网络

Bitcoin 节点只与有限数量的对等节点直接连接。交易和区块由这些连接逐跳传递，不依赖一台负责全网分发的服务器。传播需要时间，各节点收到消息的次序也不一致。

![比特币架构：交易经点对点网络传播，矿工构造区块并寻找工作量证明，各全节点独立验证区块、选择有效链并更新 UTXO](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/blockchain/assets/bitcoin-architecture.svg)

一个便于理解的传播循环如下。`announce` 表示告诉邻居“我有这份数据”，对方缺少时再请求；它不要求每一次通知都重复发送完整对象。

```text
on_transaction(tx, peer):
    if already_have(tx):
        return

    # 接纳流程联合处理依赖、冲突、交易验证和本地策略
    result = try_accept_to_mempool(tx, active_tip, mempool)
    if result == MISSING_PARENT:
        defer_until_parent_is_available(tx)
        return
    if result == ACCEPTED:              # 仅更新交易池，未修改链上 UTXO
        announce(tx, peers_except(peer))

on_inventory(object_id, peer):
    if not already_have(object_id):
        request_object(peer, object_id)

on_block(block, peer):
    if already_have(block):
        return
    if parent_is_unknown(block):
        request_missing_ancestors(peer, block)
        defer(block)
        return

    result = validate_block_on_its_parent(block)
    if result is valid:
        block_tree.store(block, result.state, result.undo)
        consider_best_chain()
        announce(block, peers_except(peer))
```

交易池的接纳流程在已确认 UTXO 之外，还会考虑未确认交易产生的输出和占用的输入，处理替换交易、祖先依赖与费用下限。**mempool 接纳策略和区块共识规则的职责不同**：某节点不愿转发的一笔交易，如果后来进入有效区块，节点仍按共识规则验证该区块。[交易池接纳与区块验证](https://github.com/bitcoin/bitcoin/blob/v30.0/src/validation.cpp)

收到完整区块时，验证要把前面的机制接在一起：

```text
function validate_block_on_its_parent(block):
    parent = lookup(block.header.previous_block_hash)
    context = next_block_context(parent)
    require block.header.nBits == expected_nBits(parent)
    require valid_header_time_and_version(block.header, parent)
    require valid_target_and_proof_of_work(block.header)
    require valid_block_structure_and_resource_limits(block)

    require merkle_root(block.transactions) == block.header.merkle_root
    require first_transaction_is_coinbase_and_only_one_exists(block)
    coinbase = block.transactions[0]
    require valid_coinbase_structure_and_height(coinbase, context)

    view = copy(UTXO_at(parent))
    fees = 0
    for tx in block.transactions[1:]:
        result = check_regular_transaction(tx, view, context)
        view.apply_atomically(result.delta)
        fees += result.fee

    require sum(coinbase.outputs.value) <= subsidy(context.height) + fees
    view.add_coinbase_outputs(coinbase, context.height)
    return {state: view, undo: changes_needed_to_restore_parent}
```

节点可以先发送区块头，让邻居得知新区块的存在；也可以利用对方已有的交易重建区块，只补传缺少的数据。这些机制分别见 [BIP 130 的区块头通告](https://github.com/bitcoin/bips/blob/master/bip-0130.mediawiki) 与 [BIP 152 的紧凑区块](https://github.com/bitcoin/bips/blob/master/bip-0152.mediawiki)。这些优化减少等待和带宽；节点最终采用一条链时，仍要完成对应的共识验证。

普通全节点可以只验证和转发而不挖矿。参与挖矿的一方还要选择交易、构造候选块，并进行哈希计算；矿池可以把这些工作分配给不同组件。

## 两个新区块同时出现以后

假如矿工甲和矿工乙几乎同时找到了新区块，两块都指向父区块 P。靠近甲的节点先看见 A，靠近乙的节点先看见 B。只要内容有效，两个块都可以暂时保存在区块树里。

```text
             A ── A2
            /
… ── P ────
            \
             B
```

节点选择的是**累计工作量最大的有效链**。Toy Model难度固定，所以直接比块数；真实网络的难度会调整，在难度相同的区段，块数更多就意味着工作量更多，跨越不同难度时，直接数块会得到错误答案。论文里的“最长链”说的是投入工作量最多的链；Bitcoin 软件在 2010 年 7 月从比较链长改为比较累计工作量。[Rutgers 讲义：Bitcoin](https://people.cs.rutgers.edu/~pxk/classes/419/notes/bitcoin.html)

对目标值为 `T` 的区块，Core 记账用的工作量为：

```text
block_work(T) = floor(2^256 / (T + 1))
chainwork(block) = chainwork(block.parent) + block_work(block.target)
```

这是按难度计算的工作量度量，节点看不到矿工究竟试了多少次。一个幸运的矿工第一次就命中，也会得到同样的工作量记分。[Core `GetBlockProof`](https://github.com/bitcoin/bitcoin/blob/v30.0/src/chain.cpp)

```text
function consider_best_chain():
    candidates = branches_with_complete_data_and_valid_ancestry()
    best = candidate_with_greatest_chainwork(candidates)

    if best.chainwork <= active_tip.chainwork:
        return                              # 平局时保留当前分支

    fork = common_ancestor(active_tip, best)
    staged = copy(active_UTXO)

    # 撤销旧分支，回到公共祖先的状态
    for block in path_backwards(active_tip, exclusive_end=fork):
        staged.apply(block.undo)

    # 按正向顺序应用新分支，公共祖先不用重复处理
    for block in path_forwards(fork, best, exclusive_start=true):
        staged = validate_and_apply(block, staged)

    atomic_commit(active_tip=best, active_UTXO=staged)
    rebuild_mempool_after_reorg()
```

链切换要把链顶和 UTXO 状态一起更新。候选分支首先得满足共识规则，再按累计工作量竞争。无效交易、超额 coinbase 和错误难度都会让分支失去资格，堆上再多算力也不会让遵守这些规则的全节点接纳它。[Core 链激活逻辑](https://github.com/bitcoin/bitcoin/blob/v30.0/src/validation.cpp)

当 A 后面又接上 A2，在同难度例子里，A 分支胜出。原先采用 B 的节点撤销 B 对 UTXO 的修改，再应用 A 和 A2，这叫链重组（reorg）。B 中没有被新链确认、且仍符合条件的普通交易，可以重新进入交易池；与新链冲突的交易失效。B 的 coinbase 奖励则随 B 一起离开当前账本。

网络没有一个“所有人都先收到”的全局时刻。交易最终的先后由被采用的区块历史决定。若付款包含在高度 `h` 的区块中，当前链顶高度为 `H`，它的确认数为 `H - h + 1`：被打包时是一次确认，再追加一个块就是两次。

## SPV：只下载区块头，能验证到哪一步

全节点逐笔检查交易，维护完整的 UTXO 状态。一个只关心自己收付款的钱包，也可以采用更轻量的 SPV（Simplified Payment Verification，简化支付验证）：保存区块头链，再为关心的交易获取一条 Merkle 路径。

例如要检查 `tx2`，客户端已有交易本身，只需获得兄弟哈希 `h3`，以及上一层的兄弟哈希 `h01`，就能算回根。

![Merkle 与 SPV：从 tx2 计算 h2，结合右侧 h3 与左侧 h01，逐层重建区块头里的根哈希](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/blockchain/assets/bitcoin-merkle-spv.svg)

```text
function verify_merkle_path(tx, path, expected_root):
    value = txid(tx)
    for step in path:                     # 从叶子走向根
        if step.sibling_side == LEFT:
            value = H(step.sibling_hash || value)
        else:
            value = H(value || step.sibling_hash)
    return value == expected_root

function check_spv_payment(tx, block_header, proof, header_chains):
    require payment_output_matches_invoice(tx)
    chain = greatest_work_header_chain(header_chains,
        genesis=BITCOIN_MAINNET_GENESIS,
        check_links=true, check_expected_difficulty=true,
        check_pow=true, check_header_time=true)
    require block_header belongs to chain
    require proof_has_valid_shape_and_position(proof)
    require verify_merkle_path(tx, proof.path, block_header.merkle_root)
    # 高度由头部链索引计算，不是区块头里的字段
    confirmations = chain.tip.height - chain.height_of(block_header) + 1
    return {included: true, confirmations}
```

若块中有 `n` 笔交易，路径长度约为 `log₂(n)`。对一个有 4,096 笔交易的整齐二叉树，路径只含 12 个兄弟哈希，共 384 字节，再加交易本身及路径元数据。这个量级来自树的结构，与下载全部交易相比小得多。

Merkle 路径证明的是“这笔交易被这个区块头承诺”。它没有检查所有输入是否真的可花、区块有没有超发、之前是否出现双花。SPV 因而依赖诚实算力维护有效历史，并依赖客户端能够接触到诚实网络；若只连接到攻击者，即使所见的头部链满足 PoW，也可能漏掉外部更强的链。伪代码返回 `included`，没有把它写成 `fully_validated`。

全节点也可以节省磁盘空间：完整验证后删除较旧的区块和撤销数据文件，保留继续验证所需的链状态。这叫剪枝。它仍然逐笔检查交易，承担全节点的验证工作。[Core 剪枝实现](https://github.com/bitcoin/bitcoin/blob/v30.0/src/node/blockstorage.cpp)

## 等待确认，究竟把风险降低了多少

假如 Alice 在向 Bob 付款的同时，开始私下挖另一条分支，把同一笔钱转给自己。Bob 看到付款进入公开链，等待确认后交付商品；Alice 随后公布私有分支，试图让它取代原有历史。攻击成功后，Bob 收到的那笔付款就从当前账本消失了。

这是一场重写付款历史的竞争。两条分支里的签名都可以有效，冲突在于同一份钱付给了不同的人。

设攻击者占总算力的比例为 `q`，诚实算力比例为 `p = 1 - q`。在算力固定、两边独立出块、难度相同的模型中，下一个块由诚实方挖出的概率是 `p`，攻击者挖出的概率是 `q`。

若攻击者此刻落后 `d` 个块，每次诚实方出块，差距加一；每次攻击者出块，差距减一。追平概率记为 `C(d)`，它满足随机游走关系：

```text
C(d) = p × C(d + 1) + q × C(d - 1)
C(0) = 1
```

当 `q < p`，初始差距 `d` 越大，最终追平的概率越低；当 `d` 趋于无穷时，`C(d)` 趋于零。带入边界条件可得到：

$$
C(d)=
\begin{cases}
1, & d\leq 0\ \text{或}\ q\geq p,\\
\left(\dfrac{q}{p}\right)^d, & d>0\ \text{且}\ q<p.
\end{cases}
$$

例如 `q = 0.1`，若已知攻击者落后 6 块，追平概率是 `(1/9)^6 ≈ 0.0000018817`，约为 `0.00018817%`。这里的条件是“已经知道差距为 6”。商户实际看得到诚实链，却看不到攻击者的私有进度，因而还要算另一层概率。

设双方从同一父块开始挖，付款进入诚实分支的第一个新区块。当诚实分支长出 `z` 个块时，付款就有了 `z` 次确认。先用产生这 `z` 个块的平均耗时估计等待时间，那么攻击者同期的出块数 `K` 可以用泊松分布近似，均值为：

$$
\lambda=z\frac{q}{p},\qquad
\Pr[K=k]=e^{-\lambda}\frac{\lambda^k}{k!}.
$$

若攻击者已经挖出 `k` 个块，他落后的是 `z-k`。将每种进度出现的概率，与从该进度最终追平的概率相乘，再求和，就得到等待后的追平概率。对 `0 < q < p`：

$$
P(z,q)=\sum_{k=0}^{\infty}
e^{-\lambda}\frac{\lambda^k}{k!}\,C(z-k)
=1-\sum_{k=0}^{z}
e^{-\lambda}\frac{\lambda^k}{k!}
\left[1-\left(\frac{q}{p}\right)^{z-k}\right].
$$

这与直接计算 `(q/p)^z` 不同。后者把攻击者在等待期间取得的进度当成了零。

下面用相邻泊松概率的递推关系计算，省去反复计算阶乘。它适合本文表格这样的中小参数。

```text
function approximate_catch_up_probability(q, z):
    require 0 <= q <= 1
    require z is integer and z >= 0
    if z == 0:
        return 1
    if q == 0:
        return 0
    if q >= 0.5:
        return 1

    p = 1 - q
    ratio = q / p
    lambda = z * ratio
    poisson = exp(-lambda)                 # Pr[K = 0]
    probability = 1

    for k in range(0, z + 1):
        probability -= poisson * (1 - ratio^(z - k))
        poisson *= lambda / (k + 1)        # Pr[K = k + 1]

    return clamp(probability, 0, 1)
```

按这个模型计算，结果如下。表内数字都是百分比，`z` 是诚实分支从共同父块起新增的区块数。

| 新增区块数 `z` | 攻击者算力 10% | 攻击者算力 30% |
| ---: | ---: | ---: |
| 1 | 20.458727% | 62.774911% |
| 3 | 1.317224% | 32.458410% |
| 6 | 0.024280% | 13.211117% |
| 10 | 0.000124% | 4.166048% |

在这个模型里，10% 算力的攻击者面对 6 个新增区块，追平概率约为 0.024%；换成 30% 算力，这个数字约为 13.21%。确认数相同，背后的算力条件不同，风险就会相差很远。

少数算力也有追平机会；`q` 越接近一半，等待带来的风险下降越慢。`q ≥ 0.5` 时，最终追平概率为 1，但允许等待的时间没有上限。

这套估算用平均等待时间近似实际等待时间，并以追平为计算终点。真实网络里，平局后的胜负还取决于传播和后续出块。若把诚实方挖到第 `z` 块所需的随机时间也纳入计算，攻击者同期出块数要改用负二项分布，数值也会变化。[Double Spend Races](https://arxiv.org/html/1702.02867v3#S5) 给出了这一步的推导。

## 端到端：花掉一个 UTXO 的完整流程

前面各节按模块拆开讲：钱包怎样签名，节点怎样验证，矿工怎样出块，分叉怎样处理。这一节换成时间顺序：Alice 在钱包里点下“发送”，到 Bob 看到 6 次确认，`A:0` 依次经过哪些参与方，每一步改了谁的状态。

沿用前面的例子：Alice 持有 100,000 聪的输出 `A:0`，付给 Bob 60,000 聪，找零 39,000 聪，手续费 1,000 聪。

![花掉一个 UTXO 的时序图：Alice 的钱包构造并签名，提交给节点做交易池接纳，交易经 P2P 传播到矿工和 Bob，矿工打包挖矿，新区块传播后各节点更新 UTXO，确认数逐步增加](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/blockchain/assets/bitcoin-spend-utxo-sequence.svg)

### A:0 同时存在于三份状态里

理解这条流程的关键在于，“`A:0` 花没花掉”这个问题有三个答案，分别来自三份互相独立的状态：

- **钱包状态**：钱包记录哪些输出属于自己、哪些已被自己的待确认交易用掉，用来避免下一笔付款再选中同一个输出。
- **交易池状态**：每个节点的 mempool 记录哪些输出已被某笔未确认交易占用。各节点的交易池并不相同，也不是共识的一部分。
- **链上状态**：全节点的 UTXO 集合，只随当前采用的链变化，所有遵守规则的全节点在同一条链上会得到同一个结果。

| 阶段 | 钱包 | Alice 节点的交易池 | 链上 UTXO 集合 | Bob 看到的 |
| --- | --- | --- | --- | --- |
| ① 构造与签名 | `A:0` 已锁定，tx 已签名 | 无 | `A:0` 未花费 | 无 |
| ② 提交 | 等待节点返回结果 | `A:0` 被 tx 占用 | `A:0` 未花费 | 无 |
| ③ 传播 | 已广播 | 同上；多数节点的交易池陆续收到 tx | `A:0` 未花费 | 待确认收款，0 确认 |
| ④ 打包与挖矿 | 已广播 | 同上 | `A:0` 未花费，只是被写进了某个候选区块 | 0 确认 |
| ⑤ 区块上链 | 1 确认 | tx 被移除 | 删除 `A:0`，新增 `(tx, 0)` 和 `(tx, 1)` | 1 确认 |
| ⑥ 更多区块 | 确认数增加 | 无 | 同上 | 确认数 = `H - h + 1` |

只有第 ⑤ 步改变链上状态。在此之前，付款只是一条被广泛转发的消息，Alice 的节点乐于转发它，不代表全网已经认可它。

### 顶层流程

下面用一个驱动函数把整条流程串起来。它只是表达先后关系；实际系统里，这些步骤由不同机器上的事件循环异步完成。

```text
function pay(wallet, node, to_address, amount, target_feerate):
    tx = wallet_send(wallet, to_address, amount, target_feerate)   # ① 构造、签名、锁定输入
    result = node.submit(tx)                                       # ② 接纳成功后，节点自动进入 ③ 中继
    if result is not ACCEPTED:
        wallet.unlock_inputs(tx)                                   # 输入还给可花费集合
        return result
    return txid(tx)                     # ④ 到 ⑥ 由网络推进，钱包只能通过节点事件跟踪

# 钱包启动时订阅一次节点事件，处理函数见 ⑥
node.subscribe(BLOCK_CONNECTED,    wallet.on_block_connected)
node.subscribe(BLOCK_DISCONNECTED, wallet.on_block_disconnected)
node.subscribe(TX_REMOVED,         wallet.on_tx_removed)
```

### ① 钱包：选币、构造、签名

钱包先找出自己能花的输出。Bitcoin Core 的钱包会排除已被自己的待确认交易用掉的输出，还会跳过来源不安全的未确认输出，比如正在替换其他交易、或已被替换的交易产生的输出。[Core 钱包选币](https://github.com/bitcoin/bitcoin/blob/v30.0/src/wallet/spend.cpp)

接下来是选币：从可用输出里挑一组，使总额覆盖付款金额加手续费。手续费按交易的虚拟字节数乘以目标费率估算，而交易大小又取决于选了几个输入、有没有找零输出，所以选币和算费要一起做。Core 会分别尝试分支定界（BnB）、背包、单次随机抽取（SRD）等算法，高费率时再加上 CoinGrinder，最后按一个“浪费”指标挑出最优解。[Core 选币算法](https://github.com/bitcoin/bitcoin/blob/v30.0/src/wallet/coinselection.h)、[Core 钱包选币](https://github.com/bitcoin/bitcoin/blob/v30.0/src/wallet/spend.cpp)

```text
function wallet_send(wallet, to_address, amount, feerate):
    candidates = [c for c in wallet.my_coins()
                  if not wallet.is_locked(c.outpoint)        # 没被自己的待确认交易占用
                  and wallet.is_safe(c)]                     # 已确认，或来自自己的交易

    # 选币：输入总额 ≥ 付款 + 手续费；手续费 = 估算大小 × 费率
    selection = select_coins(candidates, amount, feerate)
    require selection exists                                 # 否则“余额不足”

    tx = new Transaction()
    for coin in selection.inputs:
        tx.inputs.append({previous_output: coin.outpoint})
    tx.outputs.append({value: amount, locking_condition: decode(to_address)})
    change = selection.total - amount - selection.fee
    if change > dust_threshold:                              # 找零太小就并入手续费
        tx.outputs.insert(random_position(), {value: change,
                          locking_condition: wallet.new_change_condition()})

    previous = [coin.output for coin in selection.inputs]
    for i in indices(tx.inputs):
        digest = signature_digest(tx, i, previous, mode=ALL)
        tx.inputs[i].authorization = sign(wallet.key_for(previous[i]), digest)

    wallet.lock_inputs(tx)                    # 钱包内部标记，防止下一笔付款再选中 A:0
    wallet.store(tx, state=PENDING)
    return tx
```

代入例子：`candidates` 里只有 `A:0`，选币结果是 100,000 聪、手续费 1,000 聪，找零 39,000 聪。签名摘要用到的 `previous` 是被花费输出本身的金额和锁定条件，签名因而同时承诺了“花的是哪笔钱”和“钱付给谁”。这一步全部在本地完成，网络上还没有任何人知道这笔交易。

### ② 节点：交易池接纳

钱包把签好的交易交给一个全节点，通常是自己运行的节点，或钱包服务商的节点。Bitcoin Core 的 `sendrawtransaction` 在提交前会先做一道保护：手续费率超过默认上限（每千虚拟字节 0.1 BTC）就拒绝，防止误把大额找零写成手续费。[Core 交易提交](https://github.com/bitcoin/bitcoin/blob/v30.0/src/node/transaction.h)

```text
function submit(tx):                            # 对应 sendrawtransaction
    require fee_rate(tx) <= MAX_RAW_TX_FEE_RATE # 默认 0.1 BTC / 1,000 vB
    result = accept_to_mempool(tx, mempool, chain)
    if result == ACCEPTED:
        on_accepted(tx)                         # 进入 ③
    return result
```

接纳一笔交易要过两类检查。共识检查和区块验证用的是同一套规则；策略检查是节点自己对交易池的要求，不满足策略的交易不会被转发，但如果它进入了有效区块，节点仍会接受那个区块。

```text
function accept_to_mempool(tx, mempool, chain):
    # 视图 = 链上 UTXO + 交易池里未确认交易产生的输出
    view = chain.utxo_view() overlaid_with mempool.unconfirmed_outputs()

    if any input.previous_output not in view:
        return MISSING_INPUTS                   # 父交易还没收到，或该输出已在链上被花掉

    # 共识规则：签名有效、金额守恒、锁定时间满足等，与区块验证相同
    result = try_check_regular_transaction(tx, view, chain.next_context())
    if result is invalid:
        return REJECTED(result.reason)

    conflicts = [mempool.spender_of(input.previous_output)
                 for input in tx.inputs
                 if mempool.spender_of(input.previous_output) exists]

    # 节点策略：标准脚本、最低转发费率、祖先/后代数量、交易池容量
    if not meets_standardness(tx) or result.fee_rate < mempool.min_feerate():
        return REJECTED(POLICY)

    if conflicts is not empty:                  # 有人先占用了同一个输入
        if not satisfies_replacement_rules(tx, result.fee, conflicts):
            return REJECTED(CONFLICT)
        mempool.remove_with_descendants(conflicts, reason=REPLACED)

    mempool.add(tx, fee=result.fee)             # 记录：A:0 已被 tx 占用
    return ACCEPTED
```

这里的 `view` 和区块验证不同：它在链上 UTXO 之上叠加了交易池里未确认交易产生的输出，所以一笔交易可以花费另一笔尚未确认的交易的找零。已被交易池占用的输出在 `view` 里仍然可见，冲突由 `mempool.spender_of` 单独检测：它记录每个输出被交易池里的哪笔交易占用，这就是交易池层面的双花检测。

如果 Alice 在 tx 之后又签了一笔花 `A:0` 的 tx'，节点不会简单拒绝，而是按替换规则（Replace-by-Fee，RBF）判断。主要条件是：tx' 的绝对手续费不能低于被替换交易的手续费之和，增加的部分要按增量转发费率覆盖 tx' 自身的大小，费率要高于所有直接冲突的交易，被替换的交易连同后代不超过 100 笔。Bitcoin Core 从 v28.0 起默认启用 full-RBF，原交易不必事先声明可替换。[Core 替换策略](https://github.com/bitcoin/bitcoin/blob/v30.0/doc/policy/mempool-replacements.md) 也就是说，交易池阶段的“先到先得”只是节点的本地策略，费率更高的冲突交易可以把它挤掉，只有区块能最终裁决。

### ③ 网络：逐跳中继

接纳成功后，节点用前文的 `inv`、`getdata`、`tx` 三步把交易交给邻居：先通告标识，对方没见过才来要完整数据。通告不是立即发出，Bitcoin Core 会把待通告交易攒起来，按随机间隔批量发送，对入站连接平均 5 秒、出站连接平均 2 秒，让旁观者更难从通告时间推断交易最早出自哪个节点。[Core 交易中继](https://github.com/bitcoin/bitcoin/blob/v30.0/src/net_processing.cpp)

```text
on_accepted(tx):
    for peer in peers:
        if peer.fee_filter <= fee_rate(tx):          # 对方声明过不收太便宜的交易
            peer.pending_announcements.add(wtxid(tx))

every random_interval(peer):                          # 平均 2 秒或 5 秒
    send(peer, INV(peer.pending_announcements))
    peer.pending_announcements.clear()

on_inv(ids, peer):
    request(peer, GETDATA([id for id in ids if not already_have(id)]))

on_tx(tx, peer):
    if already_have(tx):
        return
    if accept_to_mempool(tx, mempool, chain) == ACCEPTED:
        on_accepted(tx)                               # 继续转给自己的邻居
```

每个节点都独立执行 `accept_to_mempool`，所以传播本身也是一轮轮的验证：无效交易走不出第一个节点。几秒钟内，多数节点的交易池里都有了 tx，其中包括矿工的节点和 Bob 钱包所连接的节点。

Bob 的钱包此时会显示一笔待确认收款。这笔钱还不能当真：只要包含 tx 的区块没出现，Alice 就仍可能让一笔冲突交易先上链。

### ④ 矿工：打包与挖矿

矿工按前面 `build_candidate` 的逻辑从交易池挑交易。tx 能不能进候选区块，取决于它的费率和区块空间的竞争，与它被收到的先后无关。tx 一旦被选中，候选区块里的 `view` 就把 `A:0` 删掉，coinbase 多领 1,000 聪手续费。

之后矿工按 `mine` 反复修改 nonce 和 extra nonce。如果别人先出了块，矿工会基于新链顶重建候选区块；新块若已包含 tx，tx 就从矿工的交易池消失，否则仍留在交易池等下一块。

### ⑤ 区块传播与上链：唯一改变链上状态的一步

新区块通过区块头通告或紧凑区块传播，各全节点运行 `validate_block_on_its_parent`，再由 `consider_best_chain` 决定是否切换链顶。只有区块被接到当前采用的链上，节点才真正修改 UTXO 集合，同时清理交易池：

```text
function connect_block(block, chain, mempool):
    chain.utxo.apply(block.delta)                 # 删除 A:0，新增 (tx,0) 与 (tx,1)
    for tx in block.transactions:
        mempool.remove(tx, reason=BLOCK)          # 已上链
        for input in tx.inputs:
            other = mempool.spender_of(input.previous_output)
            if other exists:                      # 交易池里与它冲突的交易已经不可能有效
                mempool.remove_with_descendants(other, reason=CONFLICT)
    notify(BLOCK_CONNECTED, block)
```

Bitcoin Core 的交易池正是这样处理新区块：先移除区块里的交易，再递归移除与它们冲突的交易及其后代。[Core 交易池](https://github.com/bitcoin/bitcoin/blob/v30.0/src/txmempool.cpp) 此后，任何其他引用 `A:0` 的交易，在这条链上都会因为找不到输入而无效。

### ⑥ 钱包：跟踪确认

钱包不主动“查询结果”，而是消费节点推来的链事件，更新自己的记录：

```text
wallet.on_block_connected(block):
    for tx in block.transactions:
        if wallet.involves(tx):
            wallet.mark_confirmed(tx, height=block.height)   # Alice：A:0 永久花掉，找零可用
                                                             # Bob：60,000 聪的输出有了确认

wallet.on_block_disconnected(block):                         # 链重组
    for tx in block.transactions:
        if wallet.involves(tx):
            wallet.mark_unconfirmed(tx)                      # 回到 0 确认，等待重新打包

wallet.on_tx_removed(tx, reason):
    if reason == CONFLICT:                                   # 与它冲突的交易已经上链
        wallet.mark_conflicted(tx)                           # 在当前链上不会再有效，除非重组撤销那笔交易
        wallet.unlock_inputs_not_spent_elsewhere(tx)
    if reason == REPLACED:                                   # 被费率更高的冲突交易挤出交易池
        wallet.mark_replaced(tx)                             # 通常不会上链，替换交易确认前仍无定论

function confirmations(tx):
    return chain.tip.height - wallet.height_of(tx) + 1      # 未上链时为 0
```

交易所在的区块高度为 `h`、当前链顶高度为 `H` 时，确认数就是 `H - h + 1`。Bob 要等几次确认，取决于这笔钱的金额和对方可能掌握的算力，前一节的概率表给出了量化依据。

### 偏离主流程的几种情况

| 情况 | 发生在哪一步 | 结果 | 钱包的处理 |
| --- | --- | --- | --- |
| 选不出足够的币 | ① | 交易根本没构造出来 | 提示余额不足 |
| 手续费率过高 | ② | `sendrawtransaction` 拒绝提交 | 修正费率后重建 |
| 费率低于节点门槛 | ② 或 ③ | 部分或全部节点不接纳、不转发 | 提高费率重新构造；原交易已进入部分交易池时，新交易按 RBF 替换它。也可以花费它的找零或收款输出，用一笔高费率子交易带动它（CPFP） |
| 冲突交易先进交易池 | ② 或 ③ | 不满足替换规则时被拒绝；满足时原交易被挤掉 | 标记为已替换 |
| 长期未被打包 | ③ 到 ④ | Bitcoin Core 默认在交易池中保留 336 小时，即 14 天，到期移除 | 钱包可重新广播或提高费率 |
| 冲突交易先上链 | ⑤ | tx 在当前链上无效，除非重组撤销了那笔冲突交易 | 标记为冲突，释放未被花费的输入 |
| 链重组撤销了包含 tx 的区块 | ⑤ 之后 | tx 回到交易池，等待重新打包；若新链里有冲突交易，tx 失效 | 确认数归零，或标记为冲突 |

交易池过期时间见 [Core 交易池参数](https://github.com/bitcoin/bitcoin/blob/v30.0/src/kernel/mempool_options.h)，替换和冲突处理见 [Core 替换策略](https://github.com/bitcoin/bitcoin/blob/v30.0/doc/policy/mempool-replacements.md) 与 [Core 交易池](https://github.com/bitcoin/bitcoin/blob/v30.0/src/txmempool.cpp)。

这张表也说明了为什么 Bob 不能只看“收到了交易”。在 ① 到 ④ 之间，付款只存在于钱包和交易池里，每个节点都可以按自己的策略丢弃或替换它；只有到了 ⑤，`A:0` 才在链上被花掉，此后的风险就只剩链重组，并且随确认数增加而迅速下降。

## 小结：回到账本的五个问题

| 问题 | Bitcoin 的机制 | 由谁检查 | 仍然依赖的前提 |
| --- | --- | --- | --- |
| 授权 | 私钥签名，签名覆盖输入引用和全部输出 | 每个全节点 | 私钥不泄露，签名算法安全 |
| 余额 | UTXO 集合，输入必须存在且未被花费，输出总额不超过输入总额 | 每个全节点 | 节点完整重放历史；SPV 钱包不做这一步 |
| 顺序 | 交易按区块和块内位置排序，分叉时跟随累计工作量最大的有效链 | 每个全节点按同一规则选链 | 诚实算力占多数，区块能及时传播 |
| 不可改 | 哈希链让篡改可被发现，工作量证明让重写历史要重做计算 | 每个全节点 | 改写成本随确认数增加，但新近的区块仍可能被重组 |
| 发行 | coinbase 领取的金额不超过补贴加手续费，补贴每 210,000 块减半 | 每个全节点 | 节点运行同一套共识规则 |

中心化账本把五个问题都交给银行。Bitcoin 把前两个和最后一个变成每个全节点都能独立执行的规则，把顺序和不可改交给算力竞争。代价同样清楚：确认需要等待，新近的付款可能被重组推翻，安全性依赖“诚实算力占多数”这一前提。掌握多数算力的攻击者可以双花自己的钱、重排或拒绝打包近期交易，却仍然无法伪造别人的签名，也无法让全节点接受超发的区块。[Rutgers 讲义：Bitcoin](https://people.cs.rutgers.edu/~pxk/classes/419/notes/bitcoin.html)

## 参考资料

- [Bitcoin: A Peer-to-Peer Electronic Cash System — Satoshi Nakamoto](https://bitcoin.org/bitcoin.pdf)：Bitcoin 原始论文，引言部分说明了可信第三方和双花问题。
- [How does Bitcoin work? — Bitcoin.org](https://bitcoin.org/en/how-it-works)：共享账本、交易与挖矿的简明介绍。
- [But how does bitcoin actually work? — 3Blue1Brown](https://www.3blue1brown.com/lessons/bitcoin/)：从朋友间的公共账本出发，逐步引出签名、分布式账本和工作量证明。
- [How the Bitcoin protocol actually works — Michael Nielsen](https://michaelnielsen.org/ddi/how-the-bitcoin-protocol-actually-works/)：从一个简单的数字货币逐步迭代，重新发明 Bitcoin，并解释每一步为什么需要。
- [Bitcoin — Rutgers CS 419 讲义](https://people.cs.rutgers.edu/~pxk/classes/419/notes/bitcoin.html)：从哈希指针、Merkle 树讲到 UTXO、累计工作量与 51% 攻击。
- [Bitcoin and Cryptocurrency Technologies — Narayanan 等](https://bitcoinbook.cs.princeton.edu/)：普林斯顿教材，第一章用中心化的 ScroogeCoin 引出去中心化的必要性。
- [Bitcoin Core v30.0](https://github.com/bitcoin/bitcoin/tree/v30.0)：本文数据结构与共识规则的实现参照，具体文件链接已附在相应段落。
- [Bitcoin Improvement Proposals](https://github.com/bitcoin/bips)：签名、见证数据、区块传播等协议的正式规范。
- [Double Spend Races — Cyril Grunspan、Ricardo Pérez-Marco](https://arxiv.org/abs/1702.02867)：双花竞赛的概率分析。
