# HFT Engineering从入门到入土（2）—— 访存与计算的极致Latency优化

目前主流的HFT系统普遍都已经做到了亚微秒交易的Latency级别，在这种情况下，我们的编程技巧将和实现常见的high throughput system的有很大的不同，因为我们将需要从计算、访存、IO去抠指令级、ns级的Latency Reduce。

下文将从进程内的访存和计算进行分析，阐述一下HFT System中常见的编程技巧（IO相关的优化会在下一篇文章介绍）。

本文所列举的优化手段主要参考了 [Building Low Latency Applications with C++](https://github.com/PacktPublishing/Building-Low-Latency-Applications-with-CPP)和 [C++ design patterns for low-latency applications including high-frequency trading](https://arxiv.org/abs/2309.04259)，下文所有Demo使用 C++20 进行实现。

RDTSC 的指令开销数据参考 [uops.info 的测量结果](https://uops.info/html-instr/RDTSC.html)。

## 1. 先定义要优化的延迟

### 1.1 处理速度和响应时间是两件事

假设一次函数调用只花了很短时间，但消息在队列中已经等了很久。只测函数内部，会得到一个漂亮却不完整的结果。

对进程内的一条消息，可以先拆成：

```text
消息进入进程 ── 排队等待 ── 开始处理 ── 业务计算与编码 ── 交给发送接口
                    │                      │
                 排队延迟                服务时间
└────────────────────── 进程内响应时间 ──────────────────────┘
```

这个终点还不包含网卡实际发出、网络传播和交易所处理。测量时必须把起止点写清楚。

吞吐量回答一秒处理多少消息，延迟回答一条消息要等多久。批量处理可能提高吞吐量，却让第一条消息等得更久；增加工作线程可能分摊计算，也可能增加队列与缓存一致性开销。因此，至少应同时观察吞吐量、p50、p99、p99.9，以及测试期间的最慢样本。平均值会掩盖少量慢请求。

下面是一个最小的延迟记录器。初始化时预留空间，采样只追加整数，排序放到测量结束以后；超过容量会记录丢失的样本数。

```cpp
#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>

class LatencySamples {
    std::vector<std::int64_t> ns_;
    std::size_t limit_;
    std::size_t dropped_ = 0;
public:
    explicit LatencySamples(std::size_t limit) : limit_(limit) {
        ns_.reserve(limit);                         // 初始化阶段
    }

    void record(std::chrono::steady_clock::time_point begin,
                std::chrono::steady_clock::time_point end) {
        if (ns_.size() == limit_) { ++dropped_; return; }
        ns_.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(
                          end - begin).count());
    }

    // 停止采样后调用。nearest-rank 百分位；空样本没有百分位。
    std::int64_t percentile(unsigned permille) {
        if (ns_.empty() || permille == 0 || permille > 1000)
            throw std::invalid_argument("invalid percentile or empty samples");
        std::sort(ns_.begin(), ns_.end());
        const auto n = ns_.size();
        const auto rank = (n / 1000) * permille +
                         ((n % 1000) * permille + 999) / 1000;
        return ns_[rank - 1];
    }

    std::size_t dropped() const { return dropped_; }
};

// 用法：record(arrival_time, send_time)；结束后读取 percentile(990)。
```

计时和记录样本本身也占用时间。上面的记录器用于观察消息延迟分布；测量几纳秒级操作时，应在一个计时区间内重复执行，并单独评估计时开销。线上采样还需要记录丢样数量和负载变化。

<a id="prepare"></a>

## 2. 把工作移出关键路径

### 2.1 能提前算出的值，不必随消息重算

协议字段宽度、固定换算系数、消息类型对应表，通常在编译时或启动时就能确定。这里要区分两个时机：真正的常量可以在编译期生成；来自配置文件的参数，应在初始化阶段解析和校验。

例如，按十进制精度取得缩放系数：

```cpp
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>

// Bad case：每次调用都重新表达一遍相同计算。
std::optional<std::uint64_t> scale_bad(unsigned decimals) {
    if (decimals > 9) return std::nullopt;
    std::uint64_t scale = 1;
    for (unsigned i = 0; i < decimals; ++i) scale *= 10;
    return scale;
}

constexpr auto make_scales() {
    std::array<std::uint64_t, 10> table{};
    table[0] = 1;
    for (std::size_t i = 1; i < table.size(); ++i)
        table[i] = table[i - 1] * 10;
    return table;
}

// constexpr 变量的初始化要求常量求值。
inline constexpr auto scales = make_scales();

std::optional<std::uint64_t> scale_good(unsigned decimals) {
    if (decimals >= scales.size()) return std::nullopt;
    return scales[decimals];
}

static_assert(scales[4] == 10'000);
```

`constexpr` 函数表示它**可以**用于常量求值。拿运行时输入调用它，仍然可能执行运行时计算；反过来，普通函数遇到可见的常量输入，也可能被编译器常量折叠。

查表同样有成本。小表常驻缓存时容易受益，大表可能把原来的几条算术指令换成一次缓存未命中。先消除重复配置处理和重复解析，再考虑是否需要查表。

### 2.2 配置固定时，用静态分派暴露具体实现

每条消息都经由虚函数或 `std::function` 调用策略，编译器未必能看到实际目标。对于启动后不变的行为，可以把选择放在外层，让内层处理具体类型。

```cpp
#include <cstdint>

struct Quote { std::int64_t bid_ticks, ask_ticks; };

// Bad case：对于已经固定的策略，仍逐条做动态分派。
struct DynamicRule {
    virtual bool accept(const Quote&) const noexcept = 0;
    virtual ~DynamicRule() = default;
};
bool check_bad(const DynamicRule& rule, const Quote& q) {
    return rule.accept(q);
}

struct SpreadRule {
    bool accept(const Quote& q) const noexcept {
        return q.bid_ticks < q.ask_ticks;
    }
};

// Good case：具体类型可见，编译器有机会内联并继续优化。
template<class Rule>
bool check_good(const Rule& rule, const Quote& q) noexcept {
    return rule.accept(q);
}

bool example() {
    return check_good(SpreadRule{}, Quote{100, 101});
}
```

目标函数可见以后，除了省去间接跳转，编译器还有机会继续做常量传播、删除无用分支。实现这种静态分派，一个普通模板函数就够了。

插件和运行时替换适合保留动态分派；目标可推断时，编译器也可能自行去虚拟化。静态分派的代价是模板实例化带来的代码增长，过多实例会增加指令缓存压力。是否发生内联，则要看优化后的代码：`inline` 关键字本身不决定机器码的调用方式。

<a id="memory"></a>

## 3. 管住内存分配、复制和查找

### 3.1 预分配，并且明确容量用完以后怎么办

动态内存分配的问题不仅是平均耗时，还包括分配器内部竞争、扩容搬迁和首次触页。低延迟路径通常会在启动时准备好存储空间，在处理时复用。

```cpp
#include <cstddef>
#include <cstdint>
#include <span>
#include <vector>

struct Update { std::uint32_t instrument; std::int64_t price_ticks; };

// Bad case：每批重新建立容器，增长期间可能多次扩容。
std::vector<Update> collect_bad(std::span<const Update> input) {
    std::vector<Update> out;
    for (const auto& u : input) out.push_back(u);
    return out;
}

class BatchBuffer {
    std::vector<Update> data_;
    std::size_t limit_;
public:
    explicit BatchBuffer(std::size_t limit) : limit_(limit) {
        data_.reserve(limit);                      // 启动时分配
    }
    // input 不得引用本对象 data_ 内的元素。
    bool collect(std::span<const Update> input) {
        if (input.size() > limit_) return false;   // 超限交给调用方处理
        data_.clear();                            // 保留 capacity
        for (const auto& u : input) data_.push_back(u);
        return true;
    }
    // 返回视图只在下一次成功 collect() 或对象销毁之前有效。
    std::span<const Update> view() const { return data_; }
};
```

`reserve()` 改变容量，不会创建相应数量的元素，所以不能在空 `vector` 上直接写 `data_[i]`。超过预留容量以后，`push_back()` 仍可扩容；示例把业务上限单独保存，就是为了避免不知不觉退回动态分配。

容量也不能只按平均流量估算。行情突发时，满队列、满对象池和批次超限，都必须有明确结果：重试、背压、拒绝，或按业务允许的规则丢弃。容量越大也未必越好，工作集变大可能降低缓存命中率。

### 3.2 频繁创建的小对象，可以改为固定槽位池

订单、订单簿节点和内部消息常常有已知的数量上限。与其每次 `new`、结束时 `delete`，可以预先放好一组对象，再用空闲链表管理槽位。

![固定槽位池：申请从空闲链表摘下一个槽位，归还时放回链表头](assets/hft-02/02-slot-pool.svg)

*图 1：空闲链表只保存索引。申请和归还都修改少量元数据，不扫描全部槽位。*

下面是一个**单线程、固定类型**的槽位池。它复用的是已经存在的 `Order` 对象，不涉及通用 placement new 或复杂析构。

```cpp
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>

struct Order { std::uint64_t id; std::int64_t price_ticks; };

template<std::size_t N>
class OrderPool {
    static_assert(N > 0);
    std::array<Order, N> objects_{};
    std::array<std::size_t, N> next_{};
    std::array<bool, N> used_{};
    std::size_t head_ = 0;                         // N 表示空链表
public:
    OrderPool() {
        for (std::size_t i = 0; i < N; ++i) next_[i] = i + 1;
    }

    std::optional<std::size_t> acquire(Order value) noexcept {
        if (head_ == N) return std::nullopt;
        const auto i = head_;
        head_ = next_[i];
        objects_[i] = value;                       // 填完整的新订单
        used_[i] = true;
        return i;
    }

    Order* get(std::size_t i) noexcept {
        return i < N && used_[i] ? &objects_[i] : nullptr;
    }

    bool release(std::size_t i) noexcept {
        if (i >= N || !used_[i]) return false;
        used_[i] = false;
        next_[i] = head_;
        head_ = i;
        return true;
    }
};

bool pool_example() {
    OrderPool<8> pool;
    const auto handle = pool.acquire(Order{7, 10'005});
    if (!handle) return false;
    pool.get(*handle)->price_ticks = 10'006;
    return pool.release(*handle);
}
```

与逐次分配相比，这个版本把存储成本放到了初始化阶段，并且让申请、归还都只需固定数量的操作。池耗尽会返回空结果，而不是覆盖活跃对象。

但整数索引不是永久身份：归还后，旧索引可能指向另一笔新订单。示例约定调用方归还后不再使用索引或指针；需要检测陈旧句柄时，应增加 generation 并校验。包含字符串、文件句柄等资源的对象还需要单独设计生命周期，不能照搬这个 POD 示例。

如果通过扫描数组寻找空槽，最坏仍要遍历整个池。空闲链表把这次扫描变成了固定数量的索引操作，因此这里的申请和归还都是 O(1)。

### 3.3 用视图避免复制，同时守住数据的生命周期

解析消息时，先把缓冲区复制成字符串，再切出字段字符串，是很常见的额外工作。`std::string_view` 和 `std::span` 可以表示一段已有数据，`std::from_chars` 可以直接在字符区间上解析整数。

```cpp
#include <charconv>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <system_error>

// Bad case：为已有字符再构造一份拥有存储的 string。
std::optional<std::uint32_t> parse_bad(std::string_view field) {
    if (field.empty()) return std::nullopt;
    const std::string copy(field);                 // 长字段可能分配
    std::uint32_t value{};
    const auto [p, ec] = std::from_chars(
        copy.data(), copy.data() + copy.size(), value);
    if (ec != std::errc{} || p != copy.data() + copy.size())
        return std::nullopt;
    return value;
}

std::optional<std::uint32_t> parse_good(std::string_view field) {
    if (field.empty()) return std::nullopt;
    std::uint32_t value{};
    const auto [p, ec] = std::from_chars(
        field.data(), field.data() + field.size(), value);
    if (ec != std::errc{} || p != field.data() + field.size())
        return std::nullopt;
    return value;                                  // 返回拥有自身存储的值
}
```

两版都检查格式、越界和未消费完的字符，区别只在是否复制输入。短字符串可能使用 SSO，不一定发生堆分配，但复制本身仍然存在。

视图不会延长原缓冲区的寿命。网络接收缓冲区下一轮被覆盖后，视图的地址可能仍有效，内容却已经不是原来的消息；缓冲区释放或重新分配后，视图才会真正悬空。如果消息要交给另一线程，应在边界上提取成有所有权的小结构体，或移交整个缓冲区的所有权。不能因为追求“零拷贝”而让消费者读到被覆盖的数据。

这条原则也适用于 `const T&`、裸指针和 `span`：减少复制要和所有权设计一起完成。

### 3.4 返回值交给编译器，避免无意义的 `std::move`

减少复制不等于给每个变量加上 `std::move`。返回局部变量时，多写一个 move 反而可能妨碍命名返回值优化（NRVO）。

```cpp
#include <cstddef>
#include <cstdint>
#include <utility>
#include <vector>

std::vector<std::uint64_t> make_bad(std::size_t n) {
    std::vector<std::uint64_t> values(n, 7);
    return std::move(values);                      // 阻止此处的 NRVO
}

std::vector<std::uint64_t> make_good(std::size_t n) {
    std::vector<std::uint64_t> values(n, 7);
    return values;                                // 可 NRVO，否则可隐式移动
}
```

编译器可以对命名局部变量执行 NRVO；C++17 对直接返回同类型纯右值则另有保证的复制消除规则。这里的 `vector` 移动通常很便宜，因此两版的实际差距可能很小，直接返回局部变量仍是更自然的写法。其他类型的移动成本取决于其实现，`std::move` 本身只转换表达式的值类别。

### 3.5 对稠密、有界的键，直接索引往往比通用查找合适

外部订单号和证券代码可能很稀疏，但进入内部系统以后，可以映射成紧凑编号。若编号范围已知，数组访问能省去哈希计算、桶访问和节点间接寻址。

```cpp
#include <array>
#include <cstdint>
#include <unordered_map>

struct State { std::int64_t position = 0; };

struct MapStates {
    std::unordered_map<std::uint32_t, State> states;
    State* find(std::uint32_t id) {
        auto it = states.find(id);                 // 稠密小整数也要做通用查找
        return it == states.end() ? nullptr : &it->second;
    }
};

struct DenseStates {
    std::array<State, 4096> states{};
    std::array<bool, 4096> active{};
    State* find(std::uint32_t id) {
        if (id >= states.size() || !active[id]) return nullptr;
        return &states[id];
    }
};
```

这个例子只比较**已知稠密编号上的查找**。若直接拿 64 位交易所订单号做下标，数组可能大得无法接受。通用哈希表也有合适的场景；预留桶可以减少 rehash，但并不保证消除每个节点的分配。

价格索引也要遵循相同约束。`price % capacity` 会把不同价格映射到同一槽位，需要处理冲突；若交易价格被限制在一个小窗口内，则可以先检查范围，再用 `price - base_price` 直接定位。

<a id="layout"></a>

## 4. 按访问方式组织数据

### 4.1 先减少工作集，再谈缓存命中

CPU 从内存搬运数据时，以缓存行为单位。一次只用几个字节，也可能把附近暂时无用的字段带进缓存。低延迟代码中，数据布局经常比某条算术指令更值得优化。

先看结构体填充：

```cpp
#include <cstdint>

struct OrderBad {
    std::uint8_t side;
    std::uint64_t id;
    std::uint32_t qty;
    std::uint64_t price_ticks;
};

struct OrderGood {
    std::uint64_t id;
    std::uint64_t price_ticks;
    std::uint32_t qty;
    std::uint8_t side;
};
```

在常见的 64 位 ABI 上，两者通常分别占 32 和 24 字节，具体以目标平台的 `sizeof` 为准。重新排列字段减少填充，但不改变字段含义。不要为了省字节到处 `#pragma pack(1)`：非对齐访问可能增加成本，甚至在某些目标上受到限制；网络报文布局也应与内部计算布局分开。

如果一个订单对象还挂着几十字节的备注、字符串和历史统计，仅调整顺序不够。应进一步考虑把经常一起访问的字段放在一起，把很少访问的字段移到旁边。

![AoS、冷热分离与 SoA：不同访问方式对应不同布局](assets/hft-02/01-data-layout.svg)

*图 2：按订单处理时可以保留紧凑结构体；只扫描数量时，连续的数量数组能避免带入无关字段。图中格子表达布局关系，不代表精确字节数。*

```cpp
#include <array>
#include <cstdint>
#include <string>

struct WholeOrder {
    std::uint64_t id;
    std::int64_t price_ticks;
    std::uint32_t qty;
    std::string note;                              // 扫描价格时不需要
    std::array<std::uint64_t, 8> history;
};

struct HotOrder {
    std::uint64_t id;
    std::int64_t price_ticks;
    std::uint32_t qty;
    std::uint32_t cold_index;
};

struct ColdOrder {
    std::string note;
    std::array<std::uint64_t, 8> history;
};
```

冷热分离让热点循环少搬运无关数据，代价是访问冷字段时需要额外定位。是否划算，取决于热字段和冷字段各自的访问频率。

### 4.2 顺序访问与 SoA：让要处理的字段连续排列

AoS（array of structures，结构体数组）适合一次处理一条记录；SoA（structure of arrays，把各字段分别存成数组）适合一次处理同一个字段。比如一个批次只统计数量是否超过阈值，SoA 可以只读数量数组。

```cpp
#include <cstddef>
#include <cstdint>
#include <span>
#include <vector>

struct Level {
    std::int64_t price_ticks;
    std::uint32_t qty;
    std::uint32_t order_count;
};

std::size_t count_aos(std::span<const Level> levels, std::uint32_t limit) {
    std::size_t count = 0;
    for (const auto& level : levels) count += (level.qty > limit);
    return count;
}

struct LevelsSoA {
    std::vector<std::int64_t> prices;
    std::vector<std::uint32_t> quantities;
    std::vector<std::uint32_t> order_counts;
};

std::size_t count_soa(std::span<const std::uint32_t> quantities,
                      std::uint32_t limit) {
    std::size_t count = 0;
    for (auto qty : quantities) count += (qty > limit);
    return count;
}
```

如果每次都同时读取价格、数量和订单数，AoS 往往更便于处理整条记录，也可能更快。SoA 适合同字段批量扫描，同时需要保持各数组的长度和索引一致。

相同原则也解释了为什么“小规模连续数组 + 扫描”有时优于复杂的树和链表：后者虽然操作次数少，指针追踪却可能让 CPU 连续等待多个不相关地址。算法复杂度仍然重要，但要和工作集规模、访问模式一起看。

比较两种布局时，应让它们完成同样的访问任务。随机访问改成顺序访问后，硬件预取和局部性也随之改变，测出的差异就包含了多项因素。

### 4.3 预热与预取：把首次使用的工作提前

初始化阶段可以分配缓冲区、初始化查找表，并用没有外部副作用的测试数据跑一遍编码和计算路径。这可以减少首次使用才发生的工作，但初始化完成不代表数据从此一直留在 L1。

下面的例子在启动时实际写入要使用的缓冲区，而不是只保留一段尚未使用的容量：

```cpp
#include <cstddef>
#include <cstdint>
#include <vector>

struct ReceiveBuffer {
    std::vector<std::byte> bytes;
    std::vector<std::uint32_t> decode_table;

    ReceiveBuffer(std::size_t buffer_bytes, std::size_t symbols)
        : bytes(buffer_bytes, std::byte{0}), decode_table(symbols, 0) {
        // 实际应用在这里载入、校验配置并建立解码表。
    }
};
```

这一步提前完成缓冲区分配和初始化；后续的页面驻留、缺页和缓存命中情况仍受操作系统及实际负载影响。预热编码路径时，使用独立的测试状态，只运行编码和计算，将真实发单、持仓修改和业务序号消耗留给正式处理流程。

软件预取则是在运行过程中提示 CPU 提前取将来会用的数据。顺序遍历常常已有硬件预取；比较值得实验的是“下一批索引已知，但目标位置不连续”的访问。

```cpp
#include <cstddef>
#include <cstdint>
#include <span>

// 前提：ids 中每个下标均已在入口处检查，小于 values.size()。
std::uint64_t sum_indirect(std::span<const std::uint64_t> values,
                           std::span<const std::size_t> ids) {
    constexpr std::size_t distance = 16;
    std::uint64_t sum = 0;
    for (std::size_t i = 0; i < ids.size(); ++i) {
#if defined(__GNUC__) || defined(__clang__)
        if (ids.size() - i > distance)
            __builtin_prefetch(&values[ids[i + distance]], 0, 1);
#endif
        sum += values[ids[i]];
    }
    return sum;
}
```

`distance = 16` 只是实验起点，不是通用参数。太近可能来不及，太远可能提前逐出其他数据，地址计算本身也必须合法。只有与不预取的相同循环比较、并确认内存等待确实减少，才有理由保留它。

<a id="execution"></a>

## 5. 缩短实际执行的代码路径

### 5.1 先做便宜且容易拒绝的检查

一条订单需要通过多项检查。有些只读几个字段，有些要查表或遍历状态。如果前面的检查已经失败，后面的计算通常没有必要继续。

```cpp
#include <cstdint>
#include <span>

struct Request { std::uint32_t qty; };

bool account_open(std::span<const std::uint8_t> flags) {
    for (auto flag : flags) if (flag == 0) return false;
    return true;
}

// Bad case：数量已经不合法，仍然先扫描账户限制。
bool accept_bad(Request r, std::span<const std::uint8_t> flags) {
    const bool account_ok = account_open(flags);
    const bool quantity_ok = r.qty > 0 && r.qty <= 10'000;
    return account_ok && quantity_ok;
}

bool accept_good(Request r, std::span<const std::uint8_t> flags) {
    return r.qty > 0 && r.qty <= 10'000 && account_open(flags);
}
```

`&&` 和 `||` 有短路语义，`&` 和 `|` 没有。检查顺序应结合成本和失败概率安排；如果必须收集全部错误，或者检查函数还承担记录、扣减等副作用，就不能随意跳过。

调整检查顺序的收益取决于输入分布；编译器也可能已经消除了部分重复计算，因此还要对照优化后的代码。

### 5.2 错误消息等到出错时再构造

热路径里经常藏着这样的开销：检查本身很便宜，但调用检查函数之前，已经把错误字符串拼好了。

```cpp
#include <cstdint>
#include <stdexcept>
#include <string>

struct OrderCheck { std::uint64_t id; std::uint32_t qty; };

void require(bool ok, const std::string& message) {
    if (!ok) throw std::runtime_error(message);
}

void validate_bad(OrderCheck o) {
    require(o.qty != 0, "zero quantity, order=" + std::to_string(o.id));
}

#if defined(__GNUC__) || defined(__clang__)
[[gnu::cold, gnu::noinline]]
#endif
void reject_zero_qty(std::uint64_t id) {
    throw std::runtime_error("zero quantity, order=" + std::to_string(id));
}

void validate_good(OrderCheck o) {
    if (o.qty == 0) [[unlikely]] {
        reject_zero_qty(o.id);
    }
}
```

第二版把格式化和异常构造都留在失败分支里。`cold`、`noinline` 是 GCC/Clang 扩展，帮助编译器安排少用的代码；即使移除这些属性，成功路径也已经省掉了字符串构造。

这里用异常只是为了缩短错误处理示例。如果拒单属于正常输入，应返回错误码或错误事件，避免靠抛异常处理日常业务。也不要给可能抛异常的函数随意加 `noexcept`：它不会把异常处理变快，异常逃出时会终止程序。

`[[likely]]`、`[[unlikely]]` 和 `__builtin_expect` 都只是给编译器的概率提示，不会替 CPU 固定预测结果。分支频率与预期不符时，提示还可能有反作用。先把错误处理和格式化从常见路径移开，通常比到处标注 `likely` 更直接。

### 5.3 分支难预测时，再考虑用数据表达选择

假设成交方向已经校验，只可能是买入和卖出。可以把方向编码成 `+1`、`-1`，减少更新持仓时的选择。

```cpp
#include <cstdint>

enum class Side : std::int8_t { Buy = 1, Sell = -1 };

// 前提：side 已校验；qty 非负；结果在 int64_t 可表示范围内。
std::int64_t update_branch(std::int64_t position, Side side,
                           std::int32_t qty) {
    if (side == Side::Buy) return position + qty;
    return position - qty;
}

std::int64_t update_arithmetic(std::int64_t position, Side side,
                               std::int32_t qty) {
    return position + static_cast<std::int64_t>(side) * qty;
}
```

两种写法都合理。第一版可能被编译成条件移动，第二版也未必更省指令；如果方向长期偏向一边，分支本来就容易预测。对于会产生副作用的两条路径，更不能为了“无分支”把两边都执行一遍再选结果。

方向编码也常用于数组索引，例如把买卖两侧映射到两个槽位。入口处需要先排除非法枚举值，保证下标始终落在数组范围内。

### 5.4 循环展开：减少依赖，而不只是复制代码

下面的求和有一条连续依赖：下一次加法要使用上一次的 `sum`。拆成几个独立累加器，可以给 CPU 更多并行执行的机会。

```cpp
#include <cstddef>
#include <cstdint>
#include <span>

std::uint64_t sum_simple(std::span<const std::uint64_t> x) {
    std::uint64_t sum = 0;
    for (auto v : x) sum += v;
    return sum;
}

std::uint64_t sum_unrolled(std::span<const std::uint64_t> x) {
    std::uint64_t a = 0, b = 0, c = 0, d = 0;
    std::size_t i = 0;
    for (; x.size() - i >= 4; i += 4) {
        a += x[i];     b += x[i + 1];
        c += x[i + 2]; d += x[i + 3];
    }
    for (; i < x.size(); ++i) a += x[i];           // 处理不足四个的尾部
    return (a + b) + (c + d);
}
```

这里使用无符号整数，按模 2⁶⁴ 求和，两版结果一致。若业务不允许回绕，还要另做范围检查。浮点求和则需要另行判断：改变加法顺序可能改变舍入结果。

手工展开会增加代码体积和寄存器使用量，而编译器通常已经会做展开和向量化。先保留简单版，看优化报告，再决定是否值得维护第二版。

### 5.5 给编译器一个容易向量化的循环

SIMD 是一条指令同时处理多个数据元素。基础工作通常是把数据变连续、减少循环中的调用、讲清输入输出是否重叠，而不是立即手写 intrinsic。

```cpp
#include <cstddef>

// GCC/Clang 扩展；其他编译器退化为普通指针参数。
#if defined(__GNUC__) || defined(__clang__)
#define LL_RESTRICT __restrict__
#else
#define LL_RESTRICT
#endif

// 前提：三个区间各有 n 个 float，且 out 与 a、b 均不重叠。
// a 和 b 都只读，可以互相重叠。
void add_arrays(const float* LL_RESTRICT a,
                const float* LL_RESTRICT b,
                float* LL_RESTRICT out, std::size_t n) {
    for (std::size_t i = 0; i < n; ++i) out[i] = a[i] + b[i];
}

#undef LL_RESTRICT
```

这里的 `__restrict__` 向编译器承诺：`out` 的写入区间与 `a`、`b` 的读取区间不重叠；只读的 `a` 和 `b` 可以重叠。编译器据此优化访存，违反前提会破坏程序正确性。调用方无法满足这一条件时，使用普通指针参数。

GCC 可以用 `-fopt-info-vec-optimized -fopt-info-vec-missed` 查看向量化结果，再结合汇编检查生成的指令。对只有几项数据的单消息计算，准备 SIMD 的成本可能超过收益；为凑满向量宽度而等待更多消息，还会增加排队延迟。

### 5.6 类型选择先满足数值语义

浮点类型混用可能引入额外转换。例如，`float` 变量乘上不带后缀的浮点字面量，会先提升到 `double`：

```cpp
float mixed_precision(float value) {
    return static_cast<float>(value * 1.23);       // 提升到 double 后计算
}

float single_precision(float value) {
    return value * 1.23f;                         // float 精度下计算
}
```

如果模型本来就以 `float` 精度计算，第二版避免了不必要的类型混用；如果需要 `double` 中间结果，它就不是等价替换，两版可能产生不同的舍入结果。

价格和数量则常用整数单位表达，例如以 tick 存价格。这样能明确相等比较和最小价格变动，但仍要处理乘法溢出：

```cpp
#include <cstdint>
#include <limits>
#include <optional>

// 示例只接受非负 tick 价格；结果单位是 tick × 数量。
std::optional<std::int64_t> notional(std::int64_t price_ticks,
                                     std::uint32_t qty) {
    if (price_ticks < 0) return std::nullopt;
    if (qty != 0 && price_ticks >
        std::numeric_limits<std::int64_t>::max() / qty)
        return std::nullopt;
    return price_ticks * static_cast<std::int64_t>(qty);
}
```

有符号整数溢出是未定义行为，无符号整数回绕也未必符合业务含义。先规定范围，再比较性能。

另一个底线是别名规则。通过 `reinterpret_cast<std::uint64_t*>(&value)` 去读写一个 `double`，不能当成合法的快速位操作。需要读取位表示时，用 `std::bit_cast`：

```cpp
#include <bit>
#include <cstdint>

std::uint64_t bits_of(double value) {
    static_assert(sizeof(double) == sizeof(std::uint64_t));
    return std::bit_cast<std::uint64_t>(value);
}
```

`bit_cast` 保留对象的位表示。若要进一步解释符号位、指数位或编码成网络字节序，还需要明确目标浮点格式和协议规则。

<a id="concurrency"></a>

## 6. 减少线程之间的协调

### 6.1 先减少共享写入，再考虑怎样加锁

当多个线程反复修改同一个对象，即使使用原子操作，也会争用承载这个对象的缓存行。一个常见改法是按证券、账户或分区划分状态，让每份状态由固定线程写入。

用计数器说明最直接：

```cpp
#include <atomic>
#include <cstdint>
#include <span>

// Bad case：每条消息都更新全局热点。
void count_shared(std::span<const std::uint8_t> accepted,
                  std::atomic<std::uint64_t>& total) {
    for (auto value : accepted)
        if (value) total.fetch_add(1, std::memory_order_relaxed);
}

// Good case：线程内累加，调用方在 join 后统一汇总返回值。
std::uint64_t count_local(std::span<const std::uint8_t> accepted) {
    std::uint64_t local = 0;
    for (auto value : accepted) local += (value != 0);
    return local;
}
```

两版在处理结束后可以得到相同总数，但第二版不再提供逐条更新的实时全局计数。它适合阶段性统计；需要即时全局状态的业务，还得保留相应的同步。

订单簿更适合按证券归属划分：某个证券的更新由同一个线程顺序处理。需要跨证券一致快照或账户级限额时，再设计额外的协调机制，不能假定把状态拆开后所有约束自然成立。

### 6.2 不共享变量，也可能共享缓存行

两个线程修改不同的计数器，如果计数器恰好落在同一缓存行，仍可能引起缓存一致性流量。这就是 false sharing，通常译作伪共享。

```cpp
#include <atomic>
#include <cstddef>
#include <cstdint>

struct CountersBad {
    std::atomic<std::uint64_t> producer{0};
    std::atomic<std::uint64_t> consumer{0};
};

// 本例按目标 CPU 的 64 字节缓存行设计，移植时需核对。
inline constexpr std::size_t cache_line = 64;
struct alignas(cache_line) Counter {
    std::atomic<std::uint64_t> value{0};
};
struct CountersGood {
    Counter producer;
    Counter consumer;
};
```

对齐后的 `Counter` 大小也必须满足其对齐要求，因此两个成员不会挤在同一个 64 字节区间中。也可以参考标准库的 `std::hardware_destructive_interference_size`，但需考虑编译目标和跨模块布局的一致性。

填充用空间换隔离，只适用于频繁独立写入的热点字段。给每条小订单都填充到一整行，可能让工作集大幅增加；多线程真正争写同一个变量，也不会因为对齐而消失。

### 6.3 SPSC 环形队列：把“发布”和“复用”分开理解

若线程间只有一个生产者和一个消费者，可以使用 SPSC（single producer, single consumer）环形队列。生产者写入槽位并公布写指针；消费者读取槽位，再公布读指针。后一个动作告诉生产者，这个位置以后可以覆盖了。

![SPSC 环形队列中的数据发布和槽位复用](assets/hft-02/spsc-memory-order.svg)

*图 3：两个方向都需要同步。先写事件再发布；先读完事件再归还槽位。[交互图](assets/hft-02/spsc-memory-order.html)。*

下面的实现保留一个空槽区分满与空，所以数组长度为 `N`，可用容量为 `N - 1`。

```cpp
#include <array>
#include <atomic>
#include <cstddef>
#include <type_traits>

template<class T, std::size_t N>
class SpscRing {
    static_assert(N >= 2 && (N & (N - 1)) == 0);
    static_assert(std::is_trivially_copyable_v<T>);
    static_assert(std::is_default_constructible_v<T>);
    static_assert(std::is_nothrow_copy_assignable_v<T>);
    static_assert(std::atomic<std::size_t>::is_always_lock_free);

    alignas(64) std::array<T, N> slots_{};
    alignas(64) std::atomic<std::size_t> write_{0};
    alignas(64) std::atomic<std::size_t> read_{0};
public:
    bool try_push(const T& value) noexcept {
        const auto w = write_.load(std::memory_order_relaxed);
        const auto next = (w + 1) & (N - 1);
        if (next == read_.load(std::memory_order_acquire)) return false;
        slots_[w] = value;
        write_.store(next, std::memory_order_release);
        return true;
    }

    bool try_pop(T& value) noexcept {
        const auto r = read_.load(std::memory_order_relaxed);
        if (r == write_.load(std::memory_order_acquire)) return false;
        value = slots_[r];
        read_.store((r + 1) & (N - 1), std::memory_order_release);
        return true;
    }
};
```

两条同步关系分别保护不同的事情：

1. 生产者填好 `slots_[w]`，随后 release 发布 `write_`；消费者通过 acquire 观察到发布后，才能读取新数据。
2. 消费者复制完 `slots_[r]`，随后 release 更新 `read_`；生产者通过 acquire 观察到空间可用后，才能复用旧槽位。

本线程独占写入的游标可以 relaxed 读取，但不能把所有原子操作一律改成 relaxed。普通 `bool`、普通下标或 `volatile` 都不能替代这里的同步。

使用时还有几个直接影响正确性的约束：队列只能有一个生产者、一个消费者；`try_push()` 返回 `false` 时本条数据尚未入队；对象析构必须等两端停止。这里限制载荷类型和平台原子能力，是为了避免复制过程中分配内存、抛异常，或由平台用锁实现下标原子操作。64 字节对齐也沿用前面的目标平台假设。

取得写槽位之前必须检查剩余容量，否则绕回数组开头时，就可能覆盖尚未消费的数据。环形寻址决定写在哪里，容量协议决定当前是否允许写入。

### 6.4 日志只交付必要字段，把格式化留给消费线程

同步日志把字符串格式化、流锁和 I/O 放进业务路径。异步日志可以减少这些工作，但前提是调用方没有先把整条日志拼好。

```cpp
#include <cstdint>
#include <ostream>

struct LogRecord {
    std::uint64_t order_id;
    std::uint32_t code;
};

void log_bad(std::ostream& out, LogRecord r) {
    out << "order=" << r.order_id << " code=" << r.code << std::endl;
}

// Queue 可以使用上一节的 SpscRing<LogRecord, 1024>。
// 只用于允许丢弃的诊断日志；dropped 由这个生产者线程独占。
template<class Queue>
void log_good(Queue& queue, LogRecord r, std::uint64_t& dropped) {
    if (!queue.try_push(r)) ++dropped;
}

// 在日志线程调用：只处理已有数据，最多 64 条，不等待凑满。
template<class Queue>
void drain_logs(Queue& queue, std::ostream& out) {
    LogRecord r{};
    for (unsigned i = 0; i < 64 && queue.try_pop(r); ++i)
        out << "order=" << r.order_id << " code=" << r.code << '\n';
}
```

格式化移到消费端后，业务线程只需入队一个小结构体，日志线程也省去了每条 `std::endl` 带来的 flush。写入流、flush 到操作系统和持久化到存储仍是不同阶段，需要按日志用途安排。

队列满时，允许丢弃的诊断日志可以计数后跳过，必须保留的记录则需要可靠性和背压设计。多个生产者可以各自配 SPSC 队列，再由日志线程汇总。

批量处理适合摊薄调度和 I/O 成本，但要设上限，并处理当前已有的数据；一直等“攒够 64 条”会让低流量时的第一条消息迟迟发不出去。停机时还要先停止生产，再排空和刷新。调用端同样值得检查：`logger.log(order.toString())` 中的 `toString()` 会在入队之前执行，换成异步 logger 并不会自动搬走这次格式化。

### 6.5 多个消费者都要看到同一条事件时，再引入 Disruptor

SPSC 适合把消息交给一个消费者。如果同一条消息要同时更新两个独立结果，随后再汇总，就需要表达广播和依赖。

Disruptor 将事件保存在预分配的环形缓冲区中。以一个菱形依赖为例：A、B 都处理每一条事件，C 等待 A 和 B 完成后再处理。生产者确认旧事件已经不再使用，才能复用槽位。

![Disruptor 的菱形消费依赖与末端背压](assets/hft-02/disruptor-diamond.svg)

*图 4：A 和 B 操作同一个事件的不同字段。C 的依赖是 AND：两边都完成才能继续。本图只有 C 是末端节点，因此由 C 的完成进度约束槽位复用。[交互图](assets/hft-02/disruptor-diamond.html)。*

下面的程序中，生产者初始化事件，A、B 分别计算 `left` 和 `right`，C 汇总；主线程在关闭并等待所有消费者退出后读取结果。

```cpp
#include "disruptor.hpp"
#include <cstdint>
#include <iostream>

struct Event {
    std::uint64_t id = 0;
    std::uint64_t left = 0;
    std::uint64_t right = 0;
};

int main() {
    using namespace disruptor;
    Disruptor<Event, ProducerMode::Single> bus(1024, WaitStrategy::Blocking, 64);
    std::uint64_t total = 0;                       // 只有 C 写

    const auto a = bus.add_consumer("A", {},
        [](Event& e, std::int64_t, bool) { e.left = e.id * 3; });
    const auto b = bus.add_consumer("B", {},
        [](Event& e, std::int64_t, bool) { e.right = e.id * 7; });
    bus.add_consumer("C", {a, b},
        [&](Event& e, std::int64_t, bool) { total += e.left + e.right; });

    bus.start();
    for (std::uint64_t id = 1; id <= 100; ++id) {
        bus.publish([id](Event& e, std::int64_t) noexcept {
            e = Event{id, 0, 0};                  // 每次填写完整的新事件
        });
    }
    bus.shutdown();                               // 排空并 join
    std::cout << total << '\n';                   // 50500
}
```

这段程序使用 `hft/disruptor_cpp20` 下的 C++20 实现。把代码保存为 `demo.cpp`，在项目根目录执行：

```bash
g++ -std=c++20 -O2 -pthread -Ihft/disruptor_cpp20/include demo.cpp -o /tmp/disruptor-demo
/tmp/disruptor-demo
```

理解这一结构时，要分清四个位置：申请到了哪个序号，哪个事件已经发布，每个消费者处理到哪里，以及哪个旧槽位可以覆盖。它们不会始终相等。

多生产者尤其容易出现发布空洞：P1 先申请序号 10，却还没填完；P2 已经完成序号 11。此时不能因为“最大已发布序号是 11”就读取 10。本地实现为每个槽位保存绝对发布序号，消费者只处理连续、已发布的区间。

![多生产者下，后面的事件先发布，不能越过前面的空洞](disruptor_cpp20/docs/diagrams/publication-gap.svg)

*图 5：后一个生产者先完成写入时，消费者仍须等待前面的事件发布。*

再看复用条件。容量为 `N`，生产者准备写绝对序号 `s`（图 4 中的 `candidate`）时，将覆盖 `s - N` 所在的物理槽位。因此必须满足：

```text
所有末端消费者的最小完成序号 >= s - N
```

在菱形图中，只检查 C 就足够，是因为 C 不可能越过 A 或 B。若另有独立消费者 D，它也是末端节点，必须一起纳入检查。

这套结构适合需要广播和消费依赖的场景，也增加了线程交接、发布标记和进度管理。若 A、B 的计算各只有几条指令，把它们放在同一线程直接调用，可能更便宜。示例中的 A、B 写不同的标量字段，避免了数据竞争，但这些字段仍可能位于同一缓存行。

这个实现支持忙等、让出 CPU 和阻塞等待，示例选择阻塞方式。忙等占用 CPU，阻塞等待需要唤醒线程，选择取决于负载和可用资源；容量等待与用户回调也会影响处理进度，所以整个组件没有无条件的 lock-free 或 wait-free 保证。回调返回后，框架就可以推进完成序号，处理函数必须在返回前结束对 `Event&` 的访问，不能把引用留给未完成的异步任务。

<a id="validation"></a>

## 7. 怎样验证一次优化

### 7.1 让基准测到真正想比较的工作

手写一个循环，前后读两次时钟，很容易测偏：输入是常量，计算被折叠了；结果没人使用，循环被删掉了；把随机数生成、分配内存和打印都放进计时区间，最后反而主要测到这些辅助操作。

下面用 Google Benchmark 比较普通求和与四路累加。输入在循环外生成，结果通过 `DoNotOptimize` 交给基准工具。编译器仍然可以对求和进行向量化、展开等优化，这正是需要比较的最终执行效果。

```cpp
#include <benchmark/benchmark.h>
#include <cstddef>
#include <cstdint>
#include <random>
#include <vector>

template<bool Unroll>
static void BM_Sum(benchmark::State& state) {
    std::vector<std::uint64_t> values(static_cast<std::size_t>(state.range(0)));
    std::mt19937_64 rng(42);
    for (auto& value : values) value = rng();

    for (auto _ : state) {
        benchmark::DoNotOptimize(values.data());
        benchmark::ClobberMemory();               // 输入内存可能发生变化
        std::uint64_t a = 0, b = 0, c = 0, d = 0;
        std::size_t i = 0;
        if constexpr (Unroll) {
            for (; values.size() - i >= 4; i += 4) {
                a += values[i];     b += values[i + 1];
                c += values[i + 2]; d += values[i + 3];
            }
        }
        for (; i < values.size(); ++i) a += values[i];
        auto result = (a + b) + (c + d);
        benchmark::DoNotOptimize(result);
    }
}

BENCHMARK_TEMPLATE(BM_Sum, false)->Arg(64)->Arg(4096);
BENCHMARK_TEMPLATE(BM_Sum, true)->Arg(64)->Arg(4096);
BENCHMARK_MAIN();
```

安装 Google Benchmark 的头文件和库以后，把代码保存为 `bench.cpp`：

```bash
g++ -std=c++20 -O3 -DNDEBUG -pthread bench.cpp -lbenchmark -o /tmp/ll-bench
/tmp/ll-bench --benchmark_repetitions=5
```

一轮测的是整个数组的求和，不是一个元素的延迟。它反复使用同一组数据，也更接近稳定运行时的缓存状态，不能由此推断冷启动或随机查找的表现。

`DoNotOptimize` 不会禁止表达式内部的一切优化，`ClobberMemory` 也不会替程序刷新 CPU 缓存。仍要检查生成的汇编，以及输入是否代表实际业务。

### 7.2 用性能计数器解释变化

如果一项修改变快了，接下来应弄清楚少掉的成本在哪里。在支持相应事件的 Linux 环境中，可以先看：

```bash
perf stat -r 5 -e cycles,instructions,branches,branch-misses,cache-misses \
  /tmp/ll-bench --benchmark_filter='BM_Sum<false>/4096$' --benchmark_min_time=0.5s
```

这条命令统计整个进程，会包含初始化和基准框架的工作；Google Benchmark 还可能自动调整迭代次数。因此比较两版时，不能只拿 `cache-misses` 的总数做结论，要同时看工作量、每次操作的成本和输入规模。某些硬件事件不可用时，工具也会明确报错，不能把缺失的计数当成零。

### 7.3 读一次时间戳也有成本

`RDTSC` 可以在用户态直接读取时间戳计数器，正常执行时不需要陷入内核。不过，一条指令也可能需要几十个核心周期。uops.info 在重复执行裸 `RDTSC` 的实验中，测得以下平均成本：

| 测试 CPU | 架构 | 平均核心周期数/条 | 假设核心频率为 3 GHz 时的换算值 |
|---|---|---:|---:|
| Intel Core i5-6500 | Skylake | 25 | 约 8.3 ns |
| Intel Core i5-12600K 的 P 核 | Alder Lake-P | 26 | 约 8.7 ns |
| AMD Ryzen 5 7600X | Zen 4 | 35 | 约 11.7 ns |

这些数据对应的是**重复执行时的平均成本**，也就是倒数吞吐量，不是孤立一条指令从开始执行到结果可用的延迟。详细实验页中，Intel 的字段为 `Core cycles`，AMD 使用 `APERF` 计数；其中一组测试将指令展开 500 次，再报告归一化结果。表中的纳秒数按“周期数 ÷ 3 GHz”估算，并非原始实验直接测出的时间，也不代表换一台机器后仍然相同。

这里还要分清两种计数：表中的核心周期数描述执行成本，`RDTSC` 返回的则是 TSC ticks。现代支持 invariant TSC 的处理器按固定速率推进 TSC，不能用 CPU 当前的睿频频率去换算 TSC 差值。

这组数据说明，读钟本身就可能占掉十纳秒量级的预算。若为了测量几条指令，在它们前后各读一次时间戳，计时开销便可能与目标代码相当。用于可靠测量时，还要处理指令排序和编译器重排；加入所需围栏、保存结果以及日志输出后的完整开销，需要另行测量。

### 7.4 最后回到完整消息链路

微基准有优势，是因为条件可控；它的局限也在这里。一个函数变快，整条链路可能仍在等待队列、共享状态或者日志线程。

回放负载至少应包含平稳流量、短时突发和接近容量上限的情况，并保留排队时间。每处理完一条才发送下一条的测试，会隐藏系统繁忙期间本来应该继续到达的请求，低估拥塞时的延迟。

最慢样本需要单独追查：它来自缺页、扩容、锁竞争、线程被调度走，还是昂贵的业务路径？这些样本应保留在尾延迟统计中。预先裁掉最慢的 1% 会改变样本分布，后续算出的 p99 也就无法代表原始负载。

着手优化现有程序时，可以按下表寻找入口：

| 观察到的现象 | 优先检查 | 常见改动 |
|---|---|---|
| 偶发扩容、分配尖峰 | 热路径是否创建容器和字符串 | 预分配、复用、固定容量及满载处理 |
| 大量时间在查找和访存 | 键空间、对象大小、指针跳转 | 稠密索引、冷热分离、连续存储 |
| 错误很少，成功路径仍在格式化 | 函数参数是否提前求值 | 把字符串构造放入错误分支 |
| 线程越多，单条消息越慢 | 共享写入、交接次数、伪共享 | 单一写入者、减少交接、分离热点字段 |
| 吞吐量提高，尾延迟反而变差 | 是否等批次、是否积压 | 限定批量大小、记录排队时间 |
| 改了语法，性能没有变化 | 两版是否生成相同机器码 | 保留更清楚的写法，继续找实际瓶颈 |

对于一条具体的消息，省去一次分配、一次字符串格式化或一次无用的线程交接，通常比把每个 `if` 改写成位运算更值得先尝试。找到成本以后，保留一份可复现的基准，再决定这项改动是否值得长期维护。

文中的函数片段可以单独保存为 `.cpp`，用 `g++ -std=c++20 -O2 -Wall -Wextra -Wpedantic -c demo.cpp` 检查编译，运行时再补上调用入口。Disruptor 和 Google Benchmark 两个完整程序的构建命令已在对应小节给出。
