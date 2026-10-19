# HFT Engineering从入门到入土（2）—— C++内存与计算优化

一条行情触发一批状态更新。程序的时间既花在计算上，也花在等待数据上。有时，把一个循环拆成两遍、额外发出几条预取指令，反而能更早得到结果。要解释这种变化，需要看数据怎样进入缓存、计算之间有哪些依赖，以及哪些工作可以重叠。

本文从编译期准备和内存管理出发，依次讨论订单簿布局、预缺页与预取、缓存行淘汰、循环与数值计算，最后用变长整数和流式解析串起一条消息的处理过程。线程通信与性能测量放在第 3 篇《C++线程通信与性能测量》中展开。

## 目录

- [1. 编译期计算与静态分派](#hft02-1)
  - [1.1 constexpr 编译期计算与查表](#hft02-1-1)
  - [1.2 函数模板、CRTP 与编译期静态分派](#hft02-1-2)
- [2. 内存分配、零拷贝视图与索引查找](#hft02-2)
  - [2.1 预分配与固定容量缓冲区](#hft02-2-1)
  - [2.2 对象池与空闲链表](#hft02-2-2)
  - [2.3 非必要不创造数据副本](#hft02-2-3)
  - [2.4 返回值优化（NRVO）与隐式移动](#hft02-2-4)
  - [2.5 稠密 ID 映射与数组直接索引](#hft02-2-5)
  - [2.6 订单簿：让容器和搜索顺应访问分布](#hft02-2-6)
- [3. 数据布局与缓存局部性](#hft02-3)
  - [3.1 结构体布局与冷热数据分离](#hft02-3-1)
  - [3.2 AoS、SoA 与顺序访问](#hft02-3-2)
  - [3.3 预缺页（Prefault）与预取（Prefetch）](#hft02-3-3)
  - [3.4 读完的数据是否还值得留在缓存](#hft02-3-4)
- [4. 分支、循环与数值计算优化](#hft02-4)
  - [4.1 短路求值与条件检查顺序](#hft02-4-1)
  - [4.2 延迟格式化与 cold/noinline 冷路径分离](#hft02-4-2)
  - [4.3 无分支计算](#hft02-4-3)
  - [4.4 循环展开和多路计算](#hft02-4-4)
  - [4.5 SIMD 自动向量化与 __restrict__](#hft02-4-5)
  - [4.6 浮点精度混用](#hft02-4-6)
  - [4.7 signed/unsigned、整数转换与循环优化](#hft02-4-7)
- [5. Stop-bit 变长整数：用短字段减少数据搬运](#hft02-5)
  - [5.1 先固定编码规则，避免混用不同 varint](#hft02-5-1)
  - [5.2 编解码实现：短值快路径，长值有界展开](#hft02-5-2)
- [6. 用状态机从字节流解析变长结构体](#hft02-6)
- [7. 总结](#hft02-7)
- [参考资料](#hft02-references)

<a id="prepare"></a>

<a id="hft02-1"></a>
## 1. 编译期计算与静态分派

<a id="hft02-1-1"></a>
### 1.1 constexpr 编译期计算与查表

协议字段宽度、固定换算系数、消息类型对应表，通常在编译时或启动时就能确定。常量在编译期生成，配置文件中的参数在初始化阶段解析和校验，就能把这些工作移出逐条消息的处理路径。后面的预缺页也采用同样的思路，把准备工作放到进程启动阶段。

下面按十进制精度查找缩放系数：

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

`constexpr` 函数可以用于常量求值；传入运行时输入时，计算也可以在运行时执行。

<a id="hft02-1-2"></a>
### 1.2 函数模板、CRTP 与编译期静态分派

每条消息都经由虚函数或 `std::function` 调用策略时，如果编译器无法确定实际目标，就需要在运行时动态分派。对于启动后不变的行为，可以在外层选定类型，让内层直接调用该类型的实现。

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

目标函数可见以后，编译器除了省去间接跳转，还有机会继续做常量传播、删除无用分支。普通模板函数就能实现这种静态分派。

如果希望把统一接口或公共处理流程放进基类，再由不同规则提供具体实现，可以使用 CRTP（Curiously Recurring Template Pattern，奇异递归模板模式）。它的写法是让派生类把自己的类型传给基类：`Derived : Base<Derived>`。

沿用上面的 `Quote`，价差检查可以写成下面这样。这段代码接在前面的定义之后即可编译。

```cpp
template<class Derived>
class RuleBase {
protected:
    RuleBase() = default;
    ~RuleBase() = default;  // 不支持通过基类指针 delete 派生对象。

public:
    bool accept(const Quote& q) const noexcept {
        // 约定：当前基类子对象确实属于 Derived 对象。
        return static_cast<const Derived&>(*this).accept_impl(q);
    }
};

class CrtpSpreadRule final : public RuleBase<CrtpSpreadRule> {
public:
    bool accept_impl(const Quote& q) const noexcept {
        return q.bid_ticks < q.ask_ticks;
    }
};

template<class Derived>
bool check_crtp(const RuleBase<Derived>& rule, const Quote& q) noexcept {
    return rule.accept(q);
}

bool crtp_example() {
    CrtpSpreadRule rule;
    return check_crtp(rule, Quote{100, 101});
}
```

调用链是 `check_crtp()` → `RuleBase<CrtpSpreadRule>::accept()` → `CrtpSpreadRule::accept_impl()`。编译器在实例化时已经知道 `Derived` 是 `CrtpSpreadRule`，直接确定调用目标；实现可见时，还能尝试把整条调用链内联起来。

CRTP 的模板参数应当对应实际派生类型，例如 `CrtpSpreadRule` 继承 `RuleBase<CrtpSpreadRule>`。基类中的 `static_cast` 据此访问派生对象，不做运行时类型检查。相关转换条件见 [C++ static_cast 规则](https://eel.is/c++draft/expr.static.cast)。

CRTP 让基类调用派生类提供的实现，适合复用公共接口或处理流程；只需调用 `rule.accept(q)` 时，普通函数模板更直接，两者也可能生成相同的机器码。`RuleBase<A>` 和 `RuleBase<B>` 是不同类型，各自对应一套静态接口。

插件和需要在运行时替换的实现适合保留动态分派；编译器能推断调用目标时，有机会自行去虚拟化。静态分派的代价是模板实例化带来的代码增长，实例过多会增加指令缓存压力。

<a id="memory"></a>

<a id="hft02-2"></a>
## 2. 内存分配、零拷贝视图与索引查找

<a id="hft02-2-1"></a>
### 2.1 预分配与固定容量缓冲区

动态内存分配会消耗处理时间，分配器内部竞争、扩容时的数据搬移，以及首次触页时的 minor page fault 还会让耗时波动。低延迟程序常在启动时准备内存，处理消息时复用。

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
        if (input.size() > limit_) return false;   // 超限交给调用方处理，避免push back触发resize
        data_.clear();                            // 保留 capacity
        for (const auto& u : input) data_.push_back(u);
        return true;
    }
    // 返回视图只在下一次成功 collect() 或对象销毁之前有效。
    std::span<const Update> view() const { return data_; }
};
```

<a id="hft02-2-2"></a>
### 2.2 对象池与空闲链表

订单、订单簿节点和内部消息常常有已知的数量上限。可以预先创建一组对象，再用空闲链表管理槽位，省去每次使用时 `new`、结束时 `delete` 的操作。

![固定槽位池：申请从空闲链表摘下一个槽位，归还时放回链表头](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/02-slot-pool.png)

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

    // 真正存放订单的数组。池创建时对象就已存在，申请时覆盖其内容。
    // 槽位下标不是 Order::id；同一槽位可以先后存放不同订单。
    std::array<Order, N> objects_{};

    // 用下标串起空闲槽位：next_[i] = j 表示 i 后面的空闲槽位是 j。
    // 只有 i 空闲时，这个值才参与空闲链；i 已借出时可保留旧值。
    std::array<std::size_t, N> next_{};

    // true 表示槽位已借出，false 表示空闲。{} 将初值全部设为 false。
    std::array<bool, N> used_{};

    // 第一个空闲槽位。有效下标为 [0, N)，用 N 表示没有空闲槽位。
    std::size_t head_ = 0;
public:
    OrderPool() {
        // 初始化为空闲链 0 -> 1 -> ... -> N-1 -> N（结束）。
        // N 只是结束标记，不能用来访问 objects_[N] 或 next_[N]。
        for (std::size_t i = 0; i < N; ++i) next_[i] = i + 1;
    }

    // 借出一个槽位并写入订单；成功返回槽位下标，池满返回 nullopt。
    std::optional<std::size_t> acquire(Order value) noexcept {
        if (head_ == N) return std::nullopt;
        const auto i = head_;   // 取走当前链表头，不扫描整个数组。
        head_ = next_[i];       // 原来的第二个空闲槽位成为新链表头。
        objects_[i] = value;    // 赋值给已有对象，不执行 new。
        used_[i] = true;        // i 已借出，不再属于空闲链。
        return i;
    }

    // 只查询，不申请。越界或槽位空闲时返回 nullptr。
    // 返回指针的业务使用权在 release(i) 时结束。
    Order* get(std::size_t i) noexcept {
        return i < N && used_[i] ? &objects_[i] : nullptr;
    }

    // 归还槽位：只恢复可复用状态，不清空字段，也不析构 Order。
    bool release(std::size_t i) noexcept {
        // 拒绝越界或归还仍为空闲状态的槽位，避免重复接入空闲链。
        if (i >= N || !used_[i]) return false;
        used_[i] = false;
        next_[i] = head_;       // i 指向旧链表头；不要求 i < head_。
        head_ = i;              // i 成为新链表头，下次申请优先取走它。
        // 顺序不能反过来，否则会把 next_[i] 写成 i，形成自环。
        return true;
    }
};

bool pool_example() {
    OrderPool<8> pool;
    // 初始 head_ 为 0，因此这里取得槽位 0，订单的业务 ID 则为 7。
    const auto handle = pool.acquire(Order{7, 10'005});
    if (!handle) return false;  // 判断 optional 是否有值，不是判断下标非零。
    pool.get(*handle)->price_ticks = 10'006;
    // 用完后归还；此后不再用旧 handle 或旧指针访问这笔订单。
    return pool.release(*handle);
}
```

`next_` 保存的是链表关系，不是数组中的相邻关系。初始链恰好按下标递增，只是因为构造函数这样连接；经历申请和归还后，它完全可以变成 `3 -> 0 -> 1 -> 2`。订单在 `objects_` 中的位置不动，变化的只有空闲链表中的索引关系。

例如，`OrderPool<4>` 当前的槽位 `0、1、2` 空闲，槽位 `3` 已借出，空闲链是：

[![head等于0，空闲链为0到1到2到结束标记4，槽位3已借出不在空闲链中。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-free-chain-before.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-free-chain-before.png)

现在调用 `release(3)`，虽然旧 `head_ = 0` 的数值比 `3` 小，仍然执行同样的头插操作：

```cpp
// release(3) 内部的状态变化：
used_[3] = false;
next_[3] = head_;  // next_[3] = 0：新归还的槽位指向旧链表头。
head_ = 3;        // 再把新归还的槽位设为链表头。
```

结果是：

[![归还槽位3后head等于3，空闲链变成3到0到1到2到结束标记4。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-free-chain-after.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-free-chain-after.png)

下一次 `acquire()` 返回 `3`，并把 `head_` 更新为 `next_[3]`，也就是 `0`。这里的“前后”指链表中的逻辑顺序，与数组下标大小无关。若原来没有空闲槽位，即 `head_ == N`，归还后就是 `i -> N`，同样成立。

<a id="hft02-2-3"></a>
### 2.3 非必要不创造数据副本

解析消息时，先把缓冲区复制成字符串，再切出字段字符串，会产生额外复制，也可能增加分配开销。

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

这里省掉的是解析前的字符串副本。消息生产端还有一种常见复制：先序列化到临时 buffer，再复制入队。队列提供预留接口后，序列化器就可以直接填写目标区域，完成后发布。原地填写并发布的具体做法，见本系列第 3 篇《C++线程通信与性能测量》1.7 节。

<a id="hft02-2-4"></a>
### 2.4 返回值优化（NRVO）与隐式移动

返回局部变量时，额外添加 `std::move` 会妨碍本可进行的命名返回值优化（NRVO）。

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

<a id="hft02-2-5"></a>
### 2.5 稠密 ID 映射与数组直接索引

对于稀疏的外部订单号和标的代码，进入内部系统后可以映射成紧凑编号。若编号范围已知，数组访问能省去哈希计算、桶访问和节点间接寻址。

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

<a id="hft02-2-6"></a>
### 2.6 订单簿：让容器和搜索顺应访问分布

直接索引解决了“已知 ID 去哪里找状态”。订单簿还需要一个有序价格索引，用来查找价格档位、最优价格及相邻档位。

在订单级行情中，程序通过订单 ID 索引查找已有订单的方向、价格和数量，通过价格索引维护各档的汇总数量。修改一笔订单的数量，往往只改变现有档位；只有新增价位或该档数量归零时，才需要插入或删除档位。两类操作的频率，会直接影响容器选择。

`std::map` 是合理的起点：按价格查找为 O(log n)，节点在未被删除时保持稳定，订单索引可以保存档位的迭代器。代价是沿树查找时，要先访问当前节点，才能取得下一节点的地址；节点若在长期分配后变得分散，还会增加随机访存。基准测试中的堆布局较紧凑时，可能低估实际部署后的这部分开销。

容器布局和搜索方式可以结合价格更新的分布来调整：

| 调整 | 加速思路 | 代价 |
|---|---|---|
| 从节点树改为紧凑有序数组 | 一条缓存行包含更多有效档位，减少指针追踪 | 中间插删需要搬移元素 |
| 把最优价放到数组尾部 | 热点插删靠近尾部时，平均搬移距离缩短 | 展示顺序与内部存储顺序需要分开 |
| 尝试无分支二分 | 输入方向难预测时，减少错误推测 | 可能增加指令与数据依赖，未必总有收益 |
| 从最优价一端线性搜索 | 大部分请求很快遇到目标，且访问连续 | 深档位或均匀分布下可能扫描很长距离 |

以买方价格为例，最优买价是最高价。将它放到尾部后，一次提高最优价的插入可能变成追加：

[![买方最优价为最高价。放在头部时插入101需搬移原元素；放在尾部且容量足够时可直接追加。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-orderbook-direction.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-orderbook-direction.png)

更新集中在最优价附近时，把最优价放到尾部可以减少元素搬移。卖方也可以采用相应方向，让最低卖价靠近尾部。

从更新最频繁的一端开始线性搜索，也利用了这种分布：多数请求在前几个档位就能找到目标，连续访问又有利于缓存。二分搜索的比较次数更少，但下一次访问位置依赖前一次比较；带分支的实现还会借助预测，提前沿某条路径执行。容器的布局、目标所在档位的深度和分支预测，共同影响搜索速度。

改用 `vector` 后，中间插删会移动档位，即使提前 `reserve()` 也一样。订单 ID 索引可以保存价格后重新定位，或通过稳定的间接索引找到档位，替代原来的节点地址和迭代器。[C++ vector 修改与失效规则](https://eel.is/c++draft/vector.modifiers)

<a id="layout"></a>

<a id="hft02-3"></a>
## 3. 数据布局与缓存局部性

<a id="hft02-3-1"></a>
### 3.1 结构体布局与冷热数据分离

CPU 从内存搬运数据时以缓存行为单位。即使只用几个字节，同一缓存行里的其他字段也会随之进入缓存。低延迟代码中，数据布局经常比某条算术指令更值得优化。

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

在常见的 64 位 ABI 上，两者分别占 32 和 24 字节。重新排列字段就减少了填充。

如果订单对象还包含几十字节的备注、字符串和历史统计，仅调整顺序不够。可以把经常一起访问的字段放在一起，把很少访问的字段移到独立结构中。

![AoS、冷热分离与 SoA：不同访问方式对应不同布局](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/01-data-layout.png)

*图 2：按订单处理时可以保留紧凑结构体；只扫描数量时，连续的数量数组能避免带入无关字段。图中格子仅表示布局关系。*

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

冷热分离让热点循环少搬运无关数据，访问冷字段时则多一次定位。

<a id="hft02-3-2"></a>
### 3.2 AoS、SoA 与顺序访问

AoS（array of structures，结构体数组）适合一次处理一条记录；SoA（structure of arrays，把各字段分别存成数组）适合一次处理同一个字段。例如一个批次只统计数量是否超过阈值，SoA 可以只读数量数组，减少带入缓存的无关字段。

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

<a id="hft02-3-3"></a>
### 3.3 预缺页（Prefault）与预取（Prefetch）

把首次访问内存时的缺页处理提前完成，就是 prefault（预缺页）。在 Linux 上，申请匿名映射取得地址后，部分页面仍可能没有可写的物理页映射。若直到第一条行情到来才写入这些页面，页面分配、清零和页表更新就会进入业务路径；这类匿名内存的首次缺页通常是 minor fault，无须从磁盘读取数据。

最直观的做法是在初始化阶段，向每个页面写一个字节。下面用匿名内存映射演示，这个缓冲区只存放字节，创建后先完成 prefault，再交给业务使用；对象析构时释放内存映射。

```cpp
#include <cerrno>
#include <cstddef>
#include <span>
#include <stdexcept>
#include <system_error>
#include <sys/mman.h>
#include <unistd.h>

class PrefaultBuffer {
    void* data_ = MAP_FAILED;
    std::size_t size_;
public:
    explicit PrefaultBuffer(std::size_t bytes) : size_(bytes) {
        if (bytes == 0) throw std::invalid_argument("empty buffer");

        // 查询基础页大小，不把 4KB hardcode在代码中。
        const long page_size = ::sysconf(_SC_PAGESIZE);
        if (page_size <= 0)
            throw std::runtime_error("cannot get page size");
        const auto page = static_cast<std::size_t>(page_size);

        // mmap 返回页对齐地址；此处尚未逐页触碰。
        data_ = ::mmap(nullptr, size_, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (data_ == MAP_FAILED)
            throw std::system_error(errno, std::generic_category(), "mmap");

        // 只对这段尚无业务数据的缓冲区执行。
        // volatile 保留实际写入，避免编译器删除看似无用的触页操作。
        auto* touch = static_cast<volatile unsigned char*>(data_);
        for (std::size_t offset = 0; offset < size_;) {
            touch[offset] = 0;  // 写访问，提前触发所需的可写页面映射。
            if (size_ - offset <= page) break;  // 下一步越出区间，避免加法溢出。
            offset += page;
        }
        // size_ 已检查非零。补写末字节，让这套触碰方法也适用于非对齐起点。
        touch[size_ - 1] = 0;
    }

    // 一个对象独占一段映射，禁止浅拷贝后重复 munmap。
    PrefaultBuffer(const PrefaultBuffer&) = delete;
    PrefaultBuffer& operator=(const PrefaultBuffer&) = delete;

    ~PrefaultBuffer() { ::munmap(data_, size_); }

    std::span<std::byte> view() noexcept {
        return {static_cast<std::byte*>(data_), size_};
    }
};

void prefault_example() {
    PrefaultBuffer buffer(16 * 1024 * 1024);  // 初始化阶段触碰 16 MiB。
    auto bytes = buffer.view();
    // 正式处理阶段复用 bytes；视图不能超过 buffer 的生命周期。
    bytes[0] = std::byte{0x42};
}
```

`mmap` 和 `malloc` 的对齐保证不同。页对齐只约束返回的虚拟地址：它是页大小的整数倍；物理页是否连续、页面是否驻留，还由映射和内存管理状态决定。

| 获取内存的方式 | 是否保证起始地址按页对齐 | 对 prefault 的影响 |
|---|---|---|
| 本例的 Linux `mmap(nullptr, ...)` | 成功时返回页对齐地址 | 从偏移 0 按页大小递进，就能触碰映射涉及的每一页 |
| 普通 `malloc(size)` | 不保证页对齐；它满足普通对象所需的对齐要求 | 返回地址可能位于页内，按页步进后还需考虑末尾跨页 |

即使 `malloc` 在内部用 `mmap` 分配大块内存，用户可用区也可以从映射内部开始，因此返回的指针仍无页对齐保证。`vector.data()` 同样如此；从页对齐映射中截取子区间时，起点是否对齐取决于偏移量。[Linux mmap 文档](https://man7.org/linux/man-pages/man2/mmap.2.html)、[Linux malloc 文档](https://man7.org/linux/man-pages/man3/malloc.3.html)

长度为一页的缓冲区，如果从页内开始，就会跨越两个页面。假设页大小为 4096 字节，缓冲区从某页内偏移 1024 的位置开始，长度为 4096 字节。以下地址均相对于该页页首：

[![4KiB缓冲区从页内1024开始，跨越两页，到5119结束；偏移0触及第一页，偏移4096越界，补写4095触及第二页。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-prefault-cross-page.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-prefault-cross-page.png)

如果只有按页步进的循环，它写完偏移 0 就退出了，因为下一个偏移 4096 已经等于缓冲区长度；但缓冲区实际跨了两个页面，第二页还没触碰。最后追加 `touch[size_ - 1] = 0`，就补齐了这一页，而且没有越界。

对于更长的区间，相邻触碰地址相差恰好一页，会落在连续页面的相同页内偏移上，不会跳过中间页面。最后一次循环写入到区间末尾的距离不足一页，因此最多漏掉末尾那一页，补写末字节即可。对于本例页对齐的 `mmap` 地址，这次补写只是再次触碰已经覆盖的页面。

这里选择写入，是因为匿名内存的首次读取可能只建立到共享零页（Linux zero page）的只读映射，后续第一次写入仍会分配私有页面。零页是内容全为零、可由多个虚拟页共享的物理页，与虚拟地址 0 无关；读取时可以让页表项指向零页，写入时再分配物理页保存数据。[Linux 匿名内存文档](https://docs.kernel.org/admin-guide/mm/concepts.html#anonymous-memory)

写入把这部分成本也移到初始化阶段。`volatile` 用于保留触页操作，避免编译器把它当成无用代码删除。

Linux 5.14 起，支持的可写映射可以使用 `madvise(addr, length, MADV_POPULATE_WRITE)`，让内核完成可写 prefault，省去用户态逐页写零。失败时可能已有部分页面处理完成；需要让页面持续驻留时，再配合锁页。[Linux madvise 文档](https://www.man7.org/linux/man-pages/man2/madvise.2.html)

```cpp
// Linux：放在构造函数中，mmap 成功之后。
if (::madvise(data_, size_, MADV_POPULATE_WRITE) == -1) {
    const int error = errno;
    // 构造函数抛异常时不会执行本对象的析构函数，必须先释放映射。
    ::munmap(data_, size_);
    data_ = MAP_FAILED;
    throw std::system_error(error, std::generic_category(),
                            "MADV_POPULATE_WRITE");
}
```

几种 prefault 写法的工作量不同：

| 写法 | 用户代码做什么 | 适用目的 |
|---|---|---|
| 按页步进写零，并补写末字节 | 覆盖每页，最后一页可能触碰两次 | 对新缓冲区做手动可写 prefault，写入次数较少，能降低启动成本 |
| 整段写零 | 覆盖每个字节，同时触页 | 启动成本不重要时，使用 memset 完成 prefault |
| `MADV_POPULATE_WRITE` | 请求内核建立可写页表，不主动改写原有字节内容 | 支持该接口的 Linux 上做可写 prefault |

Prefault 提前建立可写物理页映射；Prefetch（预取）提示 CPU 以缓存行为单位，把即将访问的数据提前带到较近的缓存。前者减少首次访问页面时的缺页开销，后者减少读取数据时的缓存未命中等待。

**理解 prefetch 的关键，是区分指令的生命周期与访存请求的生命周期：取数请求可以继续由存储硬件处理，而 prefetch 指令本身不必一直等到数据返回才退休。** 它仍然消耗执行资源和传输带宽，收益来自把访存等待与当前工作重叠。

以 Intel x86 乱序执行核心访问已映射的普通可缓存内存为例，CPU 分别跟踪尚未退休的指令与尚未完成的缓存行请求：

| 跟踪对象 | 典型硬件结构 | 跟踪的状态 |
|---|---|---|
| 尚未退休的指令 | ROB（Reorder Buffer，重排序缓冲区） | 指令是否完成、是否可以按程序顺序提交结果 |
| 尚未完成的缓存行请求 | LFB（Line Fill Buffer，缓存行填充缓冲区）等未命中跟踪结构 | 哪条缓存行正在获取、请求是否已经完成 |

一条普通 load 大致经历以下过程：

1. 取指、译码、寄存器重命名，为指令分配 ROB 等资源，并为读取结果准备目标物理寄存器。
2. 地址生成单元 AGU 计算地址，DTLB 提供地址翻译，L1 数据缓存参与查询；其中部分操作可以并行。
3. 如果缓存未命中，由 LFB 等结构跟踪请求，继续查询更远的缓存，必要时经内存控制器访问 DRAM。存储子系统继续处理取数请求，无须核心执行一串软件指令逐字节搬运。
4. 数据返回后，把 load 所需的值送给目标寄存器，唤醒依赖它的指令，并处理缓存填充。load 满足完成条件、且轮到它退休后，才能提交结果并释放相应的指令跟踪资源。

这里的“等待”有两个不同影响。依赖 load 结果的指令必须等数据；不依赖它的后续指令可以先乱序执行，但不能越过这条尚未完成的 load 退休。若等待足够长，后续指令会在有限的 ROB 等结构中积累，最终限制 CPU 继续分配和执行更多指令。普通 load 在等内存时，并不会一直独占 ALU 或地址生成单元。[Intel 对 load、prefetch 与退休机制的说明，第 7 页](https://www.intel.com/content/dam/www/public/us/en/documents/research/1999-vol03-iss-2-intel-technology-journal.pdf)

软件 prefetch 同样经过译码、地址生成和地址翻译，但不需要把数据返回到供程序使用的目标寄存器。在 Intel 描述的实现中，完成地址翻译后，它就可以按程序顺序退休，无须等到数据返回。已经发出的取数请求继续由 LFB 等结构跟踪。**指令从 ROB 中退出和缓存行到达，是分别完成的两件事。** 地址翻译和资源不足仍会造成等待，预取请求也可能被硬件忽略。[Intel 优化手册，3.7.1 节](https://cdrdv2-public.intel.com/779559/355308-Software-Optimization-Manual-047-Changes-Doc.pdf)、[Intel Fill Buffer 事件说明](https://perfmon-events.intel.com/platforms/emeraldrapids/core-events/core/)

逐页 load 只直接触及每页中的一条缓存行。以 4 KiB 页、64 B 缓存行为例，一页有 64 条缓存行，每页读一个字节远没有覆盖整页。即使启动时逐行读遍，大数据集也会把早先读入的行挤出缓存。运行中的 prefetch 则围绕即将使用的数据持续发出请求。

提前执行普通 load 同样可以预热缓存、重叠访存；若其结果不用，编译器可能删掉这个读取。如果读取结果会在稍后真正使用，也可以通过循环展开、批处理等方式安排多个独立 load。硬件预取或乱序执行已经充分隐藏等待时，显式 prefetch 可能没有额外收益。

顺序访问常有硬件预取支持。下面展示间接访问：`ids` 本身连续读取，但 `values[ids[i]]` 的目标地址取决于下标内容；如果这些下标分散，硬件不一定能及时预测后续的 `values` 访问。程序可以先读取未来的下标，显式提示 CPU 准备对应的缓存行。

```cpp
#include <cstddef>
#include <cstdint>
#include <span>

// 前提：两个 span 在调用期间保持有效；ids 中的每个下标均已在入口处检查，
// 小于 values.size()。这里把校验留在入口，避免在热点循环中重复检查。
std::uint64_t sum_indirect(std::span<const std::uint64_t> values,
                           std::span<const std::size_t> ids) {
    // 提前 16 次迭代准备数据，不是提前 16 页或 16 条缓存行。
    // 这是演示参数；合适的距离取决于访存延迟、循环耗时和缓存容量。
    constexpr std::size_t distance = 16;
    std::uint64_t sum = 0;
    for (std::size_t i = 0; i < ids.size(); ++i) {
#if defined(__GNUC__) || defined(__clang__)
        // 先比较剩余元素数，再计算 i + distance，避免越界和加法溢出。
        // 接近尾部时只停止发出新的预取，当前元素仍然会正常累加。
        if (ids.size() - i > distance) {
            // 这里是一次真实的下标读取，用来确定未来要访问哪个 values 元素。
            const auto future_index = ids[i + distance];

            // &values[future_index] 只计算目标地址，不在这里读取它的数值。
            // rw = 0：准备读；locality = 1：预计时间局部性较低，并非指定 L1。
            // 这是缓存提示，不返回元素值，也不保证调用后数据已经到达缓存。
            __builtin_prefetch(&values[future_index], 0, 1);
        }
#endif
        // 真正读取并使用当前元素。它与未来元素的取数有机会重叠。
        // 到 i + distance 时，仍须通过普通 load 读取那个元素；
        // 如果此前的预取有效且数据仍在缓存中，这次 load 的等待就可能缩短。
        sum += values[ids[i]];
    }
    return sum;
}
```

`__builtin_prefetch` 在目标支持时生成预取指令。`locality = 1` 表达预期复用程度，处理器决定具体缓存行为；后面的 load 才真正把数据读入寄存器。[GCC 预取内建函数文档](https://gcc.gnu.org/onlinedocs/gcc/Other-Builtins.html#index-__builtin_prefetch)

这段代码让预取比实际使用提前 16 次迭代：`i = 0` 时准备 `values[ids[16]]`，`i = 1` 时准备 `values[ids[17]]`，同时读取、累加当前元素。等到 `i = 16`，先前的取数请求已经经过一段时间的处理，甚至已经完成。前 16 项直接读取；`ids.size() <= 16` 时，整个循环不发出预取。

工作集较大且访问位置分散时，经常要等待较远的缓存或 DRAM 返回数据。预取可以提前发出多个取数请求，在执行当前计算的同时，等待后续要读取的数据。

**prefetch 后立刻 load 同一地址，通常缺少这种提前量，因此可能没有收益，甚至更慢。** 例如，在支持该内建函数的编译器上：

```cpp
#include <cstdint>

// 前提：p 指向一个有效的 std::uint64_t 对象。
std::uint64_t read_immediately(const std::uint64_t* p) {
    __builtin_prefetch(p, 0, 1);  // 发出提示，不表示取数已经完成。
    return *p;                  // 没有刻意安排独立工作，马上就需要这个值。
}
```

缓存行尚未到达时，紧随其后的 load 仍要等待。实现即使将它并入同一个在途请求，也只能省下 prefetch 提前发出请求的那段时间；预取本身还增加了译码、地址生成和资源开销。能否获益，取决于两次实际发射之间有多少工作可以与取数重叠。

提前量看的是实际执行间隔。编译器和 CPU 都可能重排指令；即使机器码把 prefetch 放在 load 前面，两条紧邻的指令通常也留不出足够的取数时间。

用一组简化数字看这个重叠：当前有 200 cycles 的独立工作，取数需要 300 cycles。工作结束后才发出读取，总共需要 `200 + 300 = 500 cycles`；在工作开始时发出请求，则约为 `max(200, 300) = 300 cycles`。普通 load 的乱序执行也能形成这种重叠；软件预取的作用是把请求进一步提前。

预取距离太小，数据来不及到达；太大，数据可能提前被淘汰，或占满在途请求资源。数据已经命中缓存、硬件预取已经跟上，或内存带宽饱和时，额外预取还可能增加争用。调整 `distance` 就是在这些成本之间寻找合适的提前量。[Intel 优化手册，第 9 章](https://cdrdv2-public.intel.com/821612/248966-Optimization-Reference-Manual-V1-050.pdf)

预取地址要覆盖实际使用的字段。一条记录跨两条缓存行，而计算时两条都要读取，就要分别预取这两条缓存行。局部性提示取决于数据的复用方式：如果一条期权记录在每次事件中只读一次，却会在下一次事件继续使用，它仍值得留在缓存里。

<a id="hft02-3-4"></a>
### 3.4 读完的数据是否还值得留在缓存

预取用于提前准备数据，而缓存容量有限，还需要考虑哪些数据值得保留。行情流中的一个事件可能只处理一次，而订单簿、持仓和估值状态会被反复访问。一次性数据持续进入缓存时，也可能挤出后续需要反复访问的状态数据。

缩小工作集、采用紧凑布局可以减少缓存污染。对于读完即弃的流数据，还可以主动使其缓存行失效，把空间留给频繁访问的表。x86 上有三条相关指令：

| 指令 | 主要行为 | 执行特点 |
|---|---|---|
| `CLFLUSH` | 使目标行在一致性域内的缓存层次中失效；脏数据先写回 | 逐条缓存行操作，脏行会产生写回流量 |
| `CLFLUSHOPT` | 同样使目标行失效，并在需要时写回；排序约束更宽松 | 多行操作可以更充分地重叠；需要顺序约束时配合相应 fence |
| `CLWB` | 写回脏数据，允许把行以非修改状态留在缓存层次中 | 具体留存位置由处理器决定 |

`CLFLUSHOPT` 会影响一致性域内的缓存副本，包括其他核心持有的副本；之后的访问或推测取数可以重新填入该行。`CLWB` 则着重于脏数据写回，允许保留缓存副本。[Intel 指令手册](https://cdrdv2-public.intel.com/782156/325383-sdm-vol-2abcd.pdf)、[Intel 对 CLWB 留存行为的说明](https://edc.intel.com/content/www/us/en/design/platforms/core-processor-series-3-datasheet-volume-1-of-2/001/cache-line-write-back-clwb/)

下面清除一条由事件数据独占的缓存行，示例按 64 B 行大小编写，使用 GCC/Clang 的 x86-64 接口。清除发生在所有消费者读完之后、槽位归还生产者之前。

```cpp
#include <array>
#include <cstddef>
#include <cpuid.h>
#include <immintrin.h>

// 完整占据三条 64 B 行，避免顺带清除相邻对象的热数据。
struct alignas(64) ConsumedEvent {
    std::array<std::byte, 192> bytes;
};
static_assert(sizeof(ConsumedEvent) == 192);

// 单独为这个函数启用指令，调用之前必须完成 CPU 特性检查。
__attribute__((target("clflushopt")))
static void flush_supported(ConsumedEvent& event) noexcept {
    for (std::size_t offset = 0; offset < event.bytes.size(); offset += 64)
        _mm_clflushopt(event.bytes.data() + offset);

    // 约束这些清除操作与后续需要排序的存储；屏障本身也有成本。
    // 这不替代调用方用于交还槽位的 C++ acquire/release 协议。
    _mm_sfence();
}

bool try_flush_consumed_event(ConsumedEvent& event) noexcept {
    // CPUID leaf 7、subleaf 0 的 EBX bit 23 表示支持 CLFLUSHOPT。
    // 不支持时保留正常缓存行为，不能直接执行未支持的指令。
    unsigned eax, ebx, ecx, edx;
    if (!__get_cpuid_count(7, 0, &eax, &ebx, &ecx, &edx) ||
        (ebx & (1u << 23)) == 0)
        return false;
    flush_supported(event);
    return true;
}
```

示例在入口检查 CPUID 特性；热路径中可在初始化时选好实现，省去逐事件检查。清除指令和屏障构成这项优化的额外成本。

清除应放在数据的最后一次使用之后。有下游继续读取，或多个对象共用一条行时，清除会迫使后续访问重新取数。对环形队列来说，消费者发布读游标就归还了槽位，清除应安排在这之前。第 3 篇《C++线程通信与性能测量》1.3 节给出了 SPSC 队列的完整实现。

流数据持续挤占缓存时，清除已读完的缓存行可以给频繁访问的表腾出空间；缓存空间充足时，额外清除反而可能拖慢处理。清除指令、脏行写回和屏障本身也有开销。若把这些操作放在结束时间戳之后，业务计时就会漏掉这部分成本。第 3 篇《C++线程通信与性能测量》3.1 节将说明如何分别统计业务区间和完整循环。

<a id="execution"></a>

<a id="hft02-4"></a>
## 4. 分支、循环与数值计算优化

<a id="hft02-4-1"></a>
### 4.1 短路求值与条件检查顺序

一条订单要通过多项检查：有些只读几个字段，有些要查表或遍历状态。如果前面的检查已经失败，且后续检查只用于判断订单是否有效，就可以提前返回。

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

`&&` 和 `||` 有短路语义，`&` 和 `|` 没有。若检查还会修改状态或产生其他副作用，提前返回也会跳过这些操作，调整顺序前要确认业务是否允许。

<a id="hft02-4-2"></a>
### 4.2 延迟格式化与 cold/noinline 冷路径分离

热路径里常见这样的开销：检查本身耗时很少，但调用检查函数之前，已经把错误字符串拼好了。

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

把字符串拼接、内存分配和异常构造推迟到检查失败之后，正常订单就不再执行这些工作。再把失败处理抽成独立函数，正常路径只留下条件检查和一个很少执行的调用。因此，把格式化放进 `if` 分支后，仍有抽离冷代码的空间。

CPU 的指令缓存（I-cache）保存机器指令。如果大段诊断、错误格式化或恢复逻辑被排在正常处理的基本块之间，就会拉大代码跨度；函数内联到多个调用点时，还可能复制这些冷代码。

抽离之后，常见路径可以更紧凑地排列，多个调用点也能共享一份冷代码，减少热函数内联时复制的内容，降低指令缓存占用和取指压力。

热函数变小后，也更容易被内联到上层调用者。例如，行情处理函数每次都调用 `validate_good`。抽离前，内联检查函数可能把格式化、异常构造及清理逻辑一起带入调用者；抽离后，内联只展开一个检查和一个冷调用。即使上层接收循环很大，这段小检查也更容易内联进去，省掉正常路径上的调用，并结合调用点信息继续优化。

这里希望内联 `validate_good` 这样的热函数，而用 `noinline` 保留 `reject_zero_qty` 这样的独立冷函数。

| 手段 | 主要作用 | 怎样配合使用 |
|---|---|---|
| 将冷分支抽成独立函数 | 把少见处理移出热函数 | 配合 `noinline` 保持函数独立 |
| `[[gnu::noinline]]` | 阻止函数体内联到调用点 | 配合 `cold` 表达低执行频率 |
| `[[gnu::cold]]` | 提示函数很少执行，帮助优化和布局；GCC 在许多目标上会把冷函数集中到专门的代码子段 | 配合 `noinline` 分别控制布局与内联 |
| `[[unlikely]]` / `__builtin_expect` | 提供分支概率提示，帮助编译器安排控制流和布局 | 用于函数内部的少见分支 |

**怎样配合使用 `always_inline` 与 `noinline`**

GCC/Clang 用函数属性控制内联，可以写成 `[[gnu::always_inline]]`、`[[gnu::noinline]]`，也支持 `__attribute__((always_inline))`、`__attribute__((noinline))`。

`always_inline` 比普通 `inline` 更强，会要求编译器绕过常规内联收益阈值尝试展开。

| 函数特征 | 属性使用方式 | 对 I-cache 的取舍 |
|---|---|---|
| 极短、调用频繁，且内联后能简化的小函数 | 默认先让编译器决定；确认漏掉有价值的内联后，再考虑 `always_inline` | 可能减少调用和辅助指令，但仍要检查多个调用点累计的代码体积 |
| 很少执行，含大量格式化、诊断或恢复代码 | 抽成独立函数，使用 `cold` + `noinline` | 保留一份共享冷代码，避免随热函数内联到多处 |
| 执行频繁，但函数体很大 | 强制内联会增加生成多份代码副本的风险，进而增加指令缓存压力 | 根据基准测试和性能剖析结果决定 |

少见且代码较多的错误处理、详细诊断或恢复分支适合抽离。执行冷路径时，仍有函数调用成本；若代码不在指令缓存中，还会产生缓存未命中的成本。如果分支实际很频繁，或整个分支只有一两条简单指令，这些成本就可能抵消收益。分支频率由真实工作负载决定，基于运行时剖析的优化（PGO）可以帮助编译器做出选择。

<a id="hft02-4-3"></a>
### 4.3 无分支计算

确认成交方向为买入或卖出后，可以分别编码成 `+1`、`-1`，减少更新持仓时的条件判断。

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

方向编码也常用于数组索引，例如把买卖两侧映射到两个槽位。入口先排除非法枚举值，下标才能始终落在数组范围内。

沿用上面的 `Side`，可以把买方数据放在 `levels[0]`，卖方数据放在 `levels[1]`。注意，前面的 `+1/-1` 是用于持仓加减的方向符号，不能直接作为这两个槽位的下标。下面先校验方向，再显式映射到 `0/1`；代码接在 `Side` 定义之后即可编译。

```cpp
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>

struct Level {
    std::int64_t price_ticks;
    std::uint32_t qty;
};

std::optional<std::size_t> side_index(Side side) noexcept {
    // enum class 仍可能通过显式转换得到未命名值，不能跳过校验。
    if (side != Side::Buy && side != Side::Sell)
        return std::nullopt;

    // 已确认只有两种方向：Buy -> false -> 0，Sell -> true -> 1。
    return static_cast<std::size_t>(side == Side::Sell);
}

bool update_level(std::array<Level, 2>& levels,
                  Side side, Level value) noexcept {
    const auto index = side_index(side);
    if (!index) return false;  // 非法方向：拒绝更新，不访问数组。

    // 检查 optional 是否有值，而不是检查 *index 是否非零。
    // 下标 0 是合法的买方槽位。
    levels[*index] = value;
    return true;
}

bool side_index_example() {
    std::array<Level, 2> levels{};
    if (!update_level(levels, Side::Buy,  Level{100, 10})) return false;
    if (!update_level(levels, Side::Sell, Level{101, 20})) return false;

    // 0 不是 Side 中定义的有效方向，应返回 false，两个槽位保持不变。
    const bool invalid_accepted = update_level(
        levels, static_cast<Side>(0), Level{999, 99});

    return !invalid_accepted &&
           levels[0].price_ticks == 100 && levels[0].qty == 10 &&
           levels[1].price_ticks == 101 && levels[1].qty == 20;
}
```

这段代码把买卖方向映射为数组下标，后续读写就可以共用一套逻辑。

分支方向难以预测时，用少量条件选择或算术操作代替跳转，可以减少反复预测错误的开销；分支高度可预测时，原来的跳转往往开销更小。前面的订单簿搜索就是一个例子，branch misses 和核心周期能帮助解释两种实现的差异。

<a id="hft02-4-4"></a>
### 4.4 循环展开和多路计算

下面的求和存在连续依赖：下一次加法要使用上一次的 `sum`。拆成几个独立累加器，可以给 CPU 更多并行执行的机会；同时展开循环，还能减少循环控制指令的执行次数。

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

多路累加拆开的是算术依赖。若时间主要花在间接读取上，还可以把一个小批次的数据读取与后续耗时较长的计算分开，让多个独立访存请求并行处理。这称为内存级并行（Memory-Level Parallelism，MLP）。

例如，一条行情需要更新一组彼此独立的记录。逐项读取后立即执行一长串计算，可能让有限的乱序执行窗口容纳不了更后面的读取。先读取一小批输入，再集中计算，就能把这批读取请求提前：

[![逐项处理在每次读取后执行长计算；批内重排先收集A、B、C输入，再计算各项，让独立访存有机会重叠。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-mlp-batches.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-mlp-batches.png)

下面用带依赖链的计算模拟每条记录的处理工作，输入只读、输出独立。

```cpp
#include <algorithm>
#include <array>
#include <cstddef>
#include <span>

struct PricingInput {
    double price;
    double reference;
    double state;
};

double evaluate_sample(const PricingInput& input) noexcept {
    double x = input.price;
    for (unsigned step = 0; step < 16; ++step)
        x = x * 1.000001 + input.state;
    return x - input.reference;
}

// 两个版本的前提相同：ids 已校验，out.size() >= ids.size()；
// out 不与 table、ids 重叠，调用期间输入不被其他线程修改。
void evaluate_serial(std::span<const PricingInput> table,
                     std::span<const std::size_t> ids,
                     std::span<double> out) noexcept {
    for (std::size_t i = 0; i < ids.size(); ++i)
        out[i] = evaluate_sample(table[ids[i]]);
}

template<std::size_t D>
void evaluate_batched(std::span<const PricingInput> table,
                      std::span<const std::size_t> ids,
                      std::span<double> out) noexcept {
    static_assert(D > 0);
    std::array<PricingInput, D> gathered;
    for (std::size_t base = 0; base < ids.size();) {
        const auto count = std::min(D, ids.size() - base);

        // 第一遍：连续发出这一批相互独立的间接读取。
        // 这里没有等待多个行情到达，处理的都是当前已知的工作项。
        for (std::size_t k = 0; k < count; ++k)
            gathered[k] = table[ids[base + k]];

        // 第二遍：在已收集的输入上计算，输出顺序与串行版本相同。
        for (std::size_t k = 0; k < count; ++k)
            out[base + k] = evaluate_sample(gathered[k]);

        base += count;  // 也处理最后不足 D 项的尾部。
    }
}
```

`evaluate_batched<4>` 每次先准备四条输入，再集中计算，代价是临时数组和额外的读写。进一步预取下一批时，把 prefetch 放在当前批计算之前，就能利用这段计算时间等待数据。

这里处理的是同一事件中已经就绪的一批数据，无须等待新行情。本例输入只读、输出独立，所以可以调整取数与计算的顺序；读写同一张状态表时，则要保留同一 ID 的多次更新之间的先后顺序。

批次增大会占用更多 LFB、寄存器和栈空间。它的收益来自访存等待的重叠，所以 miss 数未降、指令数上升时，处理速度仍可能提高。核心周期和 fill-buffer-full 停顿可以帮助观察批大小的影响。

<a id="hft02-4-5"></a>
### 4.5 SIMD 自动向量化与 __restrict__

SIMD（Single Instruction, Multiple Data）用一条指令同时处理多个数据元素。让同类数据连续存放、减少循环中的调用，并消除阻碍并行计算的数据依赖，有利于编译器生成向量指令。

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

向量化除了算术指令，还有装载、重排、类型转换和结果整理的成本。比较整段计算时，要把相对于标量实现新增的数据组织操作也算进去。

假设四组报价已经在内存中，要分别计算价差，AoS 和 SoA 所需的数据组织工作就不同：

```cpp
#include <array>
#include <cstddef>

// 简化的布局示例，用 float 展示四个元素的向量运算。
struct QuotePair {
    float bid;
    float ask;
};

std::array<float, 4> spreads_aos(const std::array<QuotePair, 4>& quotes) {
    std::array<float, 4> result;
    for (std::size_t i = 0; i < result.size(); ++i)
        result[i] = quotes[i].ask - quotes[i].bid;
    return result;
}
```

在常见布局下，报价字段按下面的顺序排列。采用四路向量减法时，可以先准备两组操作数，让同一组报价的买卖价处于对应的元素位置，也就是同一个 lane：

[![AoS内每条记录包含相邻的bid与ask；向量运算先组织ask和bid两个操作数，使对应报价处于同一个lane，再逐lane相减。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-aos-simd-lanes.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-aos-simd-lanes.png)

从交错排列的字段得到这两组操作数，一种做法是先连续加载，再用 shuffle 指令调整元素位置；对于位置更分散的数据，也可以使用 gather，把多个地址上的元素收集到向量寄存器中。

如果报价原本就按 SoA 保存，同一侧的四个价格已经连续，可以直接作为向量装载的输入：

```cpp
#include <array>
#include <cstddef>

struct QuotesSoA {
    std::array<float, 4> bid;  // bid0 bid1 bid2 bid3
    std::array<float, 4> ask;  // ask0 ask1 ask2 ask3
};

std::array<float, 4> spreads_soa(const QuotesSoA& quotes) {
    std::array<float, 4> result;
    for (std::size_t i = 0; i < result.size(); ++i)
        result[i] = quotes.ask[i] - quotes.bid[i];
    return result;
}
```

<a id="hft02-4-6"></a>
### 4.6 浮点精度混用

浮点类型混用可能引入额外的转换开销。例如，`float` 变量乘上不带后缀的浮点字面量，会先提升到 `double`：

```cpp
float mixed_precision(float value) {
    return static_cast<float>(value * 1.23);       // 提升到 double 后计算
}

float single_precision(float value) {
    return value * 1.23f;                         // float 精度下计算
}
```

<a id="hft02-4-7"></a>
### 4.7 signed/unsigned、整数转换与循环优化

下面两个函数只统计循环次数，参数 `i` 都是 `int`，区别在于循环变量 `k` 的类型：

```cpp
int f_signed(int i) {
    int count = 0;
    for (int k = i; k < i + 10; ++k)
        ++count;
    return count;
}

int f_unsigned(int i) {  // 注意：参数 i 仍是 int。
    int count = 0;
    for (unsigned k = i; k < i + 10; ++k)
        ++count;
    return count;
}
```

signed 版本：

```asm
f(int):
    mov eax, 10
    ret
main:
    sub rsp, 8
    call rand
    mov eax, 10
    add rsp, 8
    ret
```

unsigned 循环变量版本：

```asm
f(int):
    lea eax, [rdi+10]
    cmp edi, eax
    sbb eax, eax
    and eax, 10
    ret
main:
    sub rsp, 8
    call rand
    lea edx, [rax+10]
    cmp eax, edx
    sbb eax, eax
    add rsp, 8
    and eax, 10
    ret
```

C++ 标准规定，有符号整数溢出是未定义行为。在默认溢出语义下，signed 版本对所有有定义输入都执行 10 次，所以编译器可以直接生成 `mov eax, 10`，省去循环和比较。[C++ 整数运算规则](https://eel.is/c++draft/basic.fundamental#2)

unsigned 版本中，`i + 10` 仍按 `int` 计算，溢出同样属于未定义行为；比较时，结果会转换成 `unsigned`。例如 `i = -5` 时，`k` 转换成很大的无符号值，上界却是 `5`，循环执行 0 次。返回值随输入为 0 或 10，因此编译器保留了比较和结果选择指令。[整数转换规则](https://eel.is/c++draft/conv.integral#3)、[混合类型运算规则](https://eel.is/c++draft/expr.arith.conv#1.5.3)

选择类型首先要满足业务的取值范围和溢出语义。下表示例以 `int` / `unsigned int`、默认溢出语义和优化编译为前提：

| 场景 | 哪种类型可能受益 | 原因与前提 |
|---|---|---|
| 比较或循环可以利用“不发生有符号溢出”的条件 | signed | 例如 `int x` 的 `x + 1 > x` 可简化为真；unsigned 版本还要处理最大值加 1 后回绕到 0 |
| 除以正的 2 的幂，例如 `x / 8` | unsigned | 可以简化为逻辑右移；signed 输入允许负值时，为满足向零截断的规则，一般会增加修正操作 |
| 对正的 2 的幂取余，例如 `x % 8` | unsigned | 可以简化为 `x & 7`；signed 输入允许负值时，还要保留负余数的语义 |
| 相同位宽的普通加减与乘法，只需同位宽结果 | 一般没有固有优势 | signed 运算不溢出的前提下，常见处理器上往往使用相同指令，差异更多来自周围代码能否进一步优化 |

<a id="hft02-5"></a>
## 5. Stop-bit 变长整数：用短字段减少数据搬运

行情协议也会使用变长整数。例如，上交所 Level-2 产品使用 FIX/FAST 接口，FAST 的整数采用 stop-bit 编码。大量字段值较小时，它能减少传输量和缓冲区占用，解码时再识别字段终点、重建整数。[上证所 Level-2 产品说明](https://www.sseinfo.com/services/assortment/level2/)

<a id="hft02-5-1"></a>
### 5.1 先固定编码规则，避免混用不同 varint

本例实现 **FAST 风格、非空、无符号 `uint64_t` 的最短编码**：每个字节的低 7 位携带数据，最高位为 `1` 表示最后一个字节，为 `0` 表示后面还有字节；高位数据组先发送。这里不是 Protobuf 的低位组在前、最高位为 `1` 表示继续的 varint。

| 数值 | 线上字节，十六进制 | 说明 |
|---|---|---|
| 0 | `80` | 一个结束字节，载荷为零 |
| 127 | `FF` | 一个字节能表示的最大无符号值 |
| 128 | `01 80` | `(1 << 7) \| 0` |
| 300 | `02 AC` | `(2 << 7) \| 44` |
| `UINT64_MAX` | `01 7F 7F 7F 7F 7F 7F 7F 7F FF` | 最多需要 10 字节 |

完整范围的 64 位整数用 varint 编码时，最长会超过定长的 8 字节。若相邻价格差值很小，可以先用差分编码把绝对价格变成小整数，再用 stop-bit 编码减少字节数；两种优化处理的是不同问题。

<a id="hft02-5-2"></a>
### 5.2 编解码实现：短值快路径，长值有界展开

下面是 stop-bit 变长编码的 C++20 标量实现，未使用 SIMD。它单独处理常见的一、两字节整数；对于较长整数，输入至少还剩 10 字节时才执行展开后的代码，省去逐字节检查输入边界的操作。

编码时，`std::bit_width` 计算所需字节数，再直接向目标缓冲区倒序填入数据组，在线上仍是高位组在前，省去了临时数组和第二遍反转。`encode` 检查容量；调用方已保证空间充足时，可以使用 `encode_unchecked`。

```cpp
#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>
#include <type_traits>
#include <utility>

namespace stopbit {
// FAST-style mandatory unsigned integer, shortest representation only.
enum class Status { done, need_more, invalid };
struct UIntState {
    std::uint64_t value = 0;
    unsigned count = 0;
};
struct Result {
    Status status;
    std::size_t consumed; // Bytes consumed from THIS span, including an invalid byte.
};

constexpr std::size_t encoded_size(std::uint64_t value) noexcept {
    return std::max<std::size_t>(1, (std::bit_width(value) + 6) / 7);
}

// Caller guarantees enough writable bytes. Returns bytes written.
inline std::size_t encode_unchecked(std::uint64_t value,
                                    std::uint8_t* out) noexcept {
    if (value < 128) {
        out[0] = static_cast<std::uint8_t>(value | 0x80);
        return 1;
    }
    const auto size = encoded_size(value);
    out[size - 1] = static_cast<std::uint8_t>((value & 0x7f) | 0x80);
    for (auto i = size - 1; i != 0; ) {
        value >>= 7;
        out[--i] = static_cast<std::uint8_t>(value & 0x7f);
    }
    return size;
}

// Zero means insufficient capacity; output remains unchanged.
inline std::size_t encode(std::uint64_t value,
                          std::span<std::uint8_t> out) noexcept {
    if (out.size() < encoded_size(value)) return 0;
    return encode_unchecked(value, out.data());
}

namespace detail {
// Used only when at least 10 input bytes are accessible. I is a byte index.
// Compile-time expansion removes loop bookkeeping and per-byte bounds checks.
template<unsigned I>
inline Result unrolled(const std::uint8_t* p, UIntState& s) noexcept {
    const auto b = p[I];
    if constexpr (I == 9) {
        if (s.value > (UINT64_MAX >> 7) || (b & 0x80) == 0)
            return {Status::invalid, I + 1};
    }
    s.value = (s.value << 7) | (b & 0x7f);
    if (b & 0x80) {
        s.count = 0;
        return {Status::done, I + 1};
    }
    if constexpr (I < 9) return unrolled<I + 1>(p, s);
    return {Status::invalid, I + 1};
}
} // namespace detail

// s must be initialized, or retained from need_more; discard it after invalid.
// On done, s.value is the decoded value and s.count == 0 (ready for next field).
inline Result decode(std::span<const std::uint8_t> input,
                     UIntState& s) noexcept {
    if (input.empty()) return {Status::need_more, 0};
    std::size_t i = 0;
    if (s.count == 0) {
        const auto b = input[i++];
        s.value = b & 0x7f;
        if (b & 0x80) return {Status::done, 1}; // Single-byte fast path.
        if (b == 0) return {Status::invalid, 1}; // Redundant leading zero group.
        s.count = 1;
        if (input.size() == 1) return {Status::need_more, 1};
        const auto second = input[i++];
        s.value = (s.value << 7) | (second & 0x7f);
        if (second & 0x80) {
            s.count = 0;
            return {Status::done, 2};
        }
        s.count = 2;
        if (input.size() >= 10)
            return detail::unrolled<2>(input.data(), s);
    }
    // Short input or a field split across receives: resume without rescanning.
    while (i < input.size()) {
        const auto b = input[i++];
        if (s.count == 9 &&
            (s.value > (UINT64_MAX >> 7) || (b & 0x80) == 0))
            return {Status::invalid, i};
        s.value = (s.value << 7) | (b & 0x7f);
        ++s.count;
        if (b & 0x80) {
            s.count = 0;
            return {Status::done, i};
        }
    }
    return {Status::need_more, i};
}
} // namespace stopbit
```

`decode` 一次只解码一个整数，结果分为 `done`、`need_more` 和 `invalid`。`consumed` 始终表示**本次输入**中已处理的字节数；`need_more` 时保留 `UIntState`，下一次只传入新字节，避免重复处理旧字节。`done` 后 `value` 是完整结果，`count` 归零，下一次调用即可解析下一个字段。

<a id="hft02-6"></a>
## 6. 用状态机从字节流解析变长结构体

TCP 不保留应用消息边界。一次接收既可以只有某个整数的前半部分，也可以包含多条行情和下一条行情的一部分。stop bit 只标记字段结束，字段的位置和整条行情的完成状态还要由解析器维护。

用一个基础行情格式说明状态机：每条消息按顺序包含 `instrument_id、sequence、price_ticks、quantity` 四个非空无符号整数。

[![消息A的四个字段编码为81、01 80、02 AC、8A。三次接收逐步完成id、seq、price、qty，第三次末尾的82开始下一条消息。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-stream-decoding.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-stream-decoding.png)

这里有两层状态：

| 状态 | 保存什么 | 什么时候推进 |
|---|---|---|
| 字段内部状态 `integer_` | 已累计的整数值和字节数 | 收到 stop bit，才得到一个完整字段 |
| 消息状态 `field_` | 当前正在解析四个字段中的哪一个 | 一个字段完成后转到下一个；第四个字段完成后将消息传给回调，再回到第一个字段 |

每条连接持有一个 `QuoteStream`。解析器直接对输入字节做移位和合并，处理后即可丢弃这些字节；字段或消息跨多次接收时，只保留整数的解析进度和未完成的结构体，省去了拼接字符串、动态分配和搬移残留字节的操作。


```cpp
namespace stopbit {
// Teaching wire schema: exactly four mandatory unsigned fields, in this order.
// Not a real exchange message, nor the native memory layout of this struct.
struct Quote {
    std::uint64_t instrument_id;
    std::uint64_t sequence;
    std::uint64_t price_ticks;
    std::uint64_t quantity;
    bool operator==(const Quote&) const = default;
};

inline std::size_t encode_quote(const Quote& q,
                                std::span<std::uint8_t> out) noexcept {
    const std::array values{q.instrument_id, q.sequence, q.price_ticks, q.quantity};
    std::size_t size = 0;
    for (auto v : values) size += encoded_size(v);
    if (out.size() < size) return 0;
    auto* p = out.data();
    for (auto v : values) p += encode_unchecked(v, p);
    return size;
}

class QuoteStream {
    Quote current_{};
    UIntState integer_{};
    unsigned field_ = 0;
    bool failed_ = false;
public:
    bool at_message_boundary() const noexcept {
        return !failed_ && field_ == 0 && integer_.count == 0;
    }

    // Emit must synchronously accept the quote, must not throw or re-enter.
    // Copy it if needed later: current_ is reused for subsequent messages.
    // done means this input ended at a message boundary, not connection EOF.
    template<class Emit>
    Result feed(std::span<const std::uint8_t> input, Emit&& emit) noexcept {
        static_assert(std::is_nothrow_invocable_v<Emit&, const Quote&>);
        if (failed_) return {Status::invalid, 0};
        std::size_t used = 0;
        while (used < input.size()) {
            const auto r = decode(input.subspan(used), integer_);
            used += r.consumed;
            if (r.status == Status::invalid) {
                failed_ = true;
                return {Status::invalid, used};
            }
            if (r.status == Status::need_more) return {Status::need_more, used};
            switch (field_) {
                case 0: current_.instrument_id = integer_.value; break;
                case 1: current_.sequence = integer_.value; break;
                case 2: current_.price_ticks = integer_.value; break;
                case 3: current_.quantity = integer_.value; break;
            }
            if (++field_ == 4) {
                emit(std::as_const(current_));
                field_ = 0;
            }
        }
        return {at_message_boundary() ? Status::done : Status::need_more, used};
    }
};
} // namespace stopbit
```

使用方式如下。`received_bytes` 只覆盖本次实际收到的字节，解析器对象在连接存续期间保留：

```cpp
stopbit::QuoteStream parser; // 放在连接对象中，不是每次接收时重新构造。

// 接收回调中：on_quote 必须同步接纳数据、noexcept，且不能重入 parser。
auto result = parser.feed(received_bytes, on_quote);
if (result.status == stopbit::Status::invalid) {
    // 停止使用该流，由连接管理层处理错误、断线和恢复。
}
// need_more 不需要阻塞等待；下次有新字节时再次调用 feed。
// TCP EOF 时另外检查 parser.at_message_boundary()，false 表示有残缺消息。
```

<a id="hft02-7"></a>
## 7. 总结

预分配把准备工作移出热路径，紧凑布局减少数据搬运，预取和批内并行让访存等待与计算重叠。编码与解析也沿着同一思路：少存一份副本，少搬一段字节，让收到的数据尽早参与处理。

下一篇《C++线程通信与性能测量》把视角移到多个线程之间，讨论 SPSC、Disruptor 和缓存行交接，再用分段时间戳、基准测试与性能计数器分析延迟。

<a id="hft02-references"></a>
## 参考资料

- C++ 低延迟设计：[Building Low Latency Applications with C++](https://github.com/PacktPublishing/Building-Low-Latency-Applications-with-CPP)、[C++ design patterns for low-latency applications including high-frequency trading](https://arxiv.org/abs/2309.04259)。
- 订单簿布局与搜索：David Gross，[When Nanoseconds Matter](https://github.com/CppCon/CppCon2024/blob/main/Presentations/When_Nanoseconds_Matter.pdf)，CppCon 2024。
- 内存级并行、预取与缓存淘汰：Sanchit Gupta，[The Latency Slippage](https://github.com/CppCon/CppCon2026/blob/6f687701cdb4099eae39011485065b52ab7da0e9/Presentations/The_Latency_Slippage.pdf)，其中[第 15–20 页](https://github.com/CppCon/CppCon2026/blob/6f687701cdb4099eae39011485065b52ab7da0e9/Presentations/The_Latency_Slippage.pdf#page=15)讨论批处理，[第 22–28 页](https://github.com/CppCon/CppCon2026/blob/6f687701cdb4099eae39011485065b52ab7da0e9/Presentations/The_Latency_Slippage.pdf#page=22)讨论预取，[第 34–35 页](https://github.com/CppCon/CppCon2026/blob/6f687701cdb4099eae39011485065b52ab7da0e9/Presentations/The_Latency_Slippage.pdf#page=34)对比缓存压力；固定版本源码：[两阶段批处理](https://github.com/0xsanchit/CacheLatency/blob/5ea23c07d524a057179e6db52b8038131927ffd1/src/exp/10b_options_batched.cpp#L186-L214)、[事件计时与清除位置](https://github.com/0xsanchit/CacheLatency/blob/5ea23c07d524a057179e6db52b8038131927ffd1/src/exp/10g_options_clwb.cpp#L327-L398)。
- LMAX 工程实践：[LMAX Group 技术博客](https://technology.lmax.com/)。
