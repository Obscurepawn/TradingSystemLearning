# 从离散ID到稠密ID：如何做高性能映射

在数据库、搜索引擎、推荐系统以及高频交易系统中，经常需要将分布在巨大整数空间中的离散 ID 转换为连续的整数 ID。

例如，一个交易系统可能接收到以下标的 ID：

```text
Original IDs: [10086, 999999, 42, 10086, 888888]
Dense IDs:    [    1,      3,  0,     1,      2]
```

经过转换后，所有不同的原始 ID 都被映射到 `[0, N)`，其中 N 是不同 ID 的数量。

这样做最大的价值是：**把原本基于哈希表的状态访问，转换为连续数组访问。**

```cpp
// 映射之前
auto& state = state_map[original_id];

// 映射之后
uint32_t id = mapper.lookup(original_id);
auto& state = states[id];
```

后续的订单簿、持仓、风控、特征计算等模块，都可以直接使用 Dense ID 访问数组。

那么，怎样才能让 ID 映射本身足够快？如果原始 ID 是随机分布的 `uint64_t`，是否有可能做到 O(1) 查询？如果愿意用更多内存换取更低延迟，又有哪些特殊的数据结构？

本文从基础算法出发，逐步分析 Radix Sort、HashMap、Bitmap + Rank、Minimal Perfect Hash，以及针对低延迟场景的 Cacheline Hash Table 和 k-Perfect Hash。

---

## 一、问题定义：我们真正需要什么？

给定一个包含 N 个不同 ID 的静态集合：

\[
S=\{x_0,x_1,\ldots,x_{N-1}\}
\]

希望构造映射：

\[
f:S\rightarrow\{0,1,\ldots,N-1\}
\]

满足：

1. **唯一性**：不同的原始 ID 映射到不同的 Dense ID。
2. **稠密性**：Dense ID 连续，不存在空洞。
3. **查询效率**：能够快速根据原始 ID 找到 Dense ID。
4. **可选的成员校验**：对于不属于集合的 ID，返回不存在。

这里需要区分两个完全不同的概念：

- **原始 ID 空间 U**：原始 ID 可能取值的范围。
- **实际 ID 数量 N**：集合中不同 ID 的数量。

例如，原始 ID 可以分布在整个 `int64_t` 非负空间，而系统实际只管理 100 万个标的。

此时：

\[
U=2^{63},\quad N=10^6
\]

Dense ID 只需要 20 bit 就能表示全部 100 万个编号，使用 `uint32_t` 已经足够。

另外，还需要明确一个重要的工程约束：

**是否允许 Dense ID 的具体编号发生变化？**

如果只要求不同 ID 具有唯一编号，那么任意排列都合法。但如果内部 ID 已经被订单簿、持仓和策略状态引用，新增标的时就不能随意改变已有编号。

这个约束对后面的 MPHF 方案尤其重要。

## 二、Radix Sort + 去重：适合一次性批量压缩

对于一批已经存在的整数 ID，最直接的方法是排序、去重，然后按顺序编号。

例如：

```text
原始 ID：
[10086, 999999, 42, 10086, 888888]

排序：
[42, 10086, 10086, 888888, 999999]

去重：
[42, 10086, 888888, 999999]

Dense ID：
  0     1        2       3
```

对于 32-bit 或 64-bit 整数，可以考虑使用 Radix Sort，而不是基于比较的 `std::sort`。

### 2.1 为什么 Radix Sort 适合整数 ID？

Radix Sort 不通过比较两个元素的大小完成排序，而是按固定宽度的 bit 分组处理。

例如，使用 LSD Radix Sort，每次处理 8 bit：

- 32-bit ID：需要 4 轮。
- 64-bit ID：需要 8 轮。
- 每一轮只需要构建 256 个 bucket 的直方图、计算前缀和、重新分配元素。

对于固定长度的整数：

\[
T(N)=O\left(\frac{w}{r}N\right)
\]

其中 w 是整数位宽，r 是每次处理的 bit 数。

当 w、r 固定时，复杂度就是 O(N)。

如果需要保持原始数组顺序，可以在排序时保留原始下标：

```cpp
struct Entry {
    uint64_t key;
    uint32_t original_pos;
};
```

完成排序与去重后，再按照 `original_pos` 写回对应的 Dense ID。

### 2.2 如何根据原始 ID 再次查询？

排序后可以保留数组：

```cpp
std::vector<uint64_t> unique_ids;
```

查询时：

```cpp
auto it = std::lower_bound(
    unique_ids.begin(),
    unique_ids.end(),
    original_id
);

if (it == unique_ids.end() || *it != original_id)
    return NOT_FOUND;

return static_cast<uint32_t>(
    it - unique_ids.begin()
);
```

因为数组下标就是 Dense ID，所以不需要额外存储映射关系。

但缺点同样明显：**单次随机查询需要 O(log N) 次比较，并可能访问多个 Cacheline。**

对于百万级、千万级的静态 ID 集合，如果查询不频繁，这是一个简单且内存效率不错的方案。

对于延迟极其敏感的行情处理热路径，它通常不是首选。

### 2.3 批量查询可以使用 Merge Join

如果一次需要转换几百万个 ID，可以对查询 ID 再次进行 Radix Sort，然后与 `unique_ids` 进行双指针归并。

排序之后，匹配阶段复杂度为：

\[
O(N+Q)
\]

其中 Q 是待查询 ID 数量。

这种方案将大量随机查询转换成顺序扫描，通常具有更好的内存访问局部性。

因此，Radix Sort 更适合**批量数据处理、离线构建索引、ETL 与回测预处理**，而不是单次低延迟查找。

## 三、HashMap：最通用的在线映射方案

如果原始 ID 可能动态加入，最自然的方案是 HashMap。

```cpp
std::unordered_map<uint64_t, uint32_t> mapper;

uint32_t get_or_create(uint64_t id) {
    auto it = mapper.find(id);

    if (it != mapper.end())
        return it->second;

    uint32_t dense = mapper.size();
    mapper.emplace(id, dense);

    return dense;
}
```

实际实现需要处理数量溢出、并发和插入异常等问题。

HashMap 的主要优势是：

- 平均 O(1) 查询。
- 支持动态插入。
- 可保持已有 ID 的编号稳定。
- 可以精确判断 Key 是否存在。

但传统 `std::unordered_map` 可能引入额外指针、节点分配和随机访存。

对于现代 CPU，查询耗时往往不主要来自哈希计算，而是来自 **Cache Miss 和内存访问依赖链**。

因此，高性能 C++ 系统通常更值得考虑开放寻址或 Flat HashMap，例如：

- `absl::flat_hash_map`
- `ankerl::unordered_dense`
- `folly::F14FastMap`

这类实现通常具有更紧凑的存储布局、更好的缓存局部性，并且部分实现会利用 SIMD 加速候选位置筛选。

不过，它们仍然需要处理哈希冲突，查询时可能产生不同长度的探测路径。

对于需要极端稳定尾延迟的系统，这一点值得关注。

## 四、Bitmap + Rank：用位图完成稠密编号

如果原始 ID 的数值范围比较小，可以使用一种非常巧妙的数据结构：Bitmap + Rank。

假设原始 ID 集合为：

\[
S=\{1,4,5,8\}
\]

构造位图：

```text
ID:       0 1 2 3 4 5 6 7 8 9
Bitmap:   0 1 0 0 1 1 0 0 1 0
```

其中 `bitmap[x] = 1` 表示 x 是一个合法的原始 ID。

定义：

\[
rank_1(x)=\sum_{i=0}^{x}bitmap[i]
\]

对于一个存在的 ID，其 Dense ID 就是：

\[
dense(x)=rank_1(x)-1
\]

结果：

```text
1 -> 0
4 -> 1
5 -> 2
8 -> 3
```

### 4.1 为什么多个 ID 不会映射成同一个 Dense ID？

如果直接计算 Rank，确实可能出现：

```text
rank(1) = 1
rank(2) = 1
rank(3) = 1
```

但关键在于，**只有 Bitmap 对应位置为 1 的 ID 才是合法输入**。

对于两个不同的合法 ID：

\[
x<y
\]

由于 `bitmap[y] = 1`：

\[
rank_1(y)-rank_1(x)
=\sum_{i=x+1}^{y}bitmap[i]\geq1
\]

因此：

\[
dense(y)>dense(x)
\]

这就证明了合法 ID 之间不可能发生冲突。

### 4.2 如何做到 O(1) 查询？

将 Bitmap 存储为 `uint64_t` 数组，并为每个 word 预先保存前缀计数。

查询时：

```cpp
uint64_t word = bits[x >> 6];
uint32_t offset = x & 63;

uint64_t mask = 1ULL << offset;

// 必须先检查成员是否存在
if ((word & mask) == 0)
    return NOT_FOUND;

// rank inclusive: 当前 bit 及之前的所有 bit
uint64_t lower = ~0ULL >> (63 - offset);

uint64_t rank =
    prefix[x >> 6] +
    std::popcount(word & lower);

return rank - 1;
```

一次查询主要包含：

- Bitmap word 读取。
- Prefix Sum 读取。
- 位运算。
- 一次 POPCNT。

没有哈希冲突，也不需要二分查找。

### 4.3 致命限制：内存取决于原始 ID 范围

假设原始 ID 是非负 `int64_t`，最大可能达到 `INT64_MAX`。

那么普通 Bitmap 需要：

\[
\frac{2^{63}}8=2^{60}\text{ bytes}=1\text{ EiB}
\]

即使实际上只有 100 万个不同 ID，仍然需要如此巨大的地址范围。

如果使用完整的 `uint64_t` 空间，则 Bitmap 的理论大小达到 2 EiB。

这意味着普通 Bitmap + Rank 只适合 ID 范围有限的场景。

对于分布在巨大整数空间中的 ID，可以进一步考虑 Roaring Bitmap、分层 Bitmap、Elias–Fano 等压缩结构。但这些数据结构的实际空间与查询复杂度依赖于 ID 分布，并不能保证随机 64-bit ID 仍然具有出色的空间效率。

## 五、Minimal Perfect Hash：静态集合的完美映射

对于随机分布在整个 64-bit 空间的静态 ID 集合，Minimal Perfect Hash Function（MPHF）是非常值得研究的方案。

其目标是构建：

\[
f:S\rightarrow[0,N)
\]

并保证集合中任意两个不同的 Key 都得到不同结果。

它同时满足：

- Perfect：不存在集合内部哈希冲突。
- Minimal：恰好使用 N 个编号。

最吸引人的一点是：**MPHF 不需要显式存储每个原始 ID 对应的 Dense ID。**

某些实现只需要每个 Key 约 2–3 bit 的辅助结构。

例如，对于 1 亿个不同 ID，2.4 bit/key 对应约 30 MB 的哈希结构。

### 5.1 核心原理：Bucket + Pilot

以 PTHash、PtrHash 使用的一类构造思想为例。

首先使用一个哈希函数把 Key 划分为 Bucket。

然后为每个 Bucket 寻找一个参数 `pilot`，使其中所有 Key 都能被映射到尚未占用的位置。

用一个简化公式表示：

\[
f(x)=(h(x)+pilot[bucket(x)])\bmod N
\]

假设有五个 Key：

```text
A, B, C, D, E
```

划分结果：

```text
Bucket 0: A, B
Bucket 1: C, D
Bucket 2: E
```

构建算法尝试不同的 pilot，直到每个 Bucket 中的所有 Key 都成功占据互不冲突的位置。

最后可能得到：

```text
A -> 0
B -> 2
C -> 3
D -> 4
E -> 1
```

真正需要保存的是一组 pilot 参数，以及构造算法所需的其他少量元数据，而不是完整的 Key → Value 表。

实际算法并不一定采用简单的加法位移，往往还会加入二次哈希、扰动和压缩编码，并使用更复杂的策略提高构建效率。

### 5.2 为什么 MPHF 能如此节省内存？

关键是两个条件：

**第一，Key 集合在构建时已经确定。**

算法可以离线搜索一组使所有已知 Key 不冲突的参数。

**第二，允许 Dense ID 使用任意排列。**

例如：

```text
A -> 3
B -> 0
C -> 2
D -> 1
```

只要不冲突且编号连续，就是合法映射。

MPHF 不需要保存用户事先指定的编号关系，而是可以自由选择最容易编码的排列。

这为大幅压缩哈希函数提供了空间。

### 5.3 MPHF 最大的限制：不能判断 Key 是否存在

MPHF 只保证对于构建集合中的 Key 不发生冲突。

如果传入一个未知 ID：

```cpp
uint32_t dense = mphf(unknown_id);
```

它仍然可能返回 `[0,N)` 中的某个结果。

因此，MPHF 本身并不是一个支持精确成员校验的 Dictionary。

如果需要校验，可以额外保存：

```cpp
std::vector<uint64_t> original_ids;
```

按照 MPHF 输出位置组织原始 ID：

```cpp
uint32_t dense = mphf(id);

if (original_ids[dense] != id)
    return NOT_FOUND;

return dense;
```

这样既支持 O(1) 映射，也支持 O(1) 的反向查找及精确成员校验。

代价是增加每个 Key 8 bytes 的存储空间。

### 5.4 为什么 MPHF 不一定是最快的？

MPHF 的 O(1) 仅表示查询步骤数量不随 N 增长，并不保证只有一次内存访问。

典型查询可能包含：

```text
计算 Hash
    |
定位 Bucket
    |
读取 Pilot
    |
计算 Slot
    |
可选 Remap
    |
访问 InstrumentState
```

CPU 需要在获得 pilot 后才能计算最终 Slot，之后才能访问对应的 InstrumentState。

这是一条存在依赖关系的执行链。

如果 Pilot、Remap 或最终状态数组发生 LLC Miss，就可能显著影响单次查询延迟。

因此，**MPHF 的空间效率非常出色，但极低延迟场景还需要评估依赖性访存次数。**

## 六、用空间换速度：比 MPHF 更激进的方案

如果目标不再是最小内存，而是尽可能降低单次映射延迟，那么可以放宽原本的设计约束。

尤其是可以放弃 Minimal，即允许哈希空间存在一定空洞。

### 6.1 Direct Addressing：最短查询路径

如果原始 ID 的范围足够小，最简单的方案反而可能最快：

```cpp
uint32_t dense = direct_map[original_id];
```

它不需要哈希计算，不需要冲突处理，甚至不需要额外的地址推导。

对于 16-bit 原始 ID：

```cpp
std::array<uint32_t, 65536> direct_map;
```

只需要 256 KiB 的映射表。

在缓存命中良好的情况下，这种方式非常有竞争力。

但如果原始 ID 是随机 `uint64_t`，完整 Direct Addressing 显然不可行。

如果 ID 包含可利用的结构，例如高位表示交易所、产品组，低位表示合约编号，也可以考虑位提取、分层数组或者分段索引，以减少实际需要分配的地址空间。

### 6.2 Cacheline-sized Hash Bucket

一个值得尝试的思路是：**将一次哈希查询所需的全部 Key 和 Value 放入同一个 Cacheline。**

例如在 64-byte Cacheline 中，放置 4 个映射：

```cpp
struct alignas(64) Bucket {
    uint64_t keys[4];
    uint32_t values[4];
    uint8_t count;
    uint8_t has_overflow;
};

static_assert(sizeof(Bucket) == 64);
```

查询流程：

```cpp
uint32_t lookup(uint64_t key) {
    uint64_t h = hash(key);
    const Bucket& b = buckets[h & mask];

    for (uint32_t i = 0; i < b.count; ++i) {
        if (b.keys[i] == key)
            return b.values[i];
    }

    return lookup_overflow(key);
}
```

这只是热路径示意，构建阶段需要保证容量、溢出处理及有效性检查都正确。

它的优势是：绝大多数查询可以在一次 Bucket 的 Cacheline 读取后完成 Key 匹配。

如果使用 SIMD，还可以同时比较多个 64-bit Key。

例如有 100 万个 Key，分配 200 万个 Bucket：

\[
\lambda=\frac{N}{B}=0.5
\]

在均匀哈希与泊松近似下，某个 Bucket 出现 5 个及以上 Key 的概率约为 0.017%。

这意味着大部分 Bucket 的容量都足够。

但对于 HFT，还有一个问题：**少量溢出可能形成尾延迟慢路径。**

因此，单纯依赖较低装载因子，并不能消除最坏情况下的哈希冲突。

### 6.3 k-Perfect Hash：保证每个 Bucket 最多 k 个 Key

如果已经知道完整的静态 Key 集合，能不能在构建阶段就保证没有 Bucket Overflow？

这正是 k-Perfect Hash（k-PHF）的思路。

假设每个 Bucket 可以容纳 4 个 Key。

构造一个函数：

\[
f:S\rightarrow[0,B)
\]

要求：

\[
\forall b,\quad |\{x\in S:f(x)=b\}|\leq4
\]

也就是说，允许多个 Key 哈希到同一个 Bucket，但每个 Bucket 最多容纳 4 个 Key。

查询时：

```text
Key
 |
k-Perfect Hash
 |
Bucket Address
 |
Load One Cacheline
 |
Compare 4 Keys
 |
Return Dense ID
```

这种方案的目标不是为每个 Key 分配独立的哈希 Slot，而是保证目标 Bucket 不会溢出。

2026 年的论文 *Non-Minimal k-Perfect Hashing: Tight Lower Bounds and an Application to Fast Static Hash Tables* 系统研究了这一方向。

研究表明，在允许一定空闲容量的条件下，可以用非常紧凑的 k-PHF 定位 Cacheline-sized Bucket。论文中的静态 Hash Set 在部分大规模测试场景中，相比其他被测方案取得了最高约 1.5 倍的加速。

需要注意，论文的测试对象主要是静态 Hash Set，不能直接将结果等同于 `uint64_t → uint32_t` 映射性能。

但其底层思路非常符合低延迟系统的优化目标：

**在构建阶段消除运行时的不确定性，用额外空间和构建成本换取稳定的热路径。**

### 6.4 Non-minimal Perfect Hash

相比 k-PHF，还有一种更直接的方法：

不要强求哈希结果严格落在 `[0,N)`，而是允许少量空洞。

例如：

```text
Minimal Perfect Hash:
100万个 Key -> [0, 1000000)

Non-minimal Perfect Hash:
100万个 Key -> [0, 1010000)
```

多出大约 1% 的 Slot，可能换取更简单的构建或查询逻辑。

PtrHash 提供的 `FastPtrHash` 就属于此类速度优先的选择，可以省去 Minimal 模式所需的部分 Remap 操作。

如果业务允许直接使用带空洞的内部 Slot：

```cpp
uint32_t slot = perfect_hash(id);
auto& state = states[slot];
```

就可以避免将结果再次映射成真正稠密的编号。

但如果系统要求所有内部 ID 必须严格连续，那么仍然需要额外映射。

在实际工程中，**连续编号本身不是目的，真正的目的是更快地定位状态、改善内存布局。**

如果牺牲 1% 的空间就能减少热路径开销，这通常是值得考虑的取舍。

---

## 七、真实高频交易系统如何使用稠密 ID？

高频交易系统经常需要为不同的标的维护大量状态：

```cpp
struct InstrumentState {
    OrderBook orderbook;
    Position position;
    RiskState risk;
    StrategyState strategy;
};
```

如果每次都使用交易所原始 ID 访问 HashMap，可能引入额外的缓存未命中。

因此，一种自然的工程设计是：

```text
Exchange Market Data
         |
         v
Exchange Security ID
         |
         v
ID Mapping Layer
         |
         v
Internal Dense ID
         |
         +----> OrderBook
         |
         +----> Position
         |
         +----> Risk Engine
         |
         +----> Strategy
```

系统在行情接入边界完成一次映射，后续组件全部使用内部 ID。

### 7.1 NASDAQ：协议直接支持数组索引

NASDAQ TotalView-ITCH 协议有一个 `Stock Locate` 字段。

这个字段是 2-byte 整数，其设计目的之一就是让接入方可以直接使用数组索引快速访问证券信息。

Stock Locate 会在交易日内保持稳定，但不保证跨交易日相同。

因此，行情处理程序可以采用：

```cpp
std::array<uint32_t, 65536> locate_to_dense;

void on_market_data(const Message& msg) {
    uint32_t id = locate_to_dense[msg.stock_locate];

    if (id == INVALID)
        return;

    states[id].on_update(msg);
}
```

这里直接数组索引通常比引入复杂 MPHF 更自然。

### 7.2 CME：需要处理离散 SecurityID

CME MDP 3.0 使用 `SecurityID`（FIX Tag 48）标识交易标的，在相关 Security Definition 中它是 `Int32` 类型。

协议并不要求其数值连续。

如果系统只订阅部分合约，可以为这些合约建立内部映射：

```text
CME SecurityID
      |
      v
Flat HashMap / MPHF
      |
      v
Internal Instrument ID
      |
      v
InstrumentState[]
```

如果订阅集合在交易 Session 内固定，MPHF 是合理候选。

如果需要动态新增、删除合约，或者内部 ID 必须长期稳定，那么 Flat HashMap 或带稳定内部编号的映射层往往更容易维护。

目前没有充分公开证据证明 Jump Trading、Jane Street 或 Citadel Securities 等机构普遍使用 MPHF 完成标的 ID 映射。以上是根据公开协议和低延迟系统原理得到的工程设计分析，而不是对某家机构内部实现的断言。

### 7.3 高频交易场景真正重要的是什么？

对于 HFT，映射算法本身可能只占整个行情处理链的一小部分。

例如：

```cpp
uint32_t id = mapper.lookup(exchange_id);

auto& book = books[id];

book.update(price, quantity);
```

即使 `lookup()` 只消耗几个纳秒，后续 OrderBook 更新仍然可能遭遇 Cache Miss。

因此需要从完整执行链考虑：

- 映射表是否能够驻留 L1/L2 Cache？
- 查询是否存在串行依赖的随机访存？
- InstrumentState 的布局是否连续？
- 热门标的是否具有更好的缓存局部性？
- 是否存在偶发的冲突探测、分页、TLB Miss？
- 是否可以在输入边界完成映射，避免后续组件重复查找？

此外，交易系统还应当考虑多交易所 ID 命名空间、会话更新、标的上下线，以及新旧映射版本切换。

单纯把哈希查询优化到最快，不一定能让整条交易链路最快。

## 八、性能对比：应该怎样选择？

下面是各类方案的核心取舍。

| 方案 | 查询复杂度 | 内存特征 | 是否适合动态更新 |
|---|---|---|---|
| Radix Sort + Binary Search | O(log N) | O(N)，连续存储 | 不适合 |
| Flat HashMap | 平均 O(1) | O(N)，存在额外容量 | 适合 |
| Bitmap + Rank | O(1) | O(U) | 更新 Rank 索引较复杂 |
| MPHF | O(1) | 每 Key 几 bit 的辅助结构 | 通常需要重建 |
| Non-minimal PHF | O(1) | 少量额外 Slot | 通常需要重建 |
| Cacheline Hash Bucket | 平均 O(1) | 较高空间开销 | 取决于实现 |
| k-Perfect Hash | O(1) | Bucket + 哈希辅助结构 | 通常需要重建 |
| Direct Addressing | O(1) | O(U) | 可支持 |

这里还有一个重要的性能认知：

**理论复杂度相同的数据结构，实际查询延迟可能差距很大。**

例如，一次 Flat HashMap 查询可能命中一个 Cacheline，也可能产生多次探测。

一次 MPHF 查询可能只需要访问一个很小的 Pilot Table，但最终访问 InstrumentState 时仍会产生随机访存。

一次 Direct Addressing 虽然只需要数组索引，但如果映射数组极其庞大，也可能带来 TLB Miss 和缓存污染。

因此不能只比较哈希计算的指令数，也不能只比较单次平均吞吐。

### 8.1 PtrHash 的实测参考

PtrHash 项目的公开 Benchmark，在 `NoHash` 配置下报告了以下平均吞吐结果：

| Key 数量 | FastPtrHash | DefaultPtrHash |
|---|---:|---:|
| 10 万 | 1.5 ns/key | 1.7 ns/key |
| 100 万 | 2.2 ns/key | 2.6 ns/key |
| 1000 万 | 3.1 ns/key | 3.3 ns/key |
| 1 亿 | 7.6 ns/key | 8.5 ns/key |

这些数据说明现代 MPHF 可以非常快。

但表中的数值是特定硬件和循环测试下的平均吞吐成本，不等于一次存在数据依赖的查询延迟，也不能直接视为 p99 或 p999。

真正用于 HFT 时，需要测试完整热路径。

### 8.2 建议的 Benchmark

构建相同的 `uint64_t → uint32_t` 静态映射集合，比较：

- `absl::flat_hash_map`
- `ankerl::unordered_dense`
- `PTHash`
- `FastPtrHash`
- 自定义 Cacheline Hash Bucket
- k-Perfect Hash

建议覆盖不同规模，例如 1,000、10,000、100,000、1,000,000 个 Key。

访问分布至少包含：

1. Uniform Random：均匀随机访问。
2. Zipf：模拟少数热门标的被反复访问。
3. Mixed Lookup：混合存在和不存在的 Key。
4. Realistic Hot Path：映射后立即访问或修改订单簿状态。

测试不仅要记录平均时间，还应关注 p99、p999、Cache Miss、TLB Miss、分支预测失败与关键路径依赖。

特别要区分：

- **Latency Benchmark**：串行查询，下一次请求依赖上一次结果。
- **Throughput Benchmark**：多个独立查询并发执行，允许 CPU 重叠内存访问。

后者的 ns/key 可以非常低，但不一定代表真实行情链路的单次处理延迟。

## 九、最终结论

将离散 ID 转换为稠密 ID，并不存在对所有场景都最优的算法。

如果是离线批量转换，Radix Sort + 去重足够简单高效。

如果需要动态插入，Flat HashMap 通常是最合理的基线。

如果原始 ID 的数值范围有限，Bitmap + Rank 或 Direct Addressing 可以实现非常高效的 O(1) 查询。

如果原始 ID 随机分布在巨大 64-bit 空间，且集合静态，MPHF 可以利用极少量辅助内存完成无冲突映射。

而如果目标是 HFT 这样的极低延迟系统，并且愿意用更多内存换取速度，就值得进一步考虑 Non-minimal Perfect Hash、Cacheline Hash Bucket 和 k-Perfect Hash。

从系统设计角度，我认为最重要的三个原则是：

**第一，把映射尽可能放在系统边界。** 外部 ID 进入系统时完成一次转换，内部组件尽量始终使用紧凑的整数编号。

**第二，优化串行依赖的内存访问，而不是只优化算法复杂度。** O(1) 并不等于最低延迟，一次额外的 LLC Miss 可能抵消很多指令级优化。

**第三，不要为了严格稠密而牺牲关键路径性能。** 如果少量空槽能够减少一次 Remap 或一次依赖访存，那么这种非完全稠密的设计可能更适合低延迟交易系统。

最终，问题不应该只是：

“如何把任意 64-bit ID 映射到 `[0,N)`？”

而应该进一步变成：

**“在给定的静态 Key 集合、访问分布和内存预算下，如何以尽可能少的依赖性访存，定位到对应的业务状态？”**

当问题被重新定义到这一层，Direct Addressing、Perfect Hash、Cacheline Bucket 和 k-Perfect Hash 的设计取舍就会更加清晰。

---

## 参考资料

1. [PTHash — C++ Minimal Perfect Hash Library](https://github.com/jermp/pthash)
2. [PtrHash — Minimal Perfect Hashing at RAM Throughput](https://github.com/RagnarGrootKoerkamp/PtrHash)
3. [Modern Minimal Perfect Hashing: A Survey](https://arxiv.org/abs/2506.06536)
4. [Non-Minimal k-Perfect Hashing: Tight Lower Bounds and an Application to Fast Static Hash Tables (ESA 2026)](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.ESA.2026.20)
5. [NASDAQ TotalView-ITCH Protocol Specification](https://nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQTVITCHSpecification.pdf)
6. [CME MDP 3.0 Security Definition](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457327569/MDP+3.0+-+Market+Data+Security+Definition+-+Futures)
