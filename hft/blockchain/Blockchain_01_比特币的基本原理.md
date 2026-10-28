# 彻底理解加密货币系统原理（一）：Bitcoin 是怎样运行的

在银行 App 里给别人转账，最直观的结果是双方余额发生变化。支撑这件事的是银行维护的账本：谁支付了多少，谁收到了多少。

Bitcoin 也要把这些事情记清楚，但它没有一家负责维护总账的银行。一批通过互联网连接的计算机共同运行这套系统：接收付款记录，检查记录是否有效，再按照相同的规则更新各自的账本。

金额以聪为单位，**1 BTC 等于 100,000,000 聪**。

## 账本、区块和链是什么

先看一笔付款最直观的意思：Alice 向 Bob 支付了 60,000 聪。Bob 要能放心使用这笔钱，账本就得记录这次转移，并且阻止 Alice 把同一份钱再花一次。对 Bitcoin 来说，“我有一些币”对应的是账本里存在一些尚未花费、且我有权支出的金额。

运行 Bitcoin 软件、参与网络通信的计算机叫作**节点**。其中，全节点按完整规则检查账本内容，维护自己认可的账本状态。节点收到新数据后会检查并转发给邻居，记录由此在网络中传播。它们各自维护副本，网络延迟会使不同节点在短时间内看到不同的最新记录。[Bitcoin.org：Bitcoin 如何运行](https://bitcoin.org/en/how-it-works)

付款记录以**交易**的形式提交给网络，再被分批组织成**区块**。一个区块可以看成一页已经排好顺序的账页：里面装着一批交易，页首还有描述这批记录的信息，这部分称为**区块头**。区块里可能有 Alice 给 Bob 的付款，也可能有其他人之间完全无关的付款。

这些“账页”通过**哈希**连接。哈希是对一段数据计算得到的固定长度摘要：同样的数据总会得到同样的摘要，内容变化后，摘要几乎一定随之变化。可以把它当成用于核对内容的数字指纹。

区块头既保存由本块交易计算出的汇总摘要，也保存前一个区块头的哈希。假设当前已有 A、B、C 三个相邻区块，B 保存 A 的摘要，C 保存 B 的摘要。沿着这些引用向前追溯，最终会到达第一个区块，也就是创世区块。这种相互引用形成的数据结构，就是**区块链（blockchain）**。

![区块链基础：区块 B 保存 A 的区块头摘要，C 保存 B 的区块头摘要；每个区块收录一批交易，链随新区块加入而增长](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/blockchain/assets/bitcoin-blockchain-basics.svg)

假如有人修改 A 中的付款，A 的区块头摘要会随之改变，B 里保存的旧摘要就对不上了。若他连 B 的引用一起改掉，B 的摘要也会变化，又会影响 C。链把前后记录关联起来，让修改能够被发现。[区块链的数据结构](https://github.com/bitcoin-dot-org/developer.bitcoin.org/blob/master/devguide/block_chain.rst)

不过，任何人都能复制这些数据，再重新计算摘要。Bitcoin 还要决定谁提出下一页账本，以及遇到两份冲突历史时采用哪一份。**矿工**负责把交易组装成候选区块，并反复计算，寻找满足网络要求的结果；这种结果叫作**工作量证明（PoW）**。全节点检查候选块是否符合规则，在有效分支中选择累计工作量最大的一条。

于是，一笔付款会经历几个不同的状态：钱包发出交易，节点收到它，矿工把它放进区块，区块被节点验证并接到当前采用的链上。到这一步，付款获得一次**确认**。此后每增加一个后继区块，确认数再加一。仅仅收到一条付款消息，还没有走完这个过程。

## 钱包、私钥、公钥与签名

用户通过钱包收付款。**钱包**是一种管理密钥、生成收款地址、构造交易的软件。账本记录公开可查，又没有银行柜员核对付款人的身份，节点怎样知道“Alice 付钱给 Bob”确实经过 Alice 同意？Bitcoin 用的是数字签名。

以用户自己掌握密钥的钱包为例，钱包会管理一组密钥。**私钥**是一段秘密数值，钱包用它为付款生成签名；**公钥**可以由私钥计算出来，用于核对签名。公钥可以公开，持有公钥的人能够验证签名，却无法在可行的计算成本内据此反推出私钥，或替持有人签出新的付款。[公钥、私钥与签名](https://github.com/bitcoin/bips/blob/master/bip-0137.mediawiki#background-on-ecdsa-signatures)

**数字签名**同时依赖私钥和被签署的内容。它不是每次都贴上去的同一张签名图片。Alice 签署的内容包括 Bob 的收款条件和金额；这些内容被改动后，原签名就无法通过验证。

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

签名也没有替节点决定付款的先后。Alice 完全可以给两笔冲突的付款各签一次名：一笔把钱付给 Bob，另一笔把同一份钱付给自己。两份签名都来自 Alice，但这份钱只能花一次。节点要结合账本中的已有记录，判断哪笔交易还有权花费它。

## 一笔交易改变了什么

钱包显示的是一个余额数字，全节点核对的则是一份份具体的可花费记录。这些记录叫作 UTXO，即 **Unspent Transaction Output，尚未花费的交易输出**。普通交易消耗一个或多个旧输出，再创建新的输出，分别指定金额和花费条件。

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

Alice 仍然能构造另一笔交易，引用 `A:0`，把钱转给自己。孤立地验证两笔交易的签名，都会成功。只有把它们放进同一个不断更新的 UTXO 状态，冲突才会显现。这也是 Bitcoin 需要全网共同历史的原因。

## 区块怎样固定一批交易

区块的主体保存交易列表，区块头则保存这批交易的摘要以及前一个区块头的哈希。交易列表怎样汇总成一个摘要？Bitcoin 使用 **Merkle 树**，最后得到的根哈希称为 Merkle root。

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

`target` 越小，命中的范围越窄。若把哈希输出看成均匀随机数，单次成功概率是 `(target + 1) / 2^256`，平均尝试次数是其倒数。

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

节点选择的是**累计工作量最大的有效链**。在难度相同的区段，块数更多就意味着工作量更多；跨越不同难度时，直接数块会得到错误答案。

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

## 参考资料

- [Bitcoin: A Peer-to-Peer Electronic Cash System — Satoshi Nakamoto](https://bitcoin.org/bitcoin.pdf)：Bitcoin 原始论文。
- [How does Bitcoin work? — Bitcoin.org](https://bitcoin.org/en/how-it-works)：共享账本、交易与挖矿的简明介绍。
- [But how does bitcoin actually work? — 3Blue1Brown](https://www.3blue1brown.com/lessons/bitcoin/)：从账本和签名逐步引出工作量证明的图解。
- [Bitcoin Core v30.0](https://github.com/bitcoin/bitcoin/tree/v30.0)：本文数据结构与共识规则的实现参照，具体文件链接已附在相应段落。
- [Bitcoin Improvement Proposals](https://github.com/bitcoin/bips)：签名、见证数据、区块传播等协议的正式规范。
- [Double Spend Races — Cyril Grunspan、Ricardo Pérez-Marco](https://arxiv.org/abs/1702.02867)：双花竞赛的概率分析。
