# HFT Engineering从入门到入土（3）—— C++线程通信与性能测量

一条行情的处理往往不止一个线程参与：业务线程把记录交给统计线程，策略把订单交给网关，主核把一部分计算交给辅助核。数据写在哪里、何时发布、何时允许覆盖，决定了这些线程如何协作，也影响缓存行在核心之间的传递。

本文先从 SPSC 队列与 Disruptor 讲线程交接，再用分段打点与 XRay 记录消息路径的耗时，最后通过基准测试和性能计数器分析优化效果。上一篇《C++内存与计算优化》中的预取、批处理和缓存行淘汰，也会在性能分析部分用作案例。

## 目录

- [HFT Engineering从入门到入土（3）—— C++线程通信与性能测量](#hft-engineering从入门到入土3-c线程通信与性能测量)
  - [目录](#目录)
  - [1. 并发通信与缓存一致性优化](#1-并发通信与缓存一致性优化)
    - [1.1 单一写入者与线程局部计数](#11-单一写入者与线程局部计数)
    - [1.2 伪共享（False Sharing）与缓存行对齐](#12-伪共享false-sharing与缓存行对齐)
    - [1.3 SPSC 环形队列与 acquire/release 内存序](#13-spsc-环形队列与-acquirerelease-内存序)
    - [1.4 异步日志与批量输出](#14-异步日志与批量输出)
    - [1.5 Disruptor：事件广播和消费依赖](#15-disruptor事件广播和消费依赖)
    - [1.6 共享请求发布以后，少做一次读回](#16-共享请求发布以后少做一次读回)
    - [1.7 少访问共享游标，让接口允许原地填写](#17-少访问共享游标让接口允许原地填写)
    - [1.8 无锁算法还需要说明单次操作的进度](#18-无锁算法还需要说明单次操作的进度)
  - [2. 沿消息路径记录延迟](#2-沿消息路径记录延迟)
    - [2.1 先确定测量的起点和终点](#21-先确定测量的起点和终点)
    - [2.2 沿同一条消息拆分进程内耗时](#22-沿同一条消息拆分进程内耗时)
    - [2.3 选时钟时，先考虑可比较性，再考虑成本](#23-选时钟时先考虑可比较性再考虑成本)
    - [2.4 时钟读取成本](#24-时钟读取成本)
    - [2.5 既看分布，也保留同一条消息的分段记录](#25-既看分布也保留同一条消息的分段记录)
    - [2.6 控制测量本身的干扰](#26-控制测量本身的干扰)
    - [2.7 用逐事件打点与 XRay 记录短路径](#27-用逐事件打点与-xray-记录短路径)
    - [2.8 用 SPSC 队列把时间戳交给统计线程](#28-用-spsc-队列把时间戳交给统计线程)
    - [2.9 延迟数据交接大概要付出多少延迟](#29-延迟数据交接大概要付出多少延迟)
  - [3. 怎样验证一次优化](#3-怎样验证一次优化)
    - [3.1 让基准测到真正想比较的工作](#31-让基准测到真正想比较的工作)
    - [3.2 用性能计数器解释变化](#32-用性能计数器解释变化)
  - [4. 总结](#4-总结)

<a id="concurrency"></a>

<a id="hft03-1"></a>
## 1. 并发通信与缓存一致性优化

<a id="hft03-1-1"></a>
### 1.1 单一写入者与线程局部计数

多个线程反复修改同一个对象时，即使使用原子操作，也会争用存放这个对象的缓存行。常见做法是按标的或账户划分状态，让每份状态由固定线程写入。

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

<a id="hft03-1-2"></a>
### 1.2 伪共享（False Sharing）与缓存行对齐

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

<a id="hft03-1-3"></a>
### 1.3 SPSC 环形队列与 acquire/release 内存序

SPSC（single producer, single consumer）环形队列适用于只有一个生产者和一个消费者的线程间通信，在低延迟系统之外也很常见。生产者写入槽位并公布写指针；消费者读完槽位，再公布读指针，通知生产者这个位置可以复用。

![SPSC 环形队列中的数据发布和槽位复用](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/spsc-memory-order.png)

*图 1：两个方向都通过同步操作交接数据。先写事件再发布；先读完事件再归还槽位。*

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

<a id="hft03-1-4"></a>
### 1.4 异步日志与批量输出

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

<a id="hft03-1-5"></a>
### 1.5 Disruptor：事件广播和消费依赖

Disruptor 的核心是预分配的环形数组，加上生产者和每个消费者各自维护的序号。在广播模式下，同一条事件会被所有相关消费者处理；消费者读取事件后，只推进自己的进度，事件仍留在槽位里，供其他消费者访问。

下面实现一个生产者、三个消费者的菱形依赖：A、B 分别计算事件的 `left`、`right`，C 等两者完成后汇总。

图与伪代码用 `s`、`N` 和 `done[C]` 判断槽位能否复用：

| 变量 | 含义 | 例子 |
|---|---|---|
| `s` | 生产者本次准备写入的新事件序号，不是数组下标 | `s = 6` 表示准备写入序号为 6 的事件；实际下标是 `6 % N` |
| `N` | 环形数组的槽位总数 | `N = 4` 表示只有 4 个槽位，下标为 0、1、2、3 |
| `done[C]` | C 已经连续处理完成的最大事件序号，由 C 在处理完事件后更新 | 值为 5 表示事件 0～5 都已完成，共 6 条；初始值 −1 表示尚未完成任何事件 |

当数组绕回旧槽位时，新事件 `s` 将覆盖上一轮的事件 `s - N`。生产者等待 `done[C] >= s - N`，确认 C 已处理完旧事件后才覆盖它。生产者读取 C 的进度自行判断，无须单独的协调线程。

![Disruptor 的消费依赖、变量含义，以及事件 6 复用事件 2 所在槽位的过程](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/disruptor-diamond-explained.png)

*图 2：A、B 并行处理，C 等两者完成。容量为 4 时，事件 6 和事件 2 使用同一个槽位；C 完成事件 2 后，生产者才能写入事件 6。*

**环形数组与进度序号**

事件序号从 0 开始递增，数组下标是 `sequence % N`。例如 `N = 4` 时，序号 0、4、8 都使用槽位 0。序号保留了轮次，因此可以区分“数组为空”和“写满一圈”，不需要像前面的 SPSC 实现那样留出一个空槽。

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

![多生产者下，后面的事件先发布，不能越过前面的空洞](assets/hft-02/disruptor-publication-gap.png)

*图 3：生产者申请序号后还要写入、发布事件；消费者按序处理，在前面的发布空洞处等待。*

这套结构适合事件广播和有先后依赖的处理流程。如果 A、B 的计算只有几条指令，线程交接和缓存一致性开销可能超过并行收益。工程实现还常加入批量发布、批量更新消费进度和进度缓存，减少原子操作与跨核通信。


<a id="hft03-1-6"></a>
### 1.6 共享请求发布以后，少做一次读回

发布共享请求后，主核可以继续使用自己保存的局部值，省去一次共享内存读取。

主核先计算价格变化 `local_move`，把它写入共享请求并通知辅助核，随后继续计算自己的部分。两种写法使用相同的数值，访存路径却可能不同：

[![两种写法都先写共享请求并release发布序号；原写法读回shared_request.move，改写继续使用local_move。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-shared-readback.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-shared-readback.png)

辅助核轮询、读取共享请求时，这条缓存行会参与跨核一致性交互。主核继续使用 `local_move`，就能省掉从共享区域读回它的访问。编译器也可能自动保留这个局部值，汇编中的 load 能看出两种写法是否产生了差别。

两个核心访问同一份数据时，交接脏数据、获取写权限都需要一致性通信。缓存行对齐可以隔离无关对象，而局部值复用进一步减少了对共享行本身的访问。

对于辅助核刚写好、主核即将读取的结果，还可以通过 `CLWB` 发起脏数据写回。读取若转由 L3 响应，就能减少跨核 snoop 转发，相应的成本是辅助核增加一次写回。`CLWB` 的行为见第 2 篇《C++内存与计算优化》3.4 节。

若先 release 发布结果标志，再执行 `CLWB` 和 `MFENCE`，主核看到标志时，后面的写回可能仍在进行。原子发布负责交接，`CLWB` 调整数据的写回时机。

整个事件要等较慢的分支完成，再合并结果。主核已经提前完成时，应优先缩短辅助核的路径；并行关键路径模型见本系列第 4 篇《高频交易系统的部署》1.3 节。

<a id="hft03-1-7"></a>
### 1.7 少访问共享游标，让接口允许原地填写

一个写者不断发布游标、多个读者不断刷新它，仍会产生一致性流量。减少共享游标的读写次数，可以降低这部分通信成本。

| 做法 | 减少的工作 | 对协议和等待的影响 |
|---|---|---|
| 消费者缓存已观察到的可读上界 | 不必每读一条消息都重新加载发布游标 | 只消费已确认的范围，用完再 acquire 刷新；槽位复用规则不变 |
| 生产者按块预留空间 | 在有共享预留上界的协议中，减少该上界的发布次数 | 预留不等于内容已写完，不能提前让消费者读取 |
| 生产者批量发布完成进度 | 减少完成游标的共享写入 | 先写好的消息可能多等一段时间才能被看见 |
| 消费者批量归还进度 | 减少消费游标的共享写入 | 生产者较晚获知空间可复用，可能更早遇到满队列 |

按块预留后，仍可以每写完一条消息就发布一次。它适用于另设共享预留上界的协议；前面的简单 SPSC 只有读、写两个游标，主要可以利用进度缓存和批量归还。Disruptor 缓存的可读范围还受前置消费者进度限制。

允许写者覆盖旧数据的广播队列中，预留越大，旧数据的保留窗口越短。这类协议要在读取期间保护存储，并处理读者落后后的恢复；单靠读后校验无法解决非原子并发读写。前面的 Disruptor 则用背压等待消费者，再复用槽位。[C++ 数据竞争规则](https://eel.is/c++draft/intro.races)

接口本身也决定了是否需要复制。调用方若必须先创建完整消息再 `push`，就常会经历“写临时区 → 复制入队”。另一种接口可以让序列化器直接填写预留区域：

[![预留足够区域，直接编码，成功后发布，消费者读完后才允许复用。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-inplace-publish.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-inplace-publish.png)

给 `SpscRing` 增加这类接口时，可以把写入分为“预留、填写、发布”：容量不足返回失败，编码失败取消预留，发布后调用方停止访问该区域。这样序列化器直接写入队列，就省掉了临时缓冲区和一次复制。

<a id="hft03-1-8"></a>
### 1.8 无锁算法还需要说明单次操作的进度

Lock-free 保证系统整体持续取得进展，但某个线程可能在 CAS 竞争中反复失败。Wait-free 进一步要求每个操作都在有界的自身算法步骤内完成。

Wait-free 限制的是算法步骤数；调度暂停和硬件争用仍会影响实际耗时。

前面的 `try_push()` 在满队列时直接返回失败；调用方若持续重试，就要等消费者释放空间。Disruptor 同样用等待慢消费者的方式保护槽位，形成背压。

共享引用计数等状态还可以通过算法设计消除无界 CAS 重试，关键是同时安排好状态表示、竞争线程的协作方式，以及最后一次释放由谁负责。

<a id="hft03-2"></a>
## 2. 沿消息路径记录延迟

看到哈希表、虚函数或动态内存分配，很容易想到几种优化办法。选择从哪里动手，先看一条行情从进入程序到产生订单，时间到底花在了哪里。

较长的处理过程可以直接用 `steady_clock` 计时。耗时缩短到亚微秒后，时钟读取和样本记录本身也可能占去明显的一部分时间。

<a id="hft03-2-1"></a>
### 2.1 先确定测量的起点和终点

一次行情驱动的交易，大致经过下面这条路径：

[![行情依次经过网卡、应用处理、发送与交易所。H0/H1是网卡边界，C0/C4是应用处理边界。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-measurement-path.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-measurement-path.png)

`C4 - C0` 是应用取得行情到订单准备完毕的进程内耗时；`H1 - H0` 则覆盖本机从收行情到发订单的路径，常被称为 tick-to-trade latency。后者包含网卡与主机之间的数据传递，以及收发路径中的等待。相减的前提是两端时钟可比较；例如网卡 PHC（PTP 硬件时钟）或设备计数器，与 CPU TSC 可能属于不同的时钟域，计算前要先确认两者的关系。

`H0` 和 `H1` 可以来自网卡硬件时间戳，即网卡在收发报文经过特定位置时记录的时刻。在支持硬件时间戳的 Linux socket 路径中，可以用 `SO_TIMESTAMPING` 请求时间戳：接收时间戳通过 `recvmsg()` 的 `SCM_TIMESTAMPING` 辅助控制消息随报文返回，发送时间戳则从 socket 错误队列异步读取。接口和关联方法见 [Linux 时间戳文档](https://docs.kernel.org/networking/timestamping.html)。

发送硬件时间戳记录报文经过本机网卡发送位置的时刻。`send()` 返回、网卡发出、交易所接受订单、订单成交，是不同的完成点。这里测量 `C0` 到 `C4` 之间的计算、访存和线程交接。

<a id="hft03-2-2"></a>
### 2.2 沿同一条消息拆分进程内耗时

先看所有业务阶段都在同一线程中执行的情况。与其给每个阶段分别读两次时钟，可以让上一阶段的结束时间同时作为下一阶段的开始时间：

[![C0到C1解码，C1到C2更新订单簿，C2到C3策略计算，C3到C4风控与编码，总区间C4减C0。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-stage-timestamps.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-stage-timestamps.png)

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

<a id="hft03-2-3"></a>
### 2.3 选时钟时，先考虑可比较性，再考虑成本

`steady_clock` 保证时间点不倒退，适合计算时间间隔，底层计数源与换算方式由标准库和操作系统决定。直接使用原始 TSC 打点，则可以先保存计数，把纳秒换算移到统计线程。[C++ steady_clock 定义](https://eel.is/c++draft/time.clock.steady)

当计算和访存耗时短到时钟读取成本不可忽略时，在 x86 上可以直接读取 TSC（Time Stamp Counter，时间戳计数器）。`RDTSC`、`RDTSCP` 可以读取它，差值按 TSC 频率换算：

```text
elapsed_ns = (end_tsc - begin_tsc) × 1,000,000,000 / tsc_hz
```

`tsc_hz` 使用 TSC 的固定计数频率，而 CPU 睿频描述的是核心当前的运行频率。

裸 `RDTSC` 不能阻止 CPU 乱序执行，`RDTSCP` 也不是完整的双向序列化屏障。测量短区间时，按目标处理器和编译器的语义插入屏障，才能约束计时边界。

以 Intel x86-64、GCC/Clang 为例，可以把时钟读取操作封装成下面的函数：

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
| 前面的 `lfence` | 等待此前指令在本核完成执行，避免提前读取时钟 |
| 后面的 `lfence` | 阻止后续指令在读取时钟前执行 |
| `asm volatile` | 避免汇编因输出未使用而被删除；单独使用不保证周围代码不重排 |
| `"memory"` clobber | 告诉编译器该汇编可能读写内存，约束周围内存访问的移动 |

`"memory"` 在编译器层面约束内存操作的重排，不会自行生成 CPU 屏障指令；`LFENCE` 则是实际执行的机器指令。把三条指令写在同一个汇编块中，也避免了编译器在它们之间插入其他指令。

纯寄存器计算仍可能移出计时区间，`"memory"` 只约束内存操作。下面的乘法就可能被提前计算、折叠或删除，因此这段代码不能直接用作可靠的乘法微基准：

```cpp
auto begin = ordered_rdtsc();
auto result = x * 123;  // 仍可能被提前计算、常量折叠，或因结果未使用而删除，编译器会有各种优化的手段。
auto end = ordered_rdtsc();
```

检查编译后的汇编，可以确认时钟读取指令与被测计算的先后顺序。

如果两个时间点来自不同 CPU 核，尤其是不同 CPU 插槽，先验证 TSC 是否一致。实际部署中，可以通过 CPU 亲和性把需要比较时间戳的线程安排在同一插槽上，减少跨插槽计时带来的误差，策略、交易和风控进程之间通过共享内存通信。

网卡 PHC 和 CPU TSC 的起点、计数速率不同。计算 `应用时间 - 网卡时间` 时，先通过时钟映射把两者换算到同一基准。跨机器测量还需要时钟同步，PTP 提供了相应机制。相关基础接口见 [Linux PHC](https://docs.kernel.org/driver-api/ptp.html)。

<a id="hft03-2-4"></a>
### 2.4 时钟读取成本

`RDTSC` 可以直接在用户态读取时间戳计数器，正常执行时无须陷入内核，但指令本身仍有成本。uops.info 在重复执行裸 `RDTSC` 的实验中，测得以下平均成本：

| 测试 CPU | 架构 | 平均核心周期数/条 | 假设核心频率为 3 GHz 时的换算值 |
|---|---|---:|---:|
| Intel Core i5-6500 | Skylake | 25 | 约 8.3 ns |
| Intel Core i5-12600K 的 P 核 | Alder Lake-P | 26 | 约 8.7 ns |
| AMD Ryzen 5 7600X | Zen 4 | 35 | 约 11.7 ns |

表中的核心周期数描述执行成本，`RDTSC` 返回的则是 TSC ticks。现代支持 invariant TSC 的处理器按固定速率推进 TSC，换算差值使用的是 TSC 频率。

按表中假设的核心频率换算，时钟读取成本约为十纳秒量级。测量几条指令时，前后各读一次时间戳的成本就可能与目标代码相当；加入约束指令排序和编译器重排的措施后，还要计入这些措施的成本。

先测一个两次时间戳读取之间没有业务计算的空区间，可以看到时钟读取和屏障本身的开销。屏障还会改变指令之间的重叠，因此这部分影响很难靠减去一个固定数值消除。TSC 的顺序约束、频率换算与跨核读数见本系列第 5 篇《深入理解 TSC、PHC 和时钟同步》。

<a id="hft03-2-5"></a>
### 2.5 既看分布，也保留同一条消息的分段记录

吞吐量代表一秒处理多少消息，延迟代表一条消息要等多久。批量处理可以提高吞吐量，但等待消息凑成一批，会让先到的消息多等一段时间；增加工作线程可以分摊计算，跨线程交接也会引入队列与缓存一致性开销。观察吞吐量、p50、p99、p99.9 和测试期间的最慢样本，才能看到这些改动对吞吐量和延迟的影响。平均值会掩盖少量慢请求。

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

对于前面的 `StageTrace`，可以为四个阶段分别建立记录器，再单独建立一个总耗时记录器。用 `record(trace.time[0], trace.time[4])` 记录总耗时，用相邻时间点记录各阶段。业务线程可以先保存整条 `StageTrace`，等测量停止后再统计，或通过队列交给统计线程处理，减少业务处理期间对多个统计容器的更新。

各阶段的 p99 不能相加得到总耗时的 p99，因为各阶段的慢样本未必来自同一条消息。先对每条消息计算总耗时，再对这些总耗时求分位数。

上面的记录器保存原始样本。固定大小的直方图更节省空间，统计粒度由桶宽决定；超过范围的样本要单独计数。把 3 μs 和 30 μs 都并入最后一桶，就会丢掉长尾的具体耗时。

<a id="hft03-2-6"></a>
### 2.6 控制测量本身的干扰

读取时钟、保存时间戳和传递统计数据都会消耗 CPU、缓存和内存带宽。处理一条消息只需几百纳秒时，五次时钟读取就可能占去相当一部分时间。

热路径只保存必要的整数、时间点和标识，格式化、排序、分位数计算和文件输出留给后续处理。每个线程独占自己的记录缓冲区，后台通过所有权移交或队列协议读取。

逐事件时间戳与采样的配合方式见 [2.7 节](#hft03-2-7)。

<a id="hft03-2-7"></a>
### 2.7 用逐事件打点与 XRay 记录短路径

忙轮询线程可以占满一个核心，却只在行情到来时运行短暂的策略判断。采样大多落在轮询上，可能准确反映 CPU 时间的分布，却没有足够样本回答“每次发单判断的 p99 是多少”。这时可以在关键处理步骤的开始和结束位置逐次记录时间戳。

一种做法是利用 RAII 对象的构造和析构，记录进入和退出代码块的时间，把区间标识、事件标识和时间戳写入预分配的线程本地缓冲区。后台线程负责汇总、格式化和导出，热路径只追加少量固定字段。

经常切换测点时，可以用 LLVM XRay：**编译时预留插桩位置，运行时按需开启或关闭。** 使用 `-fxray-instrument` 构建后，符合条件的函数会留下插桩区域及其映射，运行库修改这些位置，即可启用或关闭记录函数的调用。[LLVM XRay](https://llvm.org/docs/XRay.html)
XRay 默认选择指令数达到阈值（通常为 200）或包含循环的函数，短函数可能需要调整 `-fxray-instruction-threshold` 或显式使用 `[[clang::xray_always_instrument]]`。记录函数应使用 `[[clang::xray_never_instrument]]` 等方式避免自身再次被插桩，并保持足够轻量。[XRay 函数选择与标记](https://llvm.org/docs/XRay.html#instrumenting-your-c-c-objective-c-application)

排查时先用总体统计和采样找到热点，再为短暂的关键区间开启 XRay，逐次记录耗时。这样既能切换跟踪范围，也能把记录量控制在关注的函数内。

<a id="hft03-2-8"></a>
### 2.8 用 SPSC 队列把时间戳交给统计线程

同一进程里的线程共享地址空间，可以直接上报时间戳，无须另建一块 `mmap` 共享内存。交接时的顺序是关键：worker 写完后统计线程才能读，统计线程读完后 worker 才能覆盖。

这里直接使用 [1.3 节](#hft03-1-3)的 SPSC 环形队列。SPSC 表示单生产者、单消费者：一个 worker 负责写入，一个统计线程负责读取。队列在启动时创建，两端持有同一个队列对象的引用；等两端线程都结束后，再销毁队列。

[![业务线程写入预分配队列并发布完整StageTrace后继续业务；统计线程通过try_pop取得局部副本，计算差值、更新直方图并定期上报。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-stage-trace-queue.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-stage-trace-queue.png)

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

<a id="hft03-2-9"></a>
### 2.9 延迟数据交接大概要付出多少延迟

| 指标 | 起点与终点 | 用途 |
|---|---|---|
| worker 入队耗时 | 调用 `try_push` 到返回 | 估算业务线程增加的直接成本 |
| 记录交接延迟 | 入队前到统计线程成功取出 | 检查发布、跨核传递、排队和轮询等待 |
| 指标上报延迟 | 记录产生到聚合结果上报完成 | 判断监控数据是否及时 |

worker 入队后即可继续处理业务，统计线程异步取出记录并上报。`try_push()` 返回只表示本次入队调用结束，统计线程可以更早或更晚读到记录。交接延迟和上报延迟与 worker 的处理耗时存在重叠，不能直接相加。

以常见的 64 位实现为例，如果一个 `time_point` 占 8 字节，`StageTrace` 就是 8 字节 ID 加 5 个时间点，共 48 字节，实际大小用 `sizeof(StageTrace)` 确认。成功入队主要做一次判满、复制这 48 字节和发布游标。

在 AMD Ryzen 9 3900X 的同一 chiplet、不同核心上，[Rigtorp SPSCQueue 基准](https://github.com/rigtorp/SPSCQueue#benchmarks)用两个队列传递 `int`，测得往返延迟为 **133 ns**。这包含消息的双向传递，单次 `try_push()` 只是其中一步。

<a id="validation"></a>

<a id="hft03-3"></a>
## 3. 怎样验证一次优化

批处理、预取、缓存行淘汰和跨核交接，都可能用更多指令换来更少等待。延迟分布告诉我们快了多少，硬件计数器帮助解释为什么变快。

<a id="hft03-3-1"></a>
### 3.1 让基准测到真正想比较的工作

先确定一次事件从哪里开始计时、到哪里结束。业务函数完成得更早、消费者可以更快地处理下一条消息、行情从到达到产生结果更快，是三个相关但不同的结果：

| 测量对象 | 起止位置 | 可以回答的问题 |
|---|---|---|
| 业务处理区间 | 开始处理 → 目标结果完成 | 这段计算、必要的交接和合并用了多久？ |
| 一次完整处理循环 | 取到事件 → 完成处理和收尾，准备接下一事件 | 统计、清理、屏障等后续工作加上去后，每次处理实际承担多少成本？ |
| 到达至完成 | 事件到达所选入口 → 对应结果完成 | 事件进入系统后，连同排队在内等了多久？ |

空轮询时间可以单独统计。应用队列入口到计算完成，适合观察软件处理；网卡接收到订单发出，则覆盖本机的完整收发路径。

下面把开始与结束时间戳放在业务处理两端：

[![事件到达与排队在业务计时之前；t_start到t_end覆盖业务处理与结果合并；CLFLUSHOPT、fence和统计在结束打点后，之后再处理下一事件。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-measurement-cleanup.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-measurement-cleanup.png)

`t_end` 之后的 flush、屏障和统计更新不会计入当前业务耗时，却会占用下一次处理前的时间。于是业务区间缩短，整轮处理仍可能变长。分别记录业务区间和完整循环，就能看到减少的等待是否抵消了新增的收尾工作。

比较两个版本时，用同一份输入、相同的编译选项和核心配置，交错运行新旧版本。这样能减少温度、频率和后台负载随时间变化带来的偏差。

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

这个基准反复扫描同一数组，缓存会随着重复访问预热。`ClobberMemory()` 约束的是编译器对内存的处理，不会清空 CPU 缓存。

测跨核读取时，每轮先由指定核心写入目标行，通过握手通知另一核心，再由后者计时一次读取。准备工作放在计时区间外，避免大量后续的本核命中淹没第一次访问的成本。

整条处理链可以按真实行情节奏回放，观察突发时的排队、吞吐和丢弃。负载发生器按到达时间发送，才能保留过载时的等待；总等上一事件结束再发送，会把这段排队隐藏掉。

<a id="hft03-3-2"></a>
### 3.2 用性能计数器解释变化

时间戳告诉我们一段代码用了多久，性能计数器则记录这段时间里 CPU 做了什么：经过多少核心周期、完成多少条指令、发生多少次分支预测失败或缓存未命中。

这些硬件计数来自 **PMU（Performance Monitoring Unit，性能监控单元）**。它是处理器内部的性能监控硬件，包含一组性能计数器。配置计数器要观察的事件后，程序运行时，硬件就会累加相应的事件次数。例如，选择分支预测失败事件，就能统计执行过程中预测错误的次数。[Intel 对 PMU 与性能计数器的介绍](https://www.intel.com/content/www/us/en/developer/articles/technical/hwpgo.html)

Linux 的 `perf` 是使用这些计数器的工具：它通过内核的 `perf_event_open` 接口配置采集事件和范围，再读取结果。`perf stat` 汇总计数，`perf record` 采集样本，帮助定位具体函数和指令。下面先统计核心周期、退休指令、分支指令、分支预测失败与缓存未命中：[Linux perf_event_open 接口](https://man7.org/linux/man-pages/man2/perf_event_open.2.html)

```bash
perf stat -r 5 -e cycles,instructions,branches,branch-misses,cache-misses \
  /tmp/ll-bench --benchmark_filter='BM_Sum<false>/4096$' --benchmark_min_time=0.5s
```

`perf stat` 统计整个程序运行，包括初始化和退出；Google Benchmark 统计基准循环。只分析事件处理时，可以在初始化结束后开启计数，在处理结束时关闭。

**先判断瓶颈类别，再定位具体操作。** 在支持相应指标的平台上，Top-Down 方法把流水线槽位的使用情况归入四类：

| 类别 | 含义 | 分析方向 |
|---|---|---|
| Retiring | 对应最终退休的微操作 | 查找多余复制、重复计算和可减少的指令 |
| Bad Speculation | 错误推测及恢复造成的浪费 | 检查分支误预测、machine clears 等 |
| Frontend Bound | 前端未能及时提供足够的微操作 | 检查热代码体积、取指、指令缓存、解码等限制 |
| Backend Bound | 后端资源或依赖限制了推进 | 继续区分执行端口、数据依赖、缓存与内存等待 |

Top-Down 百分比描述流水线槽位的利用情况，具体事件和公式按 CPU 微架构选取。[Intel Top-Down 方法](https://www.intel.com/content/www/us/en/docs/vtune-profiler/cookbook/2025-4/top-down-microarchitecture-analysis-method.html)

可以按下面的步骤分析性能，逐步定位到需要修改的代码：

[![先用perf stat比较总体计数及可用Top-Down指标，再采样找持续热点，用annotate查看源码和汇编，修改后同时比较延迟与计数。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-perf-investigation.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-02/text-perf-investigation.png)

分析时保留部署版本的编译优化，并加入调试符号，用 `perf annotate` 对照源码与汇编，查看实际生成的 load、prefetch 或 flush。

对前篇的内存、计算优化和本篇的跨核通信，可以按改动的机制选择对应指标：

| 改动 | 主要观察指标 | 同时关注的开销 |
|---|---|---|
| 批处理提高内存级并行（MLP） | 未完成请求的重叠程度、停顿与核心周期 | 批次增大后的 fill-buffer-full 停顿、临时数据读写 |
| prefetch 让数据更早到达 | 需求 load 的等待、缓存命中层级、不同预取距离下的延迟 | 新增请求的带宽和填充缓冲区占用 |
| flush 减少一次性数据污染 | 热表命中率、缓存压力变化前后的延迟 | flush、写回和屏障占用的时间 |
| 局部副本或写回提示改善跨核交接 | snoop/转发、缓存响应层级、结果可用延迟 | 写回操作和跨核通信成本 |

例如，批处理后 miss 数相近、指令数增加、核心周期下降，就可以分析取数与计算是否有了更多重叠。

**分析计数结果时，要同时看事件的含义、统计的起止位置，以及平均值如何计算。** `cache-misses` 这样的通用事件不足以区分每一级缓存、需求读取与预取；实际可用事件及其含义要查目标 CPU 文档。退休 load 数、缓存行填充数和访问字节数也不是同一个单位，同一条缓存行可以被重复读取。

几百纳秒的路径适合分开计时与计数：计时轮记录延迟分布，计数轮观察硬件行为。逐事件开关计数器可能触发系统调用，把它移到独立运行中，可以减少对计时轮的干扰。

每处理一个事件的平均计数，按“计数总量 / 同一统计区间内实际完成的事件数”计算，并单独记录丢弃数量。把每轮的延迟和计数结果一起保存，便于回查当时的运行情况。

<a id="hft03-4"></a>
## 4. 总结

线程通信的成本来自共享状态访问、缓存行传递和等待。单写者、游标缓存、局部值复用与原地填写，都在减少这些工作；SPSC 和 Disruptor 则把数据发布、消费与槽位复用连成明确的顺序。

沿同一条消息保存时间戳，可以找到等待发生在哪一段；基准测试和硬件计数器进一步说明代码变化怎样影响执行。第 4 篇《高频交易系统的部署》继续讨论整机的核心分配、缓存竞争和后台负载，第 5 篇《深入理解 TSC、PHC 和时钟同步》展开时钟读取与跨设备对时。

<a id="hft03-references"></a>
## 参考资料

- 并发通信与性能分析：[David Gross — When Nanoseconds Matter](https://github.com/CppCon/CppCon2024/blob/main/Presentations/When_Nanoseconds_Matter.pdf)、[LMAX 技术博客](https://technology.lmax.com/)。
- Sanchit Gupta：[The Latency Slippage](https://github.com/CppCon/CppCon2026/blob/6f687701cdb4099eae39011485065b52ab7da0e9/Presentations/The_Latency_Slippage.pdf)。配套代码：[跨核交接与计时](https://github.com/0xsanchit/CacheLatency/blob/5ea23c07d524a057179e6db52b8038131927ffd1/src/exp/10g_options_clwb.cpp)、[单次跨核读取](https://github.com/0xsanchit/CacheLatency/blob/5ea23c07d524a057179e6db52b8038131927ffd1/src/exp/06_dirty_line_level.cpp)、[计数框架](https://github.com/0xsanchit/CacheLatency/blob/5ea23c07d524a057179e6db52b8038131927ffd1/src/exp/harness.h#L216-L285)。
- Daniel Anderson：[When Lock-Free Still Isn't Enough](https://github.com/CppCon/CppCon2024/blob/main/Presentations/When_Lock-Free_Still_Isn%27t_Enough.pdf)。有界并发算法与引用计数设计。
