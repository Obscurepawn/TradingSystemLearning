# HFT Engineering从入门到入土（2）—— 常见低延迟系统访存与计算的优化手段

目前主流 HFT 系统已普遍达到亚微秒级交易延迟。围绕这一目标，编程时要深入计算、访存和 I/O，检查每条指令、每次数据访问带来的纳秒级开销。这与以高吞吐为主要目标的系统有很大不同。

这篇文章聚焦进程内的访存与计算，网络 I/O 与内核旁路优化放在下一篇讨论。

这篇文章所列举的优化手段主要参考了 [Building Low Latency Applications with C++](https://github.com/PacktPublishing/Building-Low-Latency-Applications-with-CPP)、[C++ design patterns for low-latency applications including high-frequency trading](https://arxiv.org/abs/2309.04259)，以及 [LMAX Group 的技术博客](https://technology.lmax.com/)。

## 1. 延迟优化的前提是先要具备测量的手段

看到哈希表、虚函数或动态内存分配，很容易想到几种优化办法。选择从哪里动手，先看一条行情从进入程序到产生订单，时间到底花在了哪里。

在以吞吐为主要目标的应用中，基于 steady clock 的软件时间戳已能满足大部分延迟测量需求，读钟开销也容易被忽略。处理时间缩短到亚微秒后，测量本身就值得单独研究。

### 1.1 先确定测量的起点和终点

一次行情驱动的交易，大致经过下面这条路径：

```text
行情进入网卡 → 应用取得行情 → 解码 → 更新订单簿 → 策略 → 风控与编码
    H0            C0                                      C4
                                                          │
                          交易所处理 ← 网络 ← 网卡发出 ← 提交发送
                                               H1
```

`C4 - C0` 是应用取得行情到订单准备完毕的进程内耗时；`H1 - H0` 则覆盖本机从收行情到发订单的路径，常被称为 tick-to-trade latency。后者包含网卡与主机之间的数据传递，以及收发路径中的等待。相减的前提是两端时钟可比较；来自不同时钟域的时间戳，例如网卡上的 TSC 和 CPU 上的 TSC，在计算前要先确认两者的关系。

`H0` 和 `H1` 可以来自网卡硬件时间戳，即网卡在收发报文经过特定位置时记录的时刻。它比应用调用收发函数时打点更接近网络边界。在支持该功能的 Linux socket 路径中，可以用 `SO_TIMESTAMPING` 请求硬件时间戳；接收时间戳随报文返回，一般会替换帧中的部分内容，例如可能用时间戳替换以太网帧的 FSC，代价是无法再用 CRC 校验数据。发送时间戳则在发送后从 socket 错误队列异步读取。接口和关联方法见 [Linux 时间戳文档](https://docs.kernel.org/networking/timestamping.html)。

发送硬件时间戳记录本机的发送边界。`send()` 返回、网卡发出、交易所接受订单、订单成交，是不同的完成点。这里测量 `C0` 到 `C4` 之间的计算、访存和线程交接。

### 1.2 沿同一条消息拆分进程内耗时

先看所有业务阶段都在同一线程中执行的情况。与其给每个阶段分别读两次时钟，可以在相邻阶段之间共用一个时间点：

```text
C0          C1               C2           C3                C4
│── 解码 ──│── 更新订单簿 ──│── 策略 ──│── 风控与编码 ──│
└──────────────── 应用处理总耗时 ───────────────────────────┘
```

这样只需五个时间点，就能计算四段耗时和总耗时：

| 区间 | 计算方式 | 这段变慢时可以继续检查什么 |
|---|---|---|
| 行情解码 | `C1 - C0` | 字段解析、字符串构造、额外复制 |
| 订单簿更新 | `C2 - C1` | 查找、节点分配、缓存未命中 |
| 策略决策 | `C3 - C2` | 特征计算、数据访问、分支路径 |
| 风控与编码 | `C4 - C3` | 账户状态读取、订单构造、格式化 |
| 应用处理总耗时 | `C4 - C0` | 上述串行路径的整体表现 |

下面用 `std::chrono::steady_clock` 实现分段打点，四个回调由调用方提供。

```cpp
#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>

using TraceClock = std::chrono::steady_clock;

struct StageTrace {
    std::uint64_t event_id;
    std::array<TraceClock::time_point, 5> time;
};

template<class Decode, class UpdateBook, class Decide, class RiskEncode>
StageTrace measure_stages(std::uint64_t event_id,
                          Decode&& decode, UpdateBook&& update_book,
                          Decide&& decide, RiskEncode&& risk_encode) {
    StageTrace trace{event_id, {}};
    trace.time[0] = TraceClock::now();
    decode();
    trace.time[1] = TraceClock::now();
    update_book();
    trace.time[2] = TraceClock::now();
    decide();
    trace.time[3] = TraceClock::now();
    risk_encode();
    trace.time[4] = TraceClock::now();
    return trace;
}

// 在统计线程中调用
auto stage_durations_ns(const StageTrace& trace) {
    std::array<std::int64_t, 4> result{};
    for (std::size_t i = 0; i < result.size(); ++i) {
        result[i] = std::chrono::duration_cast<std::chrono::nanoseconds>(
            trace.time[i + 1] - trace.time[i]).count();
    }
    return result;
}
```

这里保留原始时间点，把单位换算和统计留到后面。同一条记录中的 `event_id` 用来把各阶段关联起来。

### 1.3 选时钟时，先考虑可比较性，再考虑成本

`steady_clock` 易于使用，保证时间点不倒退，适合计算时间间隔，能够满足大部分应用的计时需求。它内部还会把 TSC 计数转换成 Unix 时间戳；在低延迟路径中，我们希望把这类转换移到指标上报线程。

当计算和访存耗时短到读钟成本不可忽略时，在 x86 上可以直接研究 TSC（Time Stamp Counter，时间戳计数器）。`RDTSC`、`RDTSCP` 可以读取它，差值按 TSC 频率换算：

```text
elapsed_ns = (end_tsc - begin_tsc) × 1,000,000,000 / tsc_hz
```

这里的 `tsc_hz` 是经过确认的 TSC 频率。用 CPU 当前的睿频频率代替它，会把核心频率变化引入时间换算。

裸 `RDTSC` 不能阻止 CPU 乱序执行，`RDTSCP` 也不是完整的双向序列化屏障。测量短区间时，按目标处理器和编译器的语义插入屏障，才能约束计时边界。

以 Intel x86-64、GCC/Clang 为例，读钟可以封装成下面的形式：

```cpp
#include <cstdint>

inline std::uint64_t ordered_rdtsc() noexcept {
    std::uint32_t lo, hi;
    asm volatile(
        "lfence\n\t"
        "rdtsc\n\t"
        "lfence"
        : "=a"(lo), "=d"(hi)
        :
        : "memory"
    );
    return (static_cast<std::uint64_t>(hi) << 32) | lo;
}
```

| 写法 | 解决的问题 |
|---|---|
| 前面的 `lfence` | 等此前指令完成到相应执行边界，避免读钟提前执行 |
| 后面的 `lfence` | 阻止后续指令越过读钟位置提前执行 |
| `asm volatile` | 避免汇编因输出未使用而被删除；单独使用不保证周围代码不重排 |
| `"memory"` clobber | 告诉编译器该汇编可能读写内存，约束周围内存访问的移动 |

`"memory"` 在编译器层面约束内存操作的重排，不会自行生成 CPU 屏障指令；`LFENCE` 则是实际执行的机器指令。把三条指令写在同一个汇编块中，也避免了编译器在它们之间插入其他指令。

纯寄存器计算仍可能移出计时区间，`"memory"` 只约束内存操作。下面的乘法就可能被提前计算、折叠或删除，因此这段代码不能直接用作可靠的乘法微基准：

```cpp
auto begin = ordered_rdtsc();
auto result = x * 123;  // 仍可能被提前计算、常量折叠，或因结果未使用而删除，编译器会有各种优化的手段。
auto end = ordered_rdtsc();
```

检查编译产物的汇编，可以确认读钟与目标计算的位置是否符合预期。

如果两个时间点来自不同 CPU 核，尤其是不同 CPU 插槽，先验证 TSC 是否一致。生产交易系统常通过精细的亲和性部署减少跨插槽的 TSC 误差，策略、交易和风控进程之间通过共享内存通信。

网卡的 PTP 硬件时钟（PHC）和 CPU 的 TSC 属于不同的时钟域，起点和计数速率可以不同。计算 `应用时间 - 网卡时间` 前，先建立时钟映射并评估误差；只把单位统一成纳秒还不够。跨机器测量还涉及时钟同步，PTP 提供了相应机制。相关基础接口见 [Linux PHC](https://docs.kernel.org/driver-api/ptp.html)。

测量进程内各阶段时，先统一使用一种可靠的单调时钟。全链路测量留到后续章节。

### 1.4 既看分布，也保留同一条消息的分段记录

吞吐量代表一秒处理多少消息，延迟代表一条消息要等多久。批量处理可以提高吞吐量，但等待组批会让先到的消息多等一段时间；增加工作线程可以分摊计算，跨线程交接也会引入队列与缓存一致性开销。观察吞吐量、p50、p99、p99.9 和测试期间的最慢样本，才能看到这些取舍。平均值会掩盖少量慢请求。

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

对于前面的 `StageTrace`，可以为四个阶段分别建立记录器，再单独建立一个总耗时记录器。用 `record(trace.time[0], trace.time[4])` 记录总耗时，用相邻时间点记录各阶段。正式热路径中更适合先保存整条 `StageTrace`，随后在停止测量或安全移交给统计线程后做这些操作，避免边处理业务边更新多个统计容器。

各阶段的 p99 不能相加得到总耗时的 p99，因为各阶段的慢样本未必来自同一条消息。先对每条消息计算总耗时，再对这些总耗时求分位数。

### 1.5 控制测量本身的干扰

读钟、写时间戳和搬运统计数据都会消耗 CPU、缓存和内存带宽。当整个消息的处理时间很短时，增加五次时钟读取就可能明显改变结果；这些打点适合定位阶段边界，投入生产前还要评估扰动。

热路径只保存必要的整数、时间点和标识，格式化、排序、分位数计算和文件输出放到路径之外。每个线程独占自己的记录缓冲区，后台通过所有权移交或队列协议读取。

### 1.6 用 SPSC 队列把时间戳交给统计线程

同一进程里的线程共享地址空间，可以直接上报时间戳，无须另建一块 `mmap` 共享内存。交接时的顺序是关键：worker 写完后统计线程才能读，统计线程读完后 worker 才能覆盖。

这里可以直接使用第 2.5.3 节的 SPSC 环形队列。SPSC 表示单生产者、单消费者：一个 worker 负责写入，一个统计线程负责读取。队列在启动时创建，两端持有同一个队列对象的引用，其寿命覆盖两端线程的整个运行期。

```text
worker                       预分配的环形队列                  统计线程
业务处理、记录时间戳
        │
完整的 StageTrace
        └── try_push ──────→ [记录][记录][空槽] ─── try_pop ──→ 局部副本
        │                                                       │
继续下一条业务消息                                         计算时间差、更新直方图
                                                          定期上报聚合指标
```

统计线程读取的是队列内部数组中的槽位，不是 worker 栈上的 `trace`。一次交接按下面的顺序完成：

1. worker 检查槽位可用，把完整记录复制进去，再通过 release 操作发布写入位置。
2. 统计线程通过 acquire 操作观察到新位置，才读取对应槽位，将记录复制到自己的局部变量。
3. 统计线程通过 release 操作发布消费进度；worker 通过 acquire 观察到空间后，才能复用槽位。统计计算继续使用局部副本。

```cpp
// worker 调用。dropped 只由这个 worker 访问；满队列不重试。
template<class Queue>
void submit_trace(Queue& queue, const StageTrace& trace,
                  std::uint64_t& dropped) {
    if (!queue.try_push(trace)) ++dropped;
}

// 统计线程调用。aggregate 在本线程内更新直方图，不做网络 I/O。
// 每轮最多取 256 条已有记录，不等待凑够一个批次。
template<class Queue, class Aggregate>
std::size_t drain_traces(Queue& queue, Aggregate&& aggregate) {
    StageTrace local{};
    std::size_t count = 0;
    while (count < 256 && queue.try_pop(local)) {
        aggregate(local.event_id, stage_durations_ns(local));
        ++count;
    }
    return count;
}
```


### 1.7 延迟数据交接大概要付出多少延迟

| 指标 | 起点与终点 | 用途 |
|---|---|---|
| worker 入队耗时 | 调用 `try_push` 到返回 | 估算业务线程增加的直接成本 |
| 记录交接延迟 | 入队前到统计线程成功取出 | 检查发布、跨核传递、排队和轮询等待 |
| 指标上报延迟 | 记录产生到聚合结果上报完成 | 判断监控数据的新鲜度 |

worker 入队后即可继续处理业务，统计线程异步取出记录并上报。`try_push()` 返回只表示本次入队调用结束，统计线程可以更早或更晚读到记录。交接延迟和上报延迟与 worker 的处理耗时存在重叠，不能直接相加。

以常见的 64 位实现为例，如果一个 `time_point` 占 8 字节，`StageTrace` 就是 8 字节 ID 加 5 个时间点，共 48 字节，实际大小用 `sizeof(StageTrace)` 确认。成功入队主要做一次判满、复制这 48 字节和发布游标。

一个公开的数量级参照是 [Rigtorp SPSCQueue 基准](https://github.com/rigtorp/SPSCQueue#benchmarks)：在 AMD Ryzen 9 3900X 的同一 chiplet、不同核心上，用两个队列传递 `int`，报告的往返延迟为 **133 ns**。这个数值覆盖一次往返，单次 `try_push()` 的成本还要单独测量。

做预算敏感性分析时，可以先把 worker 入队成本设在几十到几百纳秒之间，观察它对总预算的影响。

## 2. 常见的 Latency 优化手段

<a id="prepare"></a>

### 2.1 编译期计算与静态分派

#### 2.1.1 constexpr 编译期计算与查表

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

#### 2.1.2 函数模板、CRTP 与编译期静态分派

每条消息都经由虚函数或 `std::function` 调用策略时，如果编译器无法确定实际目标，就会留下动态分派。对于启动后不变的行为，可以把选择放在外层，让内层处理具体类型。

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

`static_cast` 不检查对象的动态类型，因此模板参数与实际派生对象匹配，且基类可访问、无歧义、非虚，才满足这里的转换条件。其他不匹配的类型若继承 `RuleBase<CrtpSpreadRule>` 后调用 `accept()`，就会破坏这一前提；示例中的 protected 构造函数只限制独立创建基类对象，继承用法仍由代码保证。相关转换条件见 [C++ static_cast 规则](https://eel.is/c++draft/expr.static.cast)。

CRTP 与普通函数模板都能实现静态分派。CRTP 让基类调用派生类提供的实现，适合复用公共接口或处理流程；如果只是调用 `rule.accept(q)`，普通函数模板更直接。`RuleBase<A>` 和 `RuleBase<B>` 是两个不同类型，无法像同一个虚基类那样直接放进统一的基类指针容器。两种写法也可能生成相同的机器码，仅为了性能而引入 CRTP 没有必要。

插件和运行时替换适合保留动态分派；目标可推断时，编译器有机会自行去虚拟化。静态分派的代价是模板实例化带来的代码增长，实例过多会增加指令缓存压力。

<a id="memory"></a>

### 2.2 内存分配、零拷贝视图与索引查找

#### 2.2.1 预分配与固定容量缓冲区

动态内存分配除了平均耗时，还存在分配器内部竞争、扩容时数据搬移，以及首次触页时 minor page fault 的风险。低延迟路径常在启动时准备内存，处理消息时复用。

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

#### 2.2.2 对象池与空闲链表

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

`next_` 保存的是链表关系，不是数组中的相邻关系。初始链恰好按下标递增，只是因为构造函数这样连接；经历申请和归还后，它完全可以变成 `3 -> 0 -> 1 -> 2`。订单在 `objects_` 中的位置不动，变化的只有管理下标。

例如，`OrderPool<4>` 当前的槽位 `0、1、2` 空闲，槽位 `3` 已借出，空闲链是：

```text
head_ = 0
空闲链：0 -> 1 -> 2 -> 4（结束标记）
已借出：3
```

现在调用 `release(3)`，虽然旧 `head_ = 0` 的数值比 `3` 小，仍然执行同样的头插操作：

```cpp
// release(3) 内部的状态变化：
used_[3] = false;
next_[3] = head_;  // next_[3] = 0：新归还的槽位指向旧链表头。
head_ = 3;        // 再把新归还的槽位设为链表头。
```

结果是：

```text
head_ = 3
空闲链：3 -> 0 -> 1 -> 2 -> 4（结束标记）
```

下一次 `acquire()` 返回 `3`，并把 `head_` 更新为 `next_[3]`，也就是 `0`。这里的“前后”指链表中的逻辑顺序，与数组下标大小无关。若原来没有空闲槽位，即 `head_ == N`，归还后就是 `i -> N`，同样成立。

#### 2.2.3 非必要不创造数据副本

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

#### 2.2.4 返回值优化（NRVO）与隐式移动

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

#### 2.2.5 稠密 ID 映射与数组直接索引

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

<a id="layout"></a>

### 2.3 数据布局与缓存局部性

#### 2.3.1 结构体布局与冷热数据分离

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

在常见的 64 位 ABI 上，两者分别占 32 和 24 字节，目标平台上的实际大小可用 `sizeof` 确认。重新排列字段减少了填充，字段含义保持不变。

如果订单对象还带着几十字节的备注、字符串和历史统计，仅调整顺序不够。可以把经常一起访问的字段放在一起，把很少访问的字段移到独立结构中。

![AoS、冷热分离与 SoA：不同访问方式对应不同布局](assets/hft-02/01-data-layout.svg)

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

冷热分离让热点循环少搬运无关数据，代价是访问冷字段时增加定位操作。是否划算，取决于热字段和冷字段各自的访问频率。

#### 2.3.2 AoS、SoA 与顺序访问

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

#### 2.3.3 预缺页（Prefault）与预取（Prefetch）

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

```text
第 1 页：[0, 4096)          第 2 页：[4096, 8192)
缓冲区：[1024, 5120)       最后一个字节位于 5119

缓冲区偏移 0    -> 地址 1024：位于第 1 页，循环会写这里。
缓冲区偏移 4096 -> 地址 5120：已经越出缓冲区，不能写。
缓冲区偏移 4095 -> 地址 5119：位于第 2 页，末字节补写会覆盖这里。
```

如果只有按页步进的循环，它写完偏移 0 就退出了，因为下一个偏移 4096 已经等于缓冲区长度；但缓冲区实际跨了两个页面，第二页还没触碰。最后追加 `touch[size_ - 1] = 0`，就补齐了这一页，而且没有越界。

对于更长的区间，相邻触碰地址相差恰好一页，会落在连续页面的相同页内偏移上，不会跳过中间页面。最后一次循环写入到区间末尾的距离不足一页，因此最多漏掉末尾那一页，补写末字节即可。对于本例页对齐的 `mmap` 地址，这次补写只是再次触碰已经覆盖的页面。

这里选择写入，是因为匿名内存的首次读取可能只建立到共享零页（Linux zero page）的只读映射，后续第一次写入仍会分配私有页面。零页是内容全为零、可由多个虚拟页共享的物理页，与虚拟地址 0 无关。Linux 官方文档的 [Anonymous Memory 小节](https://docs.kernel.org/admin-guide/mm/concepts.html#anonymous-memory)描述了这一过程：读取时可以建立指向特殊全零物理页的页表项，写入时再分配物理页保存数据。

写入把这部分成本也移到初始化阶段。`volatile` 用于保留触页操作，避免编译器把它当成无用代码删除。

Linux 5.14 起，在支持的可写映射上可以使用 `madvise(addr, length, MADV_POPULATE_WRITE)`，让内核完成可写 prefault，省去用户态逐页写零。返回失败时，部分页面仍可能已完成处理；返回成功也只表示完成预缺页，页面后续的驻留还涉及锁页。[Linux madvise 文档](https://www.man7.org/linux/man-pages/man2/madvise.2.html)

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

软件预取在运行过程中提示 CPU 提前读取即将使用的数据。顺序访问常有硬件预取支持，下面展示间接访问时的软件预取：

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

<a id="execution"></a>

### 2.4 分支、循环与数值计算优化

#### 2.4.1 短路求值与条件检查顺序

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

#### 2.4.2 延迟格式化与 cold/noinline 冷路径分离

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

把字符串拼接、内存分配和异常构造推迟到检查失败之后，正常订单就不再执行这些工作。再把失败处理抽成独立函数，正常路径只留下条件检查和一个很少执行的调用。因此，把格式化放进 `if` 分支后，仍有抽离冷代码的空间。

CPU 的指令缓存（I-cache）保存机器指令。如果大段诊断、错误格式化或恢复逻辑被排在正常处理的基本块之间，就会拉大代码跨度；函数内联到多个调用点时，还可能复制这些冷代码。

抽离之后，常见路径可以更紧凑地排列，多个调用点也能共享一个冷函数，减少随内联复制的冷代码。这样有利于降低热代码的指令缓存占用和取指压力，收益取决于编译器最终布局、缓存行边界和实际执行路径；未执行的分支并非都会进入 I-cache。

热函数变小后，也更容易被内联到上层调用者。例如，行情处理函数每次都调用 `validate_good`。抽离前，内联检查函数可能把格式化、异常构造及清理逻辑一起带入调用者；抽离后，内联只展开一个检查和一个冷调用。即使上层接收循环很大，这段小检查也更容易内联进去，省掉正常路径上的调用，并结合调用点信息继续优化。

这里希望内联 `validate_good` 这样的热函数，而用 `noinline` 保留 `reject_zero_qty` 这样的独立冷函数。

| 手段 | 主要作用 | 不保证什么 |
|---|---|---|
| 将冷分支抽成独立函数 | 把少见处理从热函数的函数体中移出 | 编译器仍可能把普通函数重新内联回来 |
| `[[gnu::noinline]]` | 阻止冷函数的函数体被内联到调用点 | 不表示该函数执行频率低，也不指定代码段位置 |
| `[[gnu::cold]]` | 告诉编译器该函数很少执行，影响优化与布局；GCC 在许多目标上会把冷函数集中到专门的代码子段 | 不等于 `noinline`，也不是让 CPU 自动识别冷热代码的指令 |
| `[[unlikely]]` / `__builtin_expect` | 提供分支概率提示，帮助编译器安排控制流和代码布局 | 不负责抽取冷函数，也不保证运行时分支预测正确 |

**怎样配合使用 `always_inline` 与 `noinline`**

严格说，它们是编译器提供的函数属性，不是 C++ 标准关键字。GCC/Clang 可以写成 `[[gnu::always_inline]]`、`[[gnu::noinline]]`，也支持 `__attribute__((always_inline))`、`__attribute__((noinline))` 的写法。

`always_inline` 比普通 `inline` 更强，会要求编译器绕过常规内联收益阈值尝试展开。

| 函数特征 | 属性使用方式 | 对 I-cache 的取舍 |
|---|---|---|
| 极短、调用频繁，且内联后能简化的小函数 | 默认先让编译器决定；确认漏掉有价值的内联后，再考虑 `always_inline` | 可能减少调用和辅助指令，但仍要检查多个调用点累计的代码体积 |
| 很少执行，含大量格式化、诊断或恢复代码 | 抽成独立函数，使用 `cold` + `noinline` | 保留一份共享冷代码，避免随热函数内联到多处 |
| 执行频繁，但函数体很大 | 强制内联会增加生成多份代码副本的风险，进而增加指令缓存压力 | 根据基准测试和性能剖析结果决定 |

少见且代码较多的错误处理、详细诊断或恢复分支适合抽离。执行冷路径时，仍有函数调用成本；若代码不在指令缓存中，还会产生缓存未命中的成本。如果分支实际很频繁，或整个分支只有一两条简单指令，这些成本就可能抵消收益。分支频率由真实工作负载决定，基于运行时剖析的优化（PGO）可以帮助编译器做出选择。

#### 2.4.3 无分支计算

成交方向经过校验、只剩买入和卖出后，可以编码成 `+1`、`-1`，减少更新持仓时的选择。

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

这段代码把买卖方向的选择统一成了一次数组访问。

#### 2.4.4 循环展开和多路计算

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

#### 2.4.5 SIMD 自动向量化与 __restrict__

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

```text
AoS 内存： [bid0 ask0] [bid1 ask1] [bid2 ask2] [bid3 ask3]

操作数 A： [ask0 ask1 ask2 ask3]
操作数 B： [bid0 bid1 bid2 bid3]
向量相减： [ask0-bid0  ask1-bid1  ask2-bid2  ask3-bid3]
```

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

#### 2.4.6 浮点精度混用

浮点类型混用可能引入额外的转换开销。例如，`float` 变量乘上不带后缀的浮点字面量，会先提升到 `double`：

```cpp
float mixed_precision(float value) {
    return static_cast<float>(value * 1.23);       // 提升到 double 后计算
}

float single_precision(float value) {
    return value * 1.23f;                         // float 精度下计算
}
```

#### 2.4.7 signed/unsigned、整数转换与循环优化

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

<a id="concurrency"></a>

### 2.5 并发通信与缓存一致性优化

#### 2.5.1 单一写入者与线程局部计数

多个线程反复修改同一个对象时，即使使用原子操作，也会争用承载这个对象的缓存行。常见做法是按标的或账户划分状态，让每份状态由固定线程写入。

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

#### 2.5.2 伪共享（False Sharing）与缓存行对齐

两个线程修改不同计数器时，如果计数器恰好落在同一缓存行，仍可能争用该缓存行，产生一致性流量。这就是 false sharing。

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

对齐后的 `Counter` 大小也满足其对齐要求，因此两个成员分处不同的 64 字节区间。选择用于隔离数据的间距时，也可以参考 `std::hardware_destructive_interference_size` 这个常量。

#### 2.5.3 SPSC 环形队列与 acquire/release 内存序

SPSC（single producer, single consumer）环形队列适用于只有一个生产者和一个消费者的线程间通信，在低延迟系统之外也很常见。生产者写入槽位并公布写指针；消费者读完槽位，再公布读指针，通知生产者这个位置可以复用。

![SPSC 环形队列中的数据发布和槽位复用](assets/hft-02/spsc-memory-order.svg)

*图 3：两个方向都通过同步操作交接数据。先写事件再发布；先读完事件再归还槽位。*

下面的实现只用两个下标记录队列状态：`read_` 指向下一条待读消息，`write_` 指向下一次写入的位置。下标到达数组末尾后会回到 0，因此它们只记录位置，不记录已经绕了几圈。

如果允许写满全部 `N` 个槽位，写下标绕一圈后就会追上读下标，此时 `write_ == read_`。但队列为空时，这两个下标也相等，仅凭它们便无法区分满和空。以 `N = 4`、消费者暂时不读取为例：

| 状态 | `read_` | `write_` | 含义 |
|---|---:|---:|---|
| 初始状态 | 0 | 0 | 队列为空 |
| 写入槽位 0、1、2 后 | 0 | 3 | 已有 3 条消息，槽位 3 留空 |
| 假如继续写入槽位 3 | 0 | 0 | 实际有 4 条消息，下标却与空队列相同 |

所以这份实现会在最后一步之前拒绝写入，始终保留一个空槽，并采用两条判断规则：

- **空：** `write_ == read_`。
- **满：** 写下标的下一个位置等于读下标，即 `(write_ + 1) % N == read_`。代码要求 `N` 是 2 的幂，因此用 `& (N - 1)` 代替 `% N`。

因此，数组长度为 `N`，可用容量为 `N - 1`。空槽并不是固定的某个位置：上例中消费者读走槽位 0 后，生产者就可以写入槽位 3，随后槽位 0 成为保留的空槽。若另外记录元素数量或区分跨圈的序号，也可以使用全部 `N` 个槽位；这里选择留空一格，让满、空判断只依赖这两个循环下标。

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

数据发布和槽位复用各有一条同步关系：

1. 生产者填好 `slots_[w]`，随后 release 发布 `write_`；消费者通过 acquire 观察到发布后，才能读取新数据。
2. 消费者复制完 `slots_[r]`，随后 release 更新 `read_`；生产者通过 acquire 观察到空间可用后，才能复用旧槽位。

本线程独占写入的游标可以 relaxed 读取；跨线程交接仍依赖上述 acquire/release 关系。

#### 2.5.4 异步日志与批量输出

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

#### 2.5.5 Disruptor：事件广播和消费依赖

Disruptor 的核心是预分配的环形数组，加上生产者和每个消费者各自维护的序号。在广播模式下，同一条事件会被所有相关消费者处理；消费者读取事件后，只推进自己的进度，事件仍留在槽位里，供其他消费者访问。

下面实现一个生产者、三个消费者的菱形依赖：A、B 分别计算事件的 `left`、`right`，C 等两者完成后汇总。

图与伪代码用 `s`、`N` 和 `done[C]` 判断槽位能否复用：

| 变量 | 含义 | 例子 |
|---|---|---|
| `s` | 生产者本次准备写入的新事件序号，不是数组下标 | `s = 6` 表示准备写入序号为 6 的事件；实际下标是 `6 % N` |
| `N` | 环形数组的槽位总数 | `N = 4` 表示只有 4 个槽位，下标为 0、1、2、3 |
| `done[C]` | C 已经连续处理完成的最大事件序号，由 C 在处理完事件后更新 | 值为 5 表示事件 0～5 都已完成，共 6 条；初始值 −1 表示尚未完成任何事件 |

当数组绕回旧槽位时，新事件 `s` 将覆盖上一轮的事件 `s - N`。生产者等待 `done[C] >= s - N`，确认 C 已处理完旧事件后才覆盖它。生产者读取 C 的进度自行判断，无须单独的协调线程。

![Disruptor 的消费依赖、变量含义，以及事件 6 复用事件 2 所在槽位的过程](assets/hft-02/disruptor-diamond-explained.svg)

*图 4：A、B 并行处理，C 等两者完成。容量为 4 时，事件 6 和事件 2 使用同一个槽位；C 完成事件 2 后，生产者才能写入事件 6。*

**环形数组与进度序号**

事件序号从 0 开始递增，数组下标是 `sequence % N`。例如 `N = 4` 时，序号 0、4、8 都使用槽位 0。序号保留了轮次，因此可以区分“数组为空”和“写满一圈”，不需要像上一节那样留出一个空槽。

```text
N = 4
slots = 预分配 N 个 Event { id, left, right }
total = 0                      // 汇总结果，线程启动后只由 C 修改。

next_claim = 0                  // 生产者私有：下一条事件的序号。
published = atomic(-1)          // 已连续发布到哪个序号。
done[A] = atomic(-1)            // A 已连续处理完哪个序号。
done[B] = atomic(-1)
done[C] = atomic(-1)

dependencies[A] = []            // A、B 只需等待事件发布。
dependencies[B] = []
dependencies[C] = [A, B]        // C 还需等待 A、B 处理完同一事件。
gates = [C]                    // 生产者复用槽位时，需要检查的末端消费者。
```

`-1` 表示尚未发布或处理任何事件；`done[X] = s` 表示 X 已处理完从 0 到 s 的全部事件。以下伪代码使用足够宽的有符号序号，暂不考虑序号溢出，省略批量处理、异常和停止逻辑。`pause()` 表示忙等中的短暂停顿。

**生产者：检查容量，写入事件，再发布序号**

写入序号 `s` 会复用 `s - N` 的槽位，所有相关消费者处理完旧事件后，生产者才能写入。`gates` 保存要检查的消费者，容量判断取它们完成序号的最小值：

```text
produce(id):
    s = next_claim

    while true:
        consumed = min(done[g].load_acquire() for g in gates)
        if s - N <= consumed:
            break               // 旧事件已处理完，可以覆盖这个槽位。
        pause()

    next_claim = s + 1           // 申请到序号，但此时还没有发布。
    slots[s % N] = Event { id, 0, 0 }
    published.store_release(s)  // 事件写完整后，消费者才可以读取。
```

初始时 `consumed = -1`，序号 0 到 3 都能写入；准备写序号 4 时，要等 C 处理完序号 0。单生产者按序写入和发布，因此 `published = s` 就能表示此前所有事件都已发布。

**消费者：等待发布和前置依赖，处理后公布进度**

每个消费者都有独立的待处理序号 `s`，都从 0 开始。它能处理到哪里，由“生产者已发布到哪里”和“前置消费者已处理到哪里”共同决定：

```text
consume(name):
    s = 0                       // 当前消费者私有，不与其他消费者共用。

    while true:
        available = published.load_acquire()
        for dependency in dependencies[name]:
            available = min(available, done[dependency].load_acquire())

        if s > available:
            pause()
            continue

        event = 引用 slots[s % N]
        if name == A:
            event.left = event.id * 3
        else if name == B:
            event.right = event.id * 7
        else if name == C:
            total += event.left + event.right    // total 仅由 C 修改。

        done[name].store_release(s)              // 对这个槽位的访问已全部结束。
        s = s + 1

// 初始化完成后，分别在三个线程中运行 consume(A)、consume(B)、consume(C)。
```

A、B 没有前置消费者，看到事件发布就可以处理；C 的可读上限是 `min(published, done[A], done[B])`。这个等待条件就是消费依赖屏障：C 不能越过较慢的一方。A、B 修改不同的标量字段，C 等两者完成后才读取结果。

这里的 acquire/release 对应三次交接：生产者把事件交给 A、B，A、B 把计算结果交给 C，C 把可复用的槽位交还生产者。消费者结束对槽位的全部访问后才更新 `done`；若把引用交给异步任务，就要等该任务结束，否则生产者复用槽位时会与任务争用数据。

**多个消费者怎样共同限制生产进度**

假设容量为 4，生产者已发布序号 0 到 3，当前 `done[A] = 3`、`done[B] = 1`、`done[C] = 1`。在消费者进度暂时不变的情况下：

| 接下来的操作 | 判断条件 | 结果 |
|---|---|---|
| C 处理序号 2 | `2 <= min(3, 3, 1)` | 不成立，等待 B 处理完 2 |
| 生产者写入序号 4 | `4 - 4 <= done[C]` | 成立，复用槽位 0 |
| 生产者写入序号 5 | `5 - 4 <= done[C]` | 成立，复用槽位 1 |
| 生产者继续写序号 6 | `6 - 4 <= done[C]` | 不成立，等待 C 处理完 2 |

生产者只检查 C 就足够，因为 C 处理完序号 s 时，A、B 也已处理完 s。如果只有独立的 A、B 两个消费者，设置 `gates = [A, B]`；如果另有一个独立消费者 D，则设置 `gates = [C, D]`。慢消费者最终会阻止生产者覆盖它仍在使用的数据，形成背压。

**多个生产者：申请序号和发布事件必须分开**

上面的 `published` 只适用于单生产者按序发布。多个生产者通过原子操作申请不同序号后，写入可以乱序完成：P1 申请 10 后尚未写完，P2 已经发布 11，此时消费者仍要等待 10。

一种简化实现是增加每个槽位的发布序号，用它区分同一物理槽位的不同轮次：

```text
next_claim = atomic(0)           // 替换单生产者私有变量。
ready = N 个 atomic(-1)         // 替换单一 published 游标。

produce_multi(event):
    s = next_claim.fetch_add(1, relaxed)   // 只拿到唯一序号，尚不能直接写槽位。
    while s - N > min(done[g].load_acquire() for g in gates):
        pause()
    slots[s % N] = event
    ready[s % N].store_release(s)

// 消费者处理自己的序号 s 前，用下面的检查替换前面代码的整个等待部分：
while ready[s % N].load_acquire() != s:
    pause()
for dependency in dependencies[name]:
    while done[dependency].load_acquire() < s:
        pause()
// 接着执行原来的事件处理、done[name].store_release(s) 和 s = s + 1。
```

消费者只检查当前待处理的序号，因此会停在第一个尚未发布的事件上。申请序号的生产者若一直未发布，这个空洞就会阻塞后续按序消费。实际实现也可以保存槽位的轮次标记。

![多生产者下，后面的事件先发布，不能越过前面的空洞](assets/hft-02/disruptor-publication-gap.svg)

*图 5：生产者申请序号后还要写入、发布事件；消费者按序处理，在前面的发布空洞处等待。*

这套结构适合事件广播和有先后依赖的处理流程。如果 A、B 的计算只有几条指令，线程交接和缓存一致性开销可能超过并行收益。工程实现还常加入批量发布、批量更新消费进度和进度缓存，减少原子操作与跨核通信。


### 2.6 Stop-bit 变长整数：用短字段减少数据搬运

行情协议也会使用变长整数，而非固定的 4 或 8 字节。例如，上交所 Level-2 产品使用 FIX/FAST 接口，FAST 的整数使用 stop-bit 编码。当大量字段值较小时，用更少字节表示它们能减少传输量与缓冲区占用，代价是解码时要识别字段终点、重建整数。整体延迟是否下降，取决于数值分布和目标 CPU 的测量结果。[上证所 Level-2 产品说明](https://www.sseinfo.com/services/assortment/level2/)

#### 2.6.1 先固定编码规则，避免混用不同 varint

本例实现 **FAST 风格、非空、无符号 `uint64_t` 的最短编码**：每个字节的低 7 位携带数据，最高位为 `1` 表示最后一个字节，为 `0` 表示后面还有字节；高位数据组先发送。这里不是 Protobuf 的低位组在前、最高位为 `1` 表示继续的 varint。

| 数值 | 线上字节，十六进制 | 说明 |
|---|---|---|
| 0 | `80` | 一个结束字节，载荷为零 |
| 127 | `FF` | 一个字节能表示的最大无符号值 |
| 128 | `01 80` | `(1 << 7) \| 0` |
| 300 | `02 AC` | `(2 << 7) \| 44` |
| `UINT64_MAX` | `01 7F 7F 7F 7F 7F 7F 7F 7F FF` | 最多需要 10 字节 |

完整范围的 64 位整数用 varint 编码时，最长会超过定长的 8 字节。若相邻价格差值很小，可以先用差分编码把绝对价格变成小整数，再用 stop-bit 编码减少字节数；两种优化处理的是不同问题。

#### 2.6.2 编解码实现：短值快路径，长值有界展开

下面是 stop-bit 变长编码的 C++20 标量实现，未使用 SIMD。它单独处理调用频繁的一、两字节整数；对于较长整数，输入至少还剩 10 字节时才走展开路径，省去逐字节检查输入边界的操作。

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

`decode` 一次只解一个整数，结果分为 `done`、`need_more` 和 `invalid`。`consumed` 始终表示**本次输入**已消费的字节数；`need_more` 时保留 `UIntState`，下一次传入新字节，不要把旧字节重新喂进去。`done` 后 `value` 是完整结果，`count` 归零，下一次调用即可解析下一个字段。

### 2.7 用状态机从字节流解析变长结构体

TCP 不保留应用消息边界。一次接收既可以只有某个整数的前半部分，也可以包含多条行情和下一条行情的一部分。stop bit 只标记字段结束，字段的位置和整条行情的完成状态还要由解析器维护。

用一个基础行情格式说明状态机：每条消息按顺序包含 `instrument_id、sequence、price_ticks、quantity` 四个非空无符号整数。

```text
消息 A：id=1，seq=128，price=300，qty=10
线上字节：[81] [01 80] [02 AC] [8A]

接收 1：81 01         → id 已完成，seq 只收到第一组
接收 2：80 02         → seq 已完成，price 只收到第一组
接收 3：AC 8A 82 ...  → 完成 price、qty，交付 A；82 开始下一条消息
```

这里有两层状态：

| 状态 | 保存什么 | 什么时候推进 |
|---|---|---|
| 字段内部状态 `integer_` | 已累计的整数值和字节数 | 收到 stop bit，才得到一个完整字段 |
| 消息状态 `field_` | 接下来正在解析四个字段中的哪一个 | 一个字段完成后推进；第四个字段完成后交付消息并回到第一个字段 |

每条连接持有一个 `QuoteStream`。输入字节直接参与移位和合并，消费后即可丢弃；跨接收边界时只保留整数进度和未完成的结构体，省去了拼接字符串、动态分配和搬移残留字节。


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

<a id="validation"></a>

## 3. 怎样验证一次优化

### 3.1 让基准测到真正想比较的工作

下面用 Google Benchmark 比较普通求和与四路累加。输入在循环外生成，结果通过 `DoNotOptimize` 交给基准工具，避免被当成无用代码删除。编译器仍可对求和进行向量化、展开等优化，基准比较的是这些优化后的执行结果。

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

```bash
g++ -std=c++20 -O3 -DNDEBUG -pthread bench.cpp -lbenchmark -o /tmp/ll-bench
/tmp/ll-bench --benchmark_repetitions=5
```


### 3.2 用性能计数器解释变化

修改后变快的原因可以借助性能计数器分析。在支持相应事件的 Linux 环境中，先查看指令数、分支和缓存未命中的变化：

```bash
perf stat -r 5 -e cycles,instructions,branches,branch-misses,cache-misses \
  /tmp/ll-bench --benchmark_filter='BM_Sum<false>/4096$' --benchmark_min_time=0.5s
```

### 3.3 读一次时间戳也有成本

`RDTSC` 可以直接在用户态读取时间戳计数器，正常执行时无须陷入内核，但指令本身仍有成本。uops.info 在重复执行裸 `RDTSC` 的实验中，测得以下平均成本：

| 测试 CPU | 架构 | 平均核心周期数/条 | 假设核心频率为 3 GHz 时的换算值 |
|---|---|---:|---:|
| Intel Core i5-6500 | Skylake | 25 | 约 8.3 ns |
| Intel Core i5-12600K 的 P 核 | Alder Lake-P | 26 | 约 8.7 ns |
| AMD Ryzen 5 7600X | Zen 4 | 35 | 约 11.7 ns |

表中的核心周期数描述执行成本，`RDTSC` 返回的则是 TSC ticks。现代支持 invariant TSC 的处理器按固定速率推进 TSC，换算差值使用的是 TSC 频率。

按表中假设的核心频率换算，读钟成本约为十纳秒量级。测量几条指令时，前后各读一次时间戳的成本就可能与目标代码相当；加入约束指令排序和编译器重排的措施后，还要计入这些措施的成本。

### 4. 总结

亚微秒级交易系统的优化已经深入到指令和数据布局，这与传统高吞吐系统的取舍不同。受量化行业特点影响，公开博客和论文对工程实践的介绍有限，这些方法也是笔者根据可检索资料整理的。头部 HFT 团队里，也许还有不少我们未见过的“编程魔法”，值得继续学习。
