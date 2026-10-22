# HFT Engineering从入门到入土（10）出现概率万分之一的Jitter，应该如何采样

做低延迟系统的人大概都遇到过这种时刻：p50 两微秒，p99 五微秒，数字漂亮得能直接贴进周报。然后某一天盘后复盘，你发现那笔本该抢到流动性的单子晚了三十微秒——行情已经跳走了，单挂在了一个已经没有意义的价位上。更让人难受的是，把 perf、strace、日志全开着复现了三天，这种事情一次都没有再发生。

这一篇就专门聊这种事。它不是某种具体的 bug，而是一整类问题的统称：系统在绝大多数时候很快，但每隔几万、几十万次请求，会冒出一次几十微秒的尖峰。我们先看清楚为什么常规手段注定抓不到它，再讲一套经过机构验证的取证方法，最后聊几个特别容易踩的坑——包括观测本身怎么把自己变成新的抖动源，以及上了公有云之后这套打法要做哪些妥协。

## 1. 中位数是面子，尾部是钱

先统一一下问题的形状。假设你的系统每秒处理几十万笔请求，正常处理时间 2~5µs，但大约每 5 万笔出现一次 40µs 的尖峰。从分布上看是这样的：

[![p50/p99 集中在 2~5µs，p99.99 上有一根几万次才出现一次的尖峰](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-01-tail-profile.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-01-tail-profile.png)

这个形态有两个值得注意的地方。

第一，只看 p99 根本看不见它。万分之一的概率对应 p99.99 这一档，p99 完全被前面 99% 的正常请求占据。很多团队的监控面板只放到 p99，于是这类事件在监控上等于不存在——它真实地造成了亏损，但没有触发任何告警。

第二，它造成的伤害和它出现的概率完全不成比例。正常请求快几微秒，赚的是排队位置上的边际优势；而一次 40µs 的尖峰发生在仓位暴露的方向上，就是实打实地错过一档成交、吃到一次 adverse selection。优化中位数提升的是期望，控制尾部控制的是破产概率。

所以做低延迟的人常说一句话：**中位数靠优化，尾部靠取证。** 中位数问题是工程问题，profile、压测、迭代；尾部问题是刑侦问题，你需要的是案发现场的证据链。

## 2. 为什么常规手段抓不到

### 2.1 采样器在抽签，而你要抓的东西一闪而过

`perf record` 这类采样 profiler 的工作方式是周期性地打断程序，记下当前的指令指针和调用栈。默认采样率 `-F 99` 意味着大约一毫秒采一次。

问题来了：尖峰只活 40µs。

[![尖峰只存活 40µs，而采样大约 1ms 一次，撞上它的概率不到 4%](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-02-sampling-miss.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-02-sampling-miss.png)

40µs 落在 1ms 采样间隔里的概率不到 4%，而尖峰本身的概率又是万分之一。两个概率叠在一起，你需要采到大约一百万个样本才可能撞上一次——而且撞上了也没用，采样拿到的是"此刻栈是什么"，不是"这 40µs 里发生了什么"。等你在报表里看到异常、手动 attach 上去，那次抖动早就结束了。现场已经被销毁了。

提高采样率能不能解决？能提高命中概率，但不能解决两个根本问题：一是你仍然无法完整保留异常前后的上下文，二是高采样率本身就会制造延迟（第 7 节细讲）。Jane Street 在分享里说得很直白：他们的交易线程大部分时间在忙轮询，采样间隔远大于真正的响应时间，关键代码里几乎落不到样本，profiler 显示的结果"大约等于没用"。

### 2.2 全量日志更糟：观察者成了凶手

另一个直觉反应是"那我把每次调用的详细日志都记下来"。这条路在普通服务上可行，在微秒级热路径上是自杀。每次调用都格式化字符串、加锁、写文件，单次开销动辄几微秒，等于给每一笔订单都加了一次抖动——你不再需要观测抖动了，因为抖动是你自己造的。

观测必须满足成本预算：热路径上每次观测的开销 C，必须远小于相邻请求的到达间隔 1/λ。在每秒百万包的速率下，这个预算只有 1µs，扣掉正常业务之后留给观测的只有几十到几百纳秒。

### 2.3 还有一种看不见，是压测器自己瞎了

比"抓不到现场"更隐蔽的一个陷阱，是你连这件事的真实发生频率都量错了。Gil Tene 把它叫做 Coordinated Omission（协同性遗漏）。

考虑最常见的闭环压测：发一个请求、等它回来、再发下一个。当系统在某一笔请求上卡住 40µs 时，压测线程也跟着堵在这一笔上。这 40µs 里，按照目标速率本应发出的几十个请求，根本就没有发出去。

[![闭环压测线程被卡住时，卡顿期间本应发出的请求从未出生](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-03-coordinated-omission.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-03-coordinated-omission.png)

最终统计里，只有真正卡住的那 1 笔被记成慢请求，卡顿期间"缺席"的几十笔从来没有出生，自然也不会出现在延迟直方图的尾巴里。真实生产环境是开环的——行情不会因为你卡住了就等你，那几十笔请求在生产中照样会来，而且每一笔都会带着积压的排队延迟。

对策有两个：一是改用开环（open-loop）压测，请求由独立时钟按计划发出，不等上一笔返回；二是用 HdrHistogram 的 `recordCorrectedValue`，把卡顿区间内应发未发的请求按预期发起时间一并记为受影响延迟。改完之后你会发现，尾巴突然长出一大截——它一直都在，只是压测器替你把它藏起来了。

## 3. 总体思路：一套三层取证体系

单次抖动的成因可能出在三个完全不同的层面：应用自己多干了活、内核或硬件打断了它、包根本就迟到了。任何单一工具都只能看到其中一层，所以机构里的标准做法是搭一套分层取证体系：

[![应用层、内核层、物理网络层三层取证，各自回答不同问题](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-04-three-layer-forensics.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-04-three-layer-forensics.png)

- **应用层**回答"慢了多少、慢在哪一类"。手段是分段 rdtscp 打点、无锁环形缓冲黑匣子、PMU 硬件计数器差分。成本是每笔请求固定的纳秒级开销，可以 7×24 常驻。
- **内核层**回答"具体元凶是谁"。ftrace、rtla、perf/eBPF 能告诉你缺页发生在哪个地址、CPU 被切给了谁、是哪个中断在捣乱。关闭时开销接近于零，事件真正发生时才付 50~200ns。
- **物理网络层**回答"这个包到底几点到的"。光分路器加 FPGA 抓包卡在操作系统之外独立打硬件时间戳，对被测主机零负载。这一层的意义是提供独立第三方事实——嫌疑人不能给自己作证。

三层的证据要能拼到一起，前提是共用同一个时间基。机内全部用 TSC 对时（ftrace 要设 `trace_clock=x86-tsc`，和应用的 rdtsc 读数同钟）；跨机器用 PTP 配合 PPS 或 GPS Grandmaster 硬件授时。没有这一步，你会陷入"应用说包早到了、网卡说包晚到了"的罗生门，因为两个时间戳根本不在一条时间线上。

接下来逐层展开，重点放在第一层——那是你自己能完全掌控、也是性价比最高的一层。

## 4. 应用层：一个常驻的飞行记录器

### 4.1 黑匣子的设计原则

核心想法借自飞机的黑匣子：**不管有没有异常，记录一直在进行；异常发生时，异常之前的历史已经在缓冲区里了。** 这正好解决了第 2 节的死局——采样器要在案发后抽签，黑匣子在案发前就已经开着机。

具体实现是一个 per-thread（或 per-CPU）的预分配无锁环形缓冲，单生产者单消费者：

- 在关键路径的分段边界（RX → 解析 → 决策 → 风控 → TX）用 `__rdtscp()` 打时间戳。rdtscp 是半串行指令，能防止它前后的指令乱序越界把测量窗口测歪；
- 每条探测记录（消息 ID、各段时间戳、队列深度等）顺序写进环形缓冲，只保留最近 N 万条，满了就滚动覆盖；
- **热路径上不分配内存、不加锁、不做系统调用、不写磁盘**。这四条是硬规矩，破了任何一条，观测开销都会从纳秒级掉到微秒级；
- 缓冲由独立的"遥测核"异步消费，落盘、压缩、上传都在那边做。

一个最小的打点长这样：

```c
struct LatencyProbe { uint64_t t_rx, t_decision, t_tx; };

static inline void on_packet() {
    unsigned aux;
    uint64_t t0 = __rdtscp(&aux);
    /* parse + book + signal + risk + order build */
    uint64_t t1 = __rdtscp(&aux);
    /* 写入预分配无锁 ring，遥测核异步消费 */
    publish_probe((struct LatencyProbe){t0, /*...*/, t1});
}
```

容量可以简单估一下：每条记录 64 字节、每秒 100 万笔，写入量约 64MB/s，一个 8MiB 的缓冲就能保住最近 130ms 的历史。而一次典型尖峰只有几十微秒，窗口绰绰有余。

在线统计用 HdrHistogram 常驻维护 p50/p95/p99/p99.9/p99.99 全档位，别只看 p99。

### 4.2 越限之后：只发信号，不抓现场

黑匣子解决了"历史已经在手里"，还差一个"异常发生时把当时的现场冻结下来"的动作。这里有一个非常关键的分工，整套机制长这样：

[![热路径打点写ring，越限时只做原子发布；遥测核负责trace_marker、snapshot、落盘](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-05-flight-recorder.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-05-flight-recorder.png)

热线程发现 `ΔT > 阈值` 后，只做四件事：写 ring（本来就在做）、一次比较、一次原子 store 把 `(seq, t0, t1)` 发布到无锁队列、保存必要的寄存器。代价 5~10ns。然后**立刻回头处理下一个请求**，不等任何人。

真正的取证在独立的遥测核上进行：

1. 轮询原子标志（或通过 eventfd 被唤醒，看你能接受多大代价）；
2. 往 `trace_marker` 写一行带 seq 和裸 TSC 的标记；
3. 对 ftrace 的 snapshot 文件执行一次写操作，让内核**原子地交换缓冲指针**；
4. 从被冻结的那份副本里读出异常前后的窗口，异步落盘；
5. 用 latch 加冷却窗口限流，防止连环触发把系统淹掉。

第 3 步的"先交换、后读取"是整个设计里最容易做错的地方。直觉的做法是发现异常后去 ring 里"捞最近的事件"，但遥测线程是个独立线程，它可能在几百微秒后才被调度运行——这段时间里 live ring 早被新事件冲了好几轮，异常窗口已经没了。正确的做法是让内核用一次指针交换把缓冲冻住，你拿到的是一张不再变化的快照。ftrace 的 snapshot 缓冲在启动时预热一次，之后只轮换不释放。

### 4.3 队头阻塞：快照的账不能只算在异常请求头上

这里要纠正一个常见的错误说法："快照只影响那个本来就已经慢了的请求，无所谓。"

在串行或共享队列的热路径上，事情不是这样的。异常请求 N 如果当场同步花 C 微秒抓栈、回溯、memcpy，排在它后面的 N+1、N+2、…… 全部要陪着等，整体排队时间向后平移，直到队列排空。这叫队头阻塞（head-of-line blocking）。

[![同步快照让异常请求变长C，后续请求全部后移；异步发布后后续请求不受影响](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-06-head-of-line-blocking.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-06-head-of-line-blocking.png)

被波及的请求数大约是 `λ × C`：每秒 100 万包、C = 5µs 时，约有 5 个无辜请求被拖慢。而且利用率 ρ 越接近 1，队列排空越慢，污染被放得越大。所以观测成本的预算约束不是"异常请求能不能容忍多几微秒"，而是 **C 必须远小于请求间隔 1/λ**。1M pps 下想把波及面压到 1 以下，C 就得小于 1µs——这一条直接判了"在热核上同步抓用户态栈"的死刑。

分析侧也要把元凶和受害者分开。同时记录每条消息的入队时间和各阶段开始/完成时间：处理时间越界的 N 是根因；从 N+1 开始处理时间正常、但排队等待变长的那一簇是排队排空的受害者，统计上应该算作 **1 次根因事件**，而不是 k+1 次独立 jitter。否则你会拿着错误的发生频率去定位错误的问题。

## 5. 给这一次抖动定罪：rdpmc 与硬件计数器

光知道"这一次慢了 40µs"还不够，你得知道这 40µs 花在了哪。靠软件日志分不出来，但 CPU 自带的性能监控单元（PMU）可以。

### 5.1 rdtsc 和 rdpmc 不是一回事

先区分两条指令：

- `rdtsc/rdtscp` 读的是时间戳计数器。它是架构级的非特权指令，现代虚拟机（KVM、AWS Nitro）通过 TSC offsetting 透传，通常不会触发 VM-Exit，容器里也能用。注意 vCPU 被宿主机调走期间 TSC 照样在走——这个特性反而是我们识别"核被拿走"的基础。
- `rdpmc` 读的是 PMU 硬件计数器（周期数、指令数、缓存缺失等）。指令本身只要 24~40 个周期，但**裸调没有意义**：物理计数器是全核共享的资源，槽位由内核动态编程，裸读拿到的值归属是错的；在 `CR4.PCE=0` 的机器上还会直接触发异常。

rdpmc 唯一合规的用法，是通过 `perf_event_open` 注册事件，再 mmap 它的共享元数据页，走用户态快路径读取：

```c
static inline uint64_t read_pmc(struct perf_state *p) {
    uint32_t seq; uint64_t count, enabled, running;
    do {
        seq = __atomic_load_n(&p->pc->lock, __ATOMIC_ACQUIRE);
        if (seq & 1) { __builtin_ia32_pause(); continue; }   // seqlock：内核正在改
        uint32_t idx = p->pc->index;
        if (p->pc->cap_user_rdpmc && idx)
            count = __rdpmc(idx - 1) + p->pc->offset;        // 纯用户态快路径
        else
            return read_perf_fd(p);                          // 慢路径：真系统调用，2~9µs
        enabled = p->pc->time_enabled;
        running = p->pc->time_running;
    } while (__atomic_load_n(&p->pc->lock, __ATOMIC_ACQUIRE) != seq);
    if (running != enabled)                                  // 发生过多路复用
        count = (uint64_t)((__int128)count * enabled / running);
    return count;
}
```

初始化时一次性 `perf_event_open` + `ioctl(RESET/ENABLE)` + mmap，热路径上只是 seqlock + rdpmc + offset，约 100~300 cycles。事件组要用 pinned + group，让整组计数器要么常驻硬件要么直接失败，避免内核轮换（multiplex）带来的缩放误差。另外注意这跟 vDSO 没关系——vDSO 服务的是时钟类接口，PMU 的共享页是 perf fd 自己 mmap 出来的。

### 5.2 三级采样：解决"每次读太贵、触发再读太迟"

PMU 计数器是单调累计的，归因靠相邻边界读数做差分。这里有个两难：每个边界都读一堆计数器太贵；但等检测到越限再第一次读，拿到的差值横跨了成千上万次请求，定位不到这一次。

实务上的解法是分三级：

| 级别 | 读什么 | 频率 | 目的 |
|---|---|---|---|
| ① | TSC（rdtscp） | 每个分段边界 | 触发器和时间窗，约 10ns |
| ② | 三个固定功能计数器：instructions / cycles:u / ref-cycles | 每个边界 | 五大类归因，约 100ns |
| ③ | 通用事件（LLC-miss、branch-miss 等） | 触发后突发开窗 + 低频 PEBS 常驻 + 诊断构建 | 微架构细分 |

第②级特意只选三个固定功能计数器，是因为它们有独立的硬件槽位，不占用只有 4~8 个的通用槽（开超线程还要减半），不会引发 multiplex。第③级的通用事件只在异常触发后短暂开窗读 K 次。还有个省钱的细节：请求 N 结束时的读数就是请求 N+1 的起始读数，每个边界只需要采一轮。

### 5.3 一张归因决策树

拿到差分之后怎么看？ΔT(TSC) 的构成只有四类来源：用户态执行、内核态执行、被切走的时间、硬件 halt 或虚拟化时间。用计数器组合就能区分：

[![ΔT归因决策树：指令变多、微架构停顿、内核介入、off-core、降频](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-07-pmc-decision-tree.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-07-pmc-decision-tree.png)

- **Δinstructions 明显变大**：这次多执行了指令——走了数据相关的冷分支、异常分支，或者在处理 fault。
- **指令数正常、cycles:u 变大、LLC-miss 高**：指令没多，但在等硬件——内存/缓存停顿、分支预测失败、跨 NUMA 访存。
- **cycles:u 不动、内核时间变长、minflt +1**：时间花进了内核——minor/major page fault 或系统调用。
- **TSC 在走而 ref-cycles 停走，出现缺口**：核根本没在执行你的代码——被抢占、vCPU 被宿主机调走、halt 进了深睡状态，或者 SMI。
- **core-cycles/ref-cycles 比例下降**：CPU 降频了——AVX-512 指令的频率许可、温度或功耗墙、云上的频率限制。

### 5.4 那个最经典的签名：TSC 在走，计数器停了

第四类值得单独多说几句，因为它是低延迟尾部最经典的形态之一。CPU 执行 HLT/MWAIT 进入 idle（C-state）时，所有标记为 UNHALTED 的硬件事件全部冻结，但 TSC 是墙上时间，谁也停不下来。两条时间线一对，缺口一目了然：

[![halt期间TSC连续而UNHALTED计数器冻结，唤醒还要付退出税](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-08-halt-cstate-gap.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-08-halt-cstate-gap.png)

C1 浅睡的退出延迟约 1µs，而 C6/CC6 深睡的退出可达几到几十微秒——这正好是典型的尾部尖峰形状。预防手段是让热核忙轮询、启动参数加 `intel_idle.max_cstate=1 processor.max_cstate=1 idle=poll`，不给它睡下去的机会。两个细节别搞混：`PAUSE` 指令不是 halt；而在虚拟机里 vCPU 被宿主机调走时，计数器上的签名和真 halt 完全相同，要靠 `%steal` 和 sched_switch 记录才能区分是谁干的。

最后给一个精度上的边界：一组 rdpmc bracket 自身大约要执行 400 条指令。实测（Zen4 约 450 cycles、SPR 约 1165、Skylake 约 1680）表明，被测区域至少要有 1.2 万条指令、4 千次以上访存，读数才可信；内核 ioctl 那种路径要 10 万~40 万条指令才收敛。别给一个几十条指令的小函数打点，读数里的噪声比信号还大。

## 6. 内核层：让操作系统自己留痕

应用黑匣子能告诉你"这一次是被切走了"，但切给了谁、为什么，要靠内核层。

### 6.1 rtla timerlat：给主机做体检

`rtla timerlat` 是内核自带的定时器延迟测量工具，可以理解成"带内核归因的 cyclictest"。它在每个被监控 CPU 上起一个 SCHED_FIFO:95 的内核线程，每周期（默认 1ms）睡眠到绝对时刻 T，然后分别报告两个数：

- **IRQ latency**：硬中断实际触发时刻减去 T。这个数大，说明中断被 SMI、NMI、关中断区间或其他 IRQ 挡住了；
- **Thread latency**：从中断触发到线程真正被调度上 CPU 的时间。这个数大，说明唤醒后的调度被其他线程干扰了。

配套的 osnoise tracepoint 还会直接报出具体是哪个 IRQ（irq_noise，精确到纳秒）、哪个线程（thread_noise）在制造噪声。越限时可以用 `stop_tracing_us` 自动停住 tracer，把当时的栈和事件完整存进文件：

```bash
# Thread latency 超过 30µs 即停追踪、存栈，监控 2~9 号核，跑 2 小时
sudo rtla timerlat top -s 30 -t 30 -T -c 2-9 -p 1000 -d 2h
```

要清楚它的定位：这是主机准入、burn-in、长时间 soak 的**体检工具**，不是热核常驻探针。它每毫秒真的会产生一次本地 APIC 时钟中断——这是测量原理，不是副作用——在 `nohz_full` 隔离核上还会强制周期性地重新打开时钟中断。生产常驻要放在 housekeeping 核上，或者拉长周期；热核的持续观察还是交给应用黑匣子。同族的 `rtla osnoise` 量化中断和线程噪声，`hwlat_detector` 关中断循环读 TSC 专抓 SMI 和固件级停顿（注意它会占满 CPU 且关中断，内核文档明确禁止生产环境使用）。

### 6.2 ftrace：黑匣子在内核里的对应物

ftrace 是内核官方的追踪框架，挂在 tracefs（`/sys/kernel/tracing/`）上。低延迟场景主要用这几样：tracepoint 事件（`exceptions:page_fault_user`、`sched:sched_switch`、`irq:irq_handler_entry` 等）、专用 tracer（osnoise/timerlat/wakeup/irqsoff）和 trigger（hist 内核直方图、snapshot 原子换缓冲、stacktrace）。它的 per-CPU ring 支持 overwrite 模式，天然就是黑匣子语义。

开销分四档，差距非常大：

| 档位 | 形态 | 开销 |
|---|---|---|
| ① 关闭 | 函数入口被热补丁成 NOP，tracepoint 走 static key | 近乎零，benchmark 看不出差异 |
| ② 低频 tracepoint 常驻（推荐） | 事件真实发生时写一次 per-CPU ring | 几十~几百 ns/事件；忙轮询纯用户态时为 0；典型组合整机 <1% |
| ③ function tracer 带过滤短窗口 | 每个被追踪函数入口付费 | 微秒级/函数点，只能短时窄过滤使用 |
| ④ 高频事件 / 全量 function_graph / 持续 cat trace_pipe | — | 10% 到 10 倍，禁止上生产热核 |

估算公式很简单：常驻开销 ≈ 事件率 × 单事件成本。每秒 1 万次事件、每次 100ns，大约 0.1% 的核占用。

生产配置的要点：`tracing_cpumask` 只圈热核、per-cpu buffer 给大、开 `options/overwrite`、时钟设成 x86-tsc、只开 fault/sched/irq 这几个低频事件：

```bash
cd /sys/kernel/tracing
echo nop    > current_tracer
echo f      > tracing_cpumask          # 只圈热核（按实际位图）
echo 8192   > buffer_size_kb
echo 1      > options/overwrite        # 黑匣子覆盖语义
echo x86-tsc > trace_clock            # 与应用 rdtsc 同钟
echo 1 > events/exceptions/page_fault_user/enable
echo 1 > events/sched/sched_switch/enable
echo 1 > events/irq/irq_handler_entry/enable
echo 1 > snapshot                     # 预热快照缓冲
echo 1 > tracing_on
# 越限后由遥测核：write(trace_marker,"SPIKE seq=..")，再 echo 1 > snapshot
```

### 6.3 perf 的正确用法：计数常驻，采样只做体检

`perf stat` 和 `perf record` 要分开评价。

`perf stat` 是纯计数：硬件计数器累加，结束时读一次，典型开销 <0.5%，可以谨慎地在生产上盯少量事件。坑是事件数超过硬件槽位会触发 multiplex，以及 `-I` 周期打印本身有 read 系统调用成本。

`perf record` 是采样：计数器溢出时触发 PMI（x86 上是 NMI）打断现场抓栈。它对低延迟系统的真正伤害不是平均开销，而是**随机注入的中断对 p99.99 的污染**——NMI 直接插进执行流，破坏 isolcpus 的隔离，返回后 L1/TLB/分支预测器还被冲过。具体数字感受一下量级：`-F 99` 加帧指针栈典型 1~5%；`--call-graph dwarf` 每个样本拷 8KB 栈，5~20% 起步，禁止上热核；`-F 9999` 是 5~30%。LBR 在多数云环境里还不可用。

所以生产时段的纪律是：交易核上不做 perf record 采样、不挂动态探针（uprobe 靠断点实现，每次命中陷内核，挂在每笔消息必经的函数上等于给每笔订单加税）。计数模式、非关键核、盘后、回放环境里，perf 全套工具照常随便用。量化 perf 影响的方法也很直接：开/关 perf 跑两组尾部直方图对比，看 p99.99 差多少，而不是看平均 CPU%。

顺带一提，`nmi_watchdog` 硬锁检测器本身就是一个周期性 perf 事件，低延迟机器的调优清单里通常会加 `kernel.nmi_watchdog=0`，既去掉周期 NMI，又腾出一个硬件计数器。

## 7. 观测税：怎么在不污染结果的前提下做分析

承认了 perf record 会扰动尾部，下一个问题是：那优化还要不要做？要做，但要把"测真值"和"找瓶颈"拆到不同的运行里。机构里常见的做法是同一份行情 trace 跑五组：

[![P0干净基准、P1/P2 on-CPU、P3/P4阻塞内核、P5加压注入，尾部数字只信P0](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-09-replay-matrix.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-09-replay-matrix.png)

- **P0 干净基准轮**：零诊断，只留常驻黑匣子，全速跑。这是唯一当真值的延迟数字，要过 replay parity 校验（见第 8 节）；
- **P1/P2 on-CPU 挖瓶颈轮**：perf record、perf stat 分组、PEBS/VTune。热点是个比例量，均匀减速不改变函数排序，这些数字只取相对占比；
- **P3/P4 阻塞与内核轮**：off-CPU 火焰图、perf sched、function_graph 短窗、timerlat/hwlat soak，专查锁等待、调度、fault、IRQ；
- **P5 加压/注入轮**：倍速回放、在指定 seq 注入 fault/缺口/重复包、噪声机、netem、随机调度延迟、过夜 soak。这一轮的目的是把 1/几万 的根因"喂"成 1/几百，让罕见事件高频复现。

A/B 对比时两边用相同的 profiling 配置，观测税在差分里自动抵消。有四类东西会被 perf 扭曲、必须换专门跑法：时间相关代码（用逻辑时钟回放）、off-CPU 占比（专跑 off-CPU 分析）、并发交错（专用竞态注入）、p99.99 尾部（只信 P0）。

cycles 火焰图本身也有盲区，顺带说三个最容易误判的：一是 LLC miss 的停顿虽然计入 cycles，但图上看不出原因，需要按 miss 事件另画火焰图、分函数算 IPC；二是普通采样有 skid，中断触发时 IP 已经往后飘了几条指令，Intel 上要用 `:pp` 精确事件（PEBS），否则热点常落在 miss 结果的"使用者"而不是真正的 load 上；三是 Off-CPU 时间完全采不到——线程睡在锁上时不消耗 cycles，图上不存在。Jane Street 分享过一个真实案例：下单逻辑本身占不到 5% CPU，订单却晚了 200µs，根因是旧 API 把流量排进了一个低优先级线程池，这种排队问题在 on-CPU 火焰图里是隐形的。

如果需要在异常发生后回看控制流，还有一件重武器是 Intel PT：硬件把压缩后的分支轨迹持续写进环形缓冲，触发时截取前约 10ms 解码成执行时间线。Jane Street 开源的 magic-trace 把这套流程封装得很好用，他们用它查出过一次 100ns 的回归（根因是一个函数没被内联）。但要注意它的成本：官方数据是持续记录通常让应用慢 2~10%，取一次快照停顿约 10µs，所以默认做法是轻量检测常开、PT 在诊断窗口提前启用、采够样本就关，而不是 7×24 常开。而且 PT 记录的是控制流，不是所有 CPU 事件的完整日志——它不直接记录每次访存的值和 LLC miss，缺页地址和电源事件还需要特定硬件扩展；普通公有云 VM 上 PT 基本默认不可用，上裸金属再考虑。

## 8. 把现场变成可复现的实验：记录与回放

抓到一次现场只能修一个 bug。想系统性地压尾部，需要把生产发生过的事情原样带回实验室。这是事件记录与回放体系要解决的问题，分几个平面：

- **L0 物理取证**：TAP + FPGA 抓包卡的线速原始 PCAP，带 GPS/PTP 纳秒时间戳，独立供电，是第三方事实基准。行业里 Databento 用 FPGA 卡支持 100Gbps 线速无损抓包，时间戳亚微秒同步且单调；云上的替代品是 Nitro 硬件包时间戳；
- **L1 主机收包**：kernel-bypass 收包，网卡硬件 RX 时间戳随包记录，hugepage 环异步落盘，可以和 L0 互证；
- **L2 归一化事件流**：解析只做一次，输出带全局单调 seq 和硬件纳秒时间戳的统一二进制事件流，单写者追加式发布，策略代码不直接碰网络字节；
- **L3 决策日志**：策略写成纯函数状态机 `f(状态, 事件) → (新状态, 动作)`，输入事件、报价、风控判定、报撤单、ACK/FILL 全部追加记录，字段里带上 seq、响应的输入 seq、二进制和配置哈希、各段 TSC。时钟、随机数、定时器全部注入。

工业上常用的组件各管一段：Chronicle Queue 做 mmap 追加日志（无分配无 GC，同进程 P99 <5µs），Aeron Archive 做流的全速率持久化和任意位置回放（ReplayMerge 能在实时和回放间无缝切换），kdb+/q 做纳秒 tick 的盘后研究和成交重建，LMAX Disruptor 则是应用内单写者预分配环的标准实现。

这套体系有一条铁律：**热路径只做内存追加，磁盘、压缩、上传全部在独立核异步；所有非确定性输入（随机种子、定时器、配置版本）都必须进日志。** 漏掉任何一个，回放就无法重现生产。

回放分两种。网络级回放从 PCAP 按原始硬件时间戳间隔，经 DPDK/ef_vi 重放回收包口（tcpreplay 精度不够，不合格），重现包间时序、突发、乱序和缺口；应用级回放把归一化事件日志按 seq 灌进同版本策略，全速模式做回归（确定性只依赖事件顺序），原始 pacing 模式重现排队、超时和竞争。

验收标准叫 replay parity：回放产出的决策流和生产决策日志**逐笔逐字段做 bit 级比对**。同一个二进制哈希下只要有任何差异，就说明还有没被捕获的非确定性来源——系统时钟、真随机数、哈希表遍历顺序、浮点、竞态、没记录的定时器。每版本必须过全天 tick 的 parity 才允许上线。

## 9. 物理层：为什么需要一个"不在场"的证人

应用日志和内核日志都来自被测主机自己。如果争议是"包到底几点到的"，本机任何软件时钟都有自证嫌疑——而且软件抓包本身受调度延迟影响，忙的时候还会丢包。

物理层的标准方案是**被动光分路器（optical TAP）**：在交易所光纤上物理复制一路光信号给 FPGA 抓包卡，卡在 MAC/物理层给每个包打纳秒级硬件时间戳，发生在操作系统处理之前，主机负载为零，也无法被主机上的任何软件影响。商业方案如 Corvil（Pico）能逐包捕获 order/tick/fill，带 600+ 协议解码器和 NIST 可溯源时间，同时满足监管审计要求（RegNMS、MiFID II、CAT）。收包侧则用低延迟网卡加 kernel bypass（Solarflare ef_vi/Onload、DPDK）忙轮询，消除系统调用、上下文切换和中断合并带来的尾差，网卡本身也提供每包硬件时间戳。

跨机时钟同步用 PTP 加硬件 PPS/GPS Grandmaster，别用纯软件 NTP——NTP 的毫秒级精度在微秒世界里等于没有对时。

## 10. 公有云专题：目标从"消灭"退化为"量化和规避"

上面这套打法在自建机房里目标是消除抖动。上了公有云，先得接受现实：C-state 策略、SMI、网卡固件、在线迁移、邻居负载，全都在租户够不到的层级。Guest 里量到的延迟包含宿主机把 vCPU 调走的时间，但你看得到结果、拿不到宿主的栈。所以云上的目标退化为三步：量化抖动、分清"我的问题还是云的问题"、用选型规避。

### 10.1 先搞清楚自己买到的是几级独占

AWS 上固定性能实例（m/c/r/i 等非 t 系列）的 CPU 和内存在实例驻留期间是预分配、不超卖的，vCPU 静态绑定到专用物理超线程；突发型 t 系列允许超卖，steal 和限速都来自这里，低延迟场景直接排除。从 t 系列到裸金属，中间有五级：

[![从t系列突发到metal裸金属的五级独占路径](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-10-cloud-exclusive-path.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-10/jitter-10-cloud-exclusive-path.png)

性价比甜点是第三级：固定性能实例启动时关掉 SMT（`--cpu-options CoreCount=N,ThreadsPerCore=1`，价格不变），一个 vCPU 就独占一整个物理核，消除了 SMT 兄弟线程干扰。Dedicated Host/Instance 主要解决合规和 BYOL，hypervisor 还在，对抖动的增量有限。真正拿回全部控制力要到 `.metal` 裸金属：没有 hypervisor，没有 steal 和 VM-Exit，C/P-state、PMU、rtla/hwlat 全部恢复物理语义。选型上单线程频率敏感的 workload 可以看 r7iz（Sapphire Rapids，全核持续 turbo 3.9GHz）和 z1d（定制 Xeon 全核持续 4.0GHz，官方直接点名金融场景）；ARM 侧 Graviton 固定频率且无超线程，但 OS 层不开放 C/P-state 控制，要控制力仍需 metal。

### 10.2 rdtsc/rdpmc/PT 在云上的可用性

权限、语义、可控性是三道墙。容器默认没有 perf/ftrace/eBPF 权限（需要 CAP_PERFMON/CAP_BPF/CAP_SYS_ADMIN，托管 K8s 常被安全策略禁掉）；vPMU 的透传则按机型分层：

| 环境 | rdtsc | rdpmc 固定计数器 | rdpmc 通用事件 / PT |
|---|---|---|---|
| .metal 裸金属 | 完整 | 完整，PMU 全暴露 | 全量 + PEBS |
| 大规格固定 VM（如 *9g 12/24/48xl） | 可用 | vPMU 透传 | 全量 PMU 事件 |
| 中小规格 VM | 可用 | 通常有 cycles/inst（以实测为准） | 常不支持 |
| 容器 / 托管 K8s | 可用 | 默认不可用 | 默认不可用 |

开机后 30 秒就能判定：`grep constant_tsc /proc/cpuinfo`、看 `perf_event_paranoid`、mmap 后读 `cap_user_rdpmc/index`、`perf stat -e cache-misses true`、容器里 `capsh --print`。vPMU 缺失不是"慢一点"，而是快路径直接失效，落进 2~9µs 的 `read()` 系统调用，热路径上等于不可用。Intel PT 更保守：支持普通 PMU 事件不代表支持 PT，普通 VM 在实测验证之前一律按不可用设计，Dedicated Host 也不等于裸金属。

### 10.3 云上 halt 的三条路和云上的独立时钟

Guest 里看到的 halt 有三种来源：自己 idle 执行 HLT（可以靠忙轮询和 max_cstate=1 防）、vCPU 被宿主机 deschedule（突发超卖、宿主维护、在线迁移，guest 防不住，靠 `%st` 识别）、固件 SMI 和封装级功耗预算（只有裸金属能看到）。

云上没有物理 TAP 旁路（VPC Traffic Mirroring 是虚拟路径，不是纳秒取证级），替代方案是 Nitro 体系：DPDK 有 ENA 官方上游 PMD，guest 内把 ENA 绑到 vfio-pci 就能用户态轮询（注意 LLQ 低延迟队列保持默认开启、metal 上必须用 vfio-pci 而非 uio）；Nitro PTP 硬件时钟表现为 `/dev/ptp*`，不耗客户资源；2025 年中起 Nitro 还在内核和 socket 之前给每个入站包打 64 位纳秒硬件时间戳，用标准 `SO_TIMESTAMPING` API 读取（`ethtool -T eth0` 验证）。配套工程上，用 ≥8xl 的固定带宽规格、同 AZ 集群置放组、ENA Express/SRD、Capacity Reservation 减少换宿主，重启后重跑基线和 TSC 周期校准。

## 11. 根因消除：抓到之后怎么办

取证体系解决的是"看见"，最后还得真把元凶干掉。两类最常见、也最根治的：

**部署纪律（NUMA 三车道）**。先 `lscpu -e=cpu,node,socket,core` 和 `numactl --hardware` 摸清拓扑，热数据、收包队列、策略线程同 NUMA，网卡在哪个节点就贴着哪个节点。启动参数隔离热核：`isolcpus=2-9 nohz_full=2-9 rcu_nocbs=2-9 intel_pstate=disable processor.max_cstate=1 idle=poll`。IRQ 亲和和 RSS 队列固定到指定核并核对 `/proc/interrupts`，热线程代码里显式绑核加 SCHED_FIFO，watchdog 和 kill-switch 留独立核。hugepage 加 NUMA 本地分配，启动时拒绝静默回退到远端节点。热路径零 malloc、零锁、零阻塞 syscall、零磁盘日志。

**minor page fault 根治**。运行期的 minor fault 是尾部榜单上的常客：`mlockall(MCL_CURRENT | MCL_FUTURE)` 锁内存，warmup 阶段逐页触碰所有热数据结构和预分配池，`MAP_POPULATE` 和 hugetlb 预触。特别小心 THP——一次 2MB 大页的 fault 要当场清零，代价几十到上百微秒，是典型的"罕见但致命"，用预触 hugetlb 或 `madvise(MADV_NOHUGEPAGE)` 避免 khugepaged 运行期介入；关掉 `kernel.numa_balancing=0`，否则自动 NUMA 平衡会拆 PTE 制造 hinting fault。验收标准是 soak 之后 `/proc/<tid>/stat` 里的 minflt 稳态零增长。容器里什么权限都没有时，也有零权限兜底：遥测核轮询 `/proc/self/task/<tid>/stat`（minflt/nivcsw/nvcsw）、`/proc/interrupts`、`/proc/stat` 的 steal、cpuidle/cpufreq sysfs，一样能搭出基础观测。

## 12. 成本速查与落地建议

最后把贯穿全文的数字汇总成一张表，热路径上的每个动作都该对照它做预算：

| 动作 | 典型成本 | 能不能上热路径 |
|---|---|---|
| RDTSC / RDTSCP | 20~40 cycles（约 7~13ns @3GHz），云上通常无 VM-Exit | 常开 |
| perf_event 快路径读 1 个计数器 | 100~300 cycles（30~100ns） | 常开（固定计数器） |
| 稳态完整记录（TSC×2 + 3 固定 PMC + ring） | 约 100~250ns/请求，边界复用可压到 60~120ns | 常开，约占 5~10µs 路径的 1~4% |
| 应用无锁 ring 写一条 | 10~30ns | 常开 |
| 阈值原子发布 | 5~10ns | 热核唯一同步动作 |
| ftrace tracepoint | 50~200ns/事件，仅事件发生时 | 低频组合常驻（整机 <1%） |
| snapshot 交换 + 落盘 | 热路径 0ns（全在遥测核） | 越限时 |
| `read(fd)` / ioctl 内核路径 | 2~9µs | 禁止 |
| perf stat 计数 | <0.5% | 谨慎常驻 |
| perf record -F99 fp | 1~5%，且随机 PMI 污染尾部 | 只做短窗体检 |
| perf record dwarf / -F9999 | 5~30%+ | 只在回放/压测环境 |

预算口诀：**TSC 常开约 1%，TSC 加三个固定计数器约 2~3%，通用事件只开窗，read/ioctl 永远不进热路径。**

如果要从零搭一套，落地顺序建议是：先上 per-thread rdtscp 黑匣子加 HdrHistogram 全档位（一周内能见效，立刻能知道尾部的真实形状）；再补阈值原子发布加遥测核 ftrace snapshot（有了案发现场）；然后接三个固定 PMC 做五类归因；有条件再上物理 TAP 或云上硬件时间戳。每加一层，都用第 7 节的 P0 基准轮量化它对 p99.99 的影响——观测系统自己也要过观测这一关。

尾部优化这件事没有银弹。一次万分之一的抖动背后，可能是一个没预热的大页、一次邻居的 SMT 争抢、一个被宿主机拿走的 vCPU，也可能只是你自己日志库的一次 buffer flush。能确定的是：只要你不在案发之前就把黑匣子开着，这一万次里的那一次，就永远只会以"一笔莫名其妙亏掉的单"的形式出现在你的复盘里，而不会以证据的形式出现在你的调试器里。

## 参考资料

- Linux 内核文档：[timerlat tracer](https://www.kernel.org/doc/html/latest/trace/timerlat-tracer.html)、[osnoise tracer](https://www.kernel.org/doc/html/latest/trace/osnoise-tracer.html)、[hwlat_detector](https://www.kernel.org/doc/html/v4.19/trace/hwlat_detector.html)、[ftrace](https://www.kernel.org/doc/html/latest/trace/ftrace.html)、[perf-intel-pt](https://raw.githubusercontent.com/torvalds/linux/master/tools/perf/Documentation/perf-intel-pt.txt)
- [perf_event_open(2)](https://man7.org/linux/man-pages/man2/perf_event_open.2.html)、[perf-record(1)](https://man7.org/linux/man-pages/man1/perf-record.1.html)、[perf-mem](https://raw.githubusercontent.com/torvalds/linux/master/tools/perf/Documentation/perf-mem.txt)
- Brendan Gregg：[CPU Flame Graphs](https://www.brendangregg.com/FlameGraphs/cpuflamegraphs.html)、[Off-CPU Analysis](https://www.brendangregg.com/offcpuanalysis.html)、[perf 示例页](https://www.brendangregg.com/perf.html)
- PMC 开销实测：Mühlig, [*How Small Can a Measured Region Be Before perf Counters Lie?*](https://jmuehlig.github.io/when-can-you-trust-your-hardware-counters/)（Zen4/Skylake/SPR, 2025）
- Jane Street：[magic-trace](https://github.com/janestreet/magic-trace)、[Overhead 说明](https://github.com/janestreet/magic-trace/wiki/Overhead)、[System Jitter and Where to Find It](https://www.janestreet.com/tech-talks/system-jitter-and-where-to-find-it/)
- LMAX：[Timing is everything](https://technology.lmax.com/posts/timing-is-everything/)；HRT：[2025 Summer Projects（Stall Monitor）](https://www.hudsonrivertrading.com/hrtbeat/intern-spotlight-2025-software-engineering-summer-projects/)
- 组件：[Chronicle Queue](https://chronicle.software/products/chronicle-queue)、[Aeron](https://real-logic.co.uk/)、[Databento PCAP](https://databento.com/pcaps)、[Pico/Corvil](https://www.pico.net/products/corvil-analytics/)
- Coordinated Omission：[Gil Tene 的测量方法论讨论](https://dev.to/sergiy_lutsak_hft/measuring-latency-in-net-without-fooling-yourself-stopwatch-percentiles-and-coordinated-omission-2711)
- LLVM XRay：[官方文档](https://llvm.org/docs/XRay.html)、[Google 2016 白皮书](https://static.googleusercontent.com/media/research.google.com/en//pubs/archive/45287.pdf)（函数级插桩的成本与语义参考）
- AWS：[Nitro 侧信道白皮书](https://docs.aws.amazon.com/whitepapers/latest/security-design-of-aws-nitro-system/the-ec2-approach-to-preventing-side-channels.html)、[CPU 选项/关 SMT](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-specify-cpu-options.html)、[Nitro 纳秒硬件包时间戳](https://aws.amazon.com/about-aws/whats-new/2025/06/amazon-time-sync-nanosecond-hardware-packet-timestamps/)、[ENA DPDK PMD](https://doc.dpdk.org/guides/nics/ena.html)、[EC2 HFT 方案](https://aws.amazon.com/cn/blogs/china/building-cefi-high-frequency-trading-system-on-aws/)
