# HFT Engineering从入门到入土（3）—— 高频交易系统的部署

上一篇讨论进程内的计算、访存和线程间通信。这一篇接着讨论 Linux 交易系统的部署：行情、策略和网关怎么拆，线程放在哪些核上，服务器又该放在哪里。

同一份程序，换台服务器、换个 PCIe 插槽，甚至只是换一组 CPU，延迟都可能变化。即使服务器已经搬到交易所附近，主机内部的数据路径仍然值得优化。

部署决定了一条消息要走多远、经过多少次交接，以及途中会和谁争用资源。对于行情驱动的交易系统，先看从行情接收到订单发出的完整路径：

```text
交易所行情出口
  → 接入线路与交换设备
  → 本机网卡与接收队列
  → 行情处理 → 策略 → 风控与订单编码
  → 本机发送队列与网卡
  → 接入线路
  → 交易所订单入口
```

这条路径既经过线路和网络设备，也经过进程、线程、内存页和 CPU 缓存。策略与网关放在同一台机器上，通信仍可能跨 CPU socket；交易线程绑了核，仍可能受到网卡中断的打扰，或与同一物理核上的 SMT 兄弟线程争用执行资源。

## 1. 先确定一个交易单元里有哪些进程

### 1.1 从完整的交易路径划分职责

把能够独立接收行情、维护策略状态并提交订单的一组程序称为一个**交易单元**。它可以服务一组标的、一个市场，或一组相互依赖的策略。哪些模块放在一起，取决于它们共享的状态和交易上的依赖关系。

一个交易单元通常需要完成以下工作，但这些职责不必各自对应一个进程：

| 职责 | 需要维护的主要状态 | 与部署有关的问题 |
|---|---|---|
| 行情接收与恢复 | 行情序列号、A/B 路去重状态、快照与增量衔接 | 哪个线程拥有接收队列；丢包恢复是否阻塞正常行情 |
| 订单簿与策略 | 本地订单簿、特征、报价状态 | 哪些状态必须由同一个线程更新；多个策略如何共享行情 |
| 发单前风控与交易网关 | 风险额度、订单编号、连接与会话序列号 | 谁最终批准发单；谁拥有交易所连接 |
| 私有回报处理 | 委托、成交、撤单与持仓状态 | 回报如何回到策略；重连后如何补齐状态 |
| 记录与观测 | 原始报文、业务事件、延迟样本、故障信息 | 磁盘或监控变慢时，会不会拖住交易 |
| 管理与恢复 | 配置版本、启动代次、主备身份、发布状态 | 谁能启停交易；如何阻止两个实例同时发单 |

热路径的进程数量取决于职责怎么组合：

| 布局 | 热路径进程数 | 主要收益 | 付出的代价 |
|---|---:|---|---|
| 行情、订单簿、策略、风控、网关集成 | 1 | 可以在一个事件循环里连续处理，减少交接 | 故障和发布范围较大；多个策略容易互相影响 |
| 共享行情进程 + 集成策略与交易进程 | 2 | 多个交易单元可复用行情接入；交易状态较集中 | 行情进入策略前增加一次 IPC |
| 行情进程 + 策略进程 + 风控/网关进程 | 3 | 行情、策略、交易连接有清晰的故障与发布边界 | 行情到发单至少经过两次 IPC；回报还要反向传递 |
| 多策略共享行情和多个交易网关 | 随分片增加 | 可以按标的、市场、账户与会话扩展 | 排队、共享状态和风险额度协调更复杂 |

### 1.2 什么时候值得拆进程

多个策略需要同一份行情时，可以共享行情进程；网关要独立维护会话，或者策略升级时不希望重启市场连接，也值得拆成独立进程。拆分还能隔离故障，避免某个模块（例如策略进程）崩溃时影响全局。

以行情、策略、交易网关三个进程为例：

```text
行情 A/B ──→ market-data ──行情事件──→ strategy ──订单意图──→ order-gateway
                │                       ↑                       │   │
                │                       └────委托/成交回报────────┘   └──→ 交易所
                │
                └──────────→ 独立记录路径

控制面进程 ──配置、启停、主备身份──→ 各业务进程
观测进程 ←─计数器、采样事件───── 各业务进程
```

这里的 `order-gateway` 负责风控检查，维护订单状态和交易所连接。每笔订单在网关进程内完成风控；控制面按全账户交易限额分配各交易单元的额度。

### 1.3 优先按独立状态分片

当一台机器装不下全部工作时，可以按市场、标的组、策略组或订单会话分片。每个分片尽量拥有自己的订单簿、策略状态和发单额度，让大部分消息在本分片内处理完。既然这里把延迟作为唯一优化目标，多台机器重复计算也完全可以接受。

把解码、特征计算、策略和风控逐段分散到多台机器，会让每条行情反复经过网络。只有某个阶段确实需要独立扩展、共享或专用硬件时，才值得这样拆。

## 2. 交易模块之间怎样通信

同一进程内通过函数调用或线程间队列传递数据，同机不同进程可以使用共享内存，跨机则要经过网络。上一篇已经讨论了进程内通信，这里接着看进程拆开后，消息怎么传。

### 2.1 同一台机器上的不同进程：共享内存 IPC

把行情和策略拆成两个进程后，可以让它们把同一个共享内存对象映射到各自的地址空间，在其中放置消息队列。行情进程写入事件，策略进程读取并更新自己的状态。数据留在本机内存中，两个进程可以独立启动、升级和重启。

这类队列常用 SPSC/SPMC RingBuffer 实现：

```text
行情进程                        策略进程
  写入消息                        读取消息
     │                              ↑
     └────── 共享内存中的队列 ────────┘
             发布位置、消费位置
```

### 2.2 不同机器之间：网络通信

行情服务与策略部署在不同机器后，数据要经过两端的网络栈或用户态收发组件、网卡和交换网络：

```text
主机 A：业务线程 → 发送缓冲 → 网络收发组件 → 网卡
                                                │
                                              网络
                                                │
主机 B：业务线程 ← 接收缓冲 ← 网络收发组件 ← 网卡
```

这种场景通常使用 UDP，传输可靠性交给应用层实现。TCP 在协议设计中考虑了公平性和拥塞控制：发生拥塞后要让网络恢复，也要限制自身的带宽占用。这里讨论的低延迟场景假设网络大多数时候稳定，我们更希望以最低延迟完成传输，不希望受到这些机制的约束。

开源社区也有牺牲公平性、增加带宽开销来换取低延迟的可靠传输实现，例如基于 UDP 的 [KCP][kcp]。

### 2.3 通信方式按边界选择

| 边界 | 可采用的方式 | 选择时关注什么 |
|---|---|---|
| 同一线程 | 函数调用、局部状态 | 最少的交接，代码与状态耦合程度较高 |
| 同进程不同线程 | 有界队列、Disruptor 等 | 缓存行流转、生产消费速率、CPU 核的位置 |
| 同机不同进程 | 共享内存队列、Unix domain socket | 消息布局、生命周期、背压、恢复与维护成本 |
| 跨机点对点可靠流 | TCP | 消息边界、连接恢复、缓冲和排队 |
| 跨机数据报与组播 | UDP + 应用层协议 | 序号、丢包恢复、扇出和速率控制 |
| 跨机特定内存通信 | RDMA | 两端支持、内存注册、队列和恢复协议 |
| 管理与配置 | Unix domain socket、TCP、HTTP/RPC | 接口清晰、鉴权、超时和可观测性 |

Aeron 是一套常用的开源低延迟通信套件，同时提供本机共享内存 IPC 和跨机 UDP 传输，应用通过统一的发布订阅接口使用。后续章节会具体介绍它的实现与用法。[Aeron 简介][aeron-overview]

## 3. 先看硬件拓扑，再分配 CPU

### 3.1 Socket、NUMA node 和逻辑 CPU 分别表示什么

CPU socket 是物理处理器插槽，NUMA node 描述 CPU 与内存的访问局部性，Linux 的 CPU 编号通常指逻辑 CPU。

同一个物理核启用 SMT（超线程）后，会呈现多个逻辑 CPU。例如，一个核可以保存两套线程上下文，但两者共用计算流水线等执行资源。一般来说，超低延迟系统会关闭 SMT。

一个 socket 可以包含多个 NUMA node，也可能有多个 LLC（Last-Level Cache，末级缓存）。所以，同 socket 内也要检查线程是否跨 NUMA、是否共享 LLC，以及网卡连接到哪一侧的 PCIe。[Linux CPU 拓扑][cpu-topology] [DPDK Linux 性能指南][dpdk-performance]

### 3.2 把网卡、线程和热内存放在一起

让网卡、处理线程和接收缓冲区位于同一 NUMA node，数据路径如下：

```text
NIC ──本地 PCIe──→ 本节点的接收内存
                         │
               本节点上的行情/策略/网关线程
                         │
                    本地发送路径
```

如果网卡连在 socket 0，接收线程和缓冲区却在 socket 1，一部分数据访问就可能经过处理器之间的互联。低延迟部署应避免这种绕行。

### 3.3 一种常见的核分配方法

假设服务器有两个 socket，每个 socket 有八个物理核，每核两个 SMT 线程，并且每个 socket 对应一个 NUMA node。CPU 拓扑如下：

```text
socket 0 / node 0：物理核的逻辑 CPU 对为 (0,16)、(1,17)、…、(7,23)
socket 1 / node 1：物理核的逻辑 CPU 对为 (8,24)、(9,25)、…、(15,31)
```

两张交易网卡连接 node 0，可以这样安排：

| 资源 | 放置位置 | 说明 |
|---|---|---|
| 系统、管理线程、必要内核工作 | CPU 0、1、16、17 | 保留 housekeeping 资源，处理系统杂务与允许的 IRQ |
| 行情热线程 | CPU 2 | 独占物理核；兄弟 CPU 18 留空并从普通任务池排除 |
| 策略热线程 | CPU 3 | 兄弟 CPU 19 同样留空并剔除 |
| 网关热线程 | CPU 4 | 兄弟 CPU 20 同样留空并剔除 |
| 其余应用辅助线程 | CPU 5、6、7 的辅助资源池 | 根据实际线程与空闲策略安排，避免落到热核 |
| 订单簿、队列、接收缓冲区 | node 0 内存 | 启动时分配、触页并验收 |
| 其余非关键线程，例如录包 | node 1 或独立记录机 | 控制跨 socket 读取交易内存的频率 |

这里预留的是物理资源，所以 CPU 18、19、20 没有运行交易线程，也不能被普通任务随意占用，需要在固件中关闭超线程。

把录包线程放到另一个 socket，能减少部分 CPU 竞争，但它读取共享日志时仍会产生跨 socket 流量。如果交易与录包都很繁忙，就在网络侧把流量分给独立记录机，例如使用交换机镜像。

## 4. 绑核之后，还要处理哪些干扰

### 4.1 Affinity 只规定线程可以在哪里运行

Linux 的 CPU affinity 是线程属性。设置主线程的掩码后，要逐一检查其他已有线程的 affinity。新线程还会继承创建者的掩码，应用可以在线程创建后分别设置。[sched_setaffinity 手册][affinity]

### 4.2 CPU isolation 是几层配置的配合

| 配置层 | 处理的干扰 | 仍需配合的工作 |
|---|---|---|
| 线程 affinity | 线程迁移、执行位置不确定 | 阻止无关任务进入相同 CPU |
| isolated cpuset partition | 普通任务调度与负载均衡 | IRQ、内核线程、定时器另行核对 |
| `nohz_full` | 满足条件时减少周期性调度 tick | 热核上保持合适的可运行任务数量，减少内核往返 |
| RCU callback offload | 把 RCU 回调移出指定 CPU | 为回调执行线程安排 housekeeping CPU |
| IRQ affinity | 设备中断的投递位置 | 同时查看队列、softirq、驱动与 managed IRQ 行为 |
| workqueue 与后台线程配置 | 其他内核及用户态工作 | 留足系统资源并检查实际运行位置 |

`nohz_full` 适合线程长时间停留在用户态、每核只有一个可运行任务的模式。系统调用、设备中断等干扰仍然存在。内核会自动把 `nohz_full` CPU 上的 RCU 回调迁走，也可以通过 `rcu_nocbs` 单独指定需要迁走回调的 CPU。[NO_HZ 文档][nohz] [内核启动参数][kernel-parameters]

迁走的工作由 housekeeping CPU 承担。它们也要留足算力，否则资源回收、网络管理和设备维护会积压，最终拖慢交易进程。

### 4.3 关键热路径核心屏蔽无用中断

假设交易线程独占一个物理核，始终在用户态忙轮询，网络收发通过 kernel bypass 直接轮询设备队列。这个核不用按时间片切换任务，也不用等待网卡通知才开始收包。可以停用相应的中断来源，把其余系统工作留给 housekeeping 核：

| 中断或事件来源 | 在这个场景下怎样处理 | 部署要点 |
|---|---|---|
| **周期性调度 tick** | 在交易核上停止 | 内核启用 `CONFIG_NO_HZ_FULL`，再配置 `nohz_full`，保持只有一个可运行任务，并避免 POSIX CPU 时间定时器等依赖；`CONFIG_NO_HZ_IDLE` 只处理空闲核，对持续忙轮询不够 |
| **管理网口、NVMe、SATA、USB 等设备 IRQ** | 设备还在使用时迁到 housekeeping 核 | 交易核不承担普通 socket、文件写入等 I/O，设备 IRQ 的 affinity 也要避开它 |
| **网卡链路变化、设备移除及其他控制事件** | 保留必要通知，移到 housekeeping 核处理 | 数据队列轮询不代表控制面也没有中断；驱动和通信框架的控制线程需要单独安排 |
| **IPI、Soft Lockup 检测、NMI 等 watchdog 事件** | 不需要处理 | NMI Watchdog、Timer 软死锁检测、Buddy IPI 检测都是卡死状态检测，在程序完全正确的情况下，这几个检测没有任何意义 |

### 4.4 电源设置

固件和电源设置影响 CPU 从空闲到运行、从短时负载到持续负载的表现。这里主要关注 C-state，也就是 CPU 的空闲状态。限制较深的 C-state 可以减少唤醒等待，代价是更高的功耗和温度。相关配置见 [CPUIdle][cpuidle] 和 [CPUFreq][cpufreq]。

## 5. 把内存准备工作移到运行之前

### 5.1 分配、触页、锁页与预热各有作用

程序启动后，先完成这些准备，再开放交易：

1. 确定线程 affinity 与 NUMA 分配策略。
2. 确认大页策略或预留页池已经就绪，再分配订单簿、订单池、队列和网络缓冲区。
3. 在目标节点触及将要使用的内存页，包括必要的栈空间。
4. 按资源限制锁定需要常驻的内存，并检查调用结果。
5. 加载配置和模型，建立连接，预热解析、策略与编码路径。
6. 确认行情、订单和风险状态已经恢复，再开放发单。

### 5.2 开启 Huge Pages，减少TLB Cache压力

大页（Huge Pages）可以用于较大的订单簿、订单池和网络缓冲区，也可用于其他提前预留的内存。

TLB 缓存虚拟地址到物理地址的映射。使用 2 MiB 大页后，一个条目就能覆盖原本需要 512 个 4 KiB 页面映射的地址范围，减轻 TLB 容量压力。Prefault 把首次触页的工作移到初始化阶段，但运行期间 TLB 条目仍会被替换，所以预触页和大页可以配合使用。[THP 原理][thp]

## 6. SmartNIC 和 FPGA

### 6.1 硬件可以替主机完成什么

SmartNIC 覆盖的功能很多，选型时先看它承担哪部分工作：

| 目标 | 典型数据路径 | 对部署的影响 |
|---|---|---|
| 缩短主机收发路径 | 网卡 → 用户态缓冲/协议栈 → CPU 程序 | 仍需处理 CPU、PCIe、NUMA 和用户态栈配置 |
| 在 FPGA 上执行交易相关逻辑 | 报文 → 卡上解析/计算/检查 → 卡上构造输出 | 把部分路径移出 CPU |

ExaNIC 这类网卡还支持以pipeline方式处理网络数据流，这样能做到不需要完整的以太网Frame就可以解析出足以做出交易决策的信息从而直接发出交易，能将单次交易决策延迟压低到小于理论上的first byte到last byte的光电信号转换时间。

## 7. 交易所接入与云上部署

### 7.1 传统交易所：靠近接入点

传统交易所通常提供机房托管（Colocation）或邻近托管服务。把服务器放进指定机房，通过机房内交叉连接或专线接入，能缩短行情和订单的网络路径。CME、JPX 都提供这类接入方式。[CME 接入方式][cme-connectivity] [JPX 接入方式][jpx-connectivity]

先确认行情出口、订单入口和可用线路，再选机房与接入服务。即使同城，线路、交换设备和排队情况不同，延迟也会不同。主线路之外，还要安排独立的备用接入路径。

### 7.2 Crypto：公有云上的低延迟部署

Crypto 交易所的系统或接入网关，很多部署在 AWS 等公有云上。交易程序也往往跟着部署到相应的云区域，缩短到交易所接口的网络路径。云上除了调整主机配置，还要选好实例的位置和网络接入方式。[AWS 加密市场交易部署介绍][aws-crypto]

以 AWS 为例：

| 部署项 | 具体做法 |
|---|---|
| **选择 region 和可用区（AZ）** | 先查交易接口的官方接入位置，再从候选 region、AZ 测量行情接收和订单响应。跨账户核对位置用 AZ ID：不同账户中的 `us-east-1a` 可能对应不同物理可用区。[AZ ID][aws-az-id] |
| **选择实例规格** | 持续忙轮询的热线程优先使用非突发性能实例；突发性能实例需考虑 CPU credit 耗尽后的限速或额外费用。除了 vCPU 数量与标称带宽，选型还要看单核性能、每秒包数（PPS）、网络基线和突发额度。[EC2 突发性能实例][aws-burstable] [网络带宽与额度][aws-network-bandwidth] |
| **安排内部热路径** | 交互频繁的模块优先放在同一实例或同一 AZ。必须跨实例时，使用 cluster placement group，让实例在同一 AZ 内靠近放置，降低组内通信延迟。[Placement groups][aws-placement] |
| **选择交易所接入方式** | 交易所提供 PrivateLink 时，比较它与公网入口的延迟和稳定性，再决定使用哪种；Coinbase Derivatives 就提供这种接入。要让交易实例与交易所网关共同放置，需要交易所配合共享 placement group。[Coinbase Derivatives 接入][coinbase-connectivity] [共享 placement group][aws-crypto] |
| **配置实例网络** | 使用支持 ENA 增强型网络的实例和驱动，减少虚拟网络开销。内部 EC2 通信可以试用 ENA Express，前提是两端都支持并启用。低负载时中位延迟可能增加，要同时比较 p50 与尾延迟。[ENA][aws-ena] [ENA Express][aws-ena-express] |
| **准备实例内资源** | 按实例暴露的 CPU 拓扑设置线程 affinity，把日志、监控和系统工作移出交易 vCPU。预分配、触页、锁页和大页准备仍按前面的步骤完成。 |

## 8. 用 PGO 优化上线的二进制

部署还包括决定上线哪一个二进制。PGO（Profile-Guided Optimization，剖面引导优化）先收集程序在代表性负载下的运行剖面，再用这些数据指导重新编译。编译器根据函数调用次数、分支走向和执行频率识别热点，调整函数内联、基本块布局等，让生成的机器码更适合实际交易路径。Clang 和 GCC 都支持这种优化。[Clang PGO][clang-pgo] [GCC PGO][gcc-pgo]

## 参考资料

- **进程与跨机通信**：[Linux POSIX 共享内存][shm]、[TCP 规范][tcp]、[UDP 使用指南][udp-guidelines]、[KCP][kcp]、[Aeron 简介][aeron-overview]。
- **CPU 拓扑与 NUMA 部署**：[Linux CPU 拓扑][cpu-topology]、[DPDK 硬件布局指南][dpdk-performance]、[NUMA 内存策略][numa-policy]、[线程 affinity][affinity]。
- **CPU 隔离与中断**：[CPU isolation][cpu-isolation]、[cgroup v2][cgroup-v2]、[NO_HZ][nohz]、[内核启动参数][kernel-parameters]、[IRQ affinity][irq-affinity]、[每 CPU 内核线程][percpu-kthreads]、[Lockup watchdogs][lockup-watchdogs]。
- **电源与内存准备**：[CPUIdle][cpuidle]、[CPUFreq][cpufreq]、[mlock][mlock]、[HugeTLB][hugetlb]、[THP][thp]。
- **用户态收发与 FPGA 网卡**：[DPDK PMD][dpdk-pmd]、[AMD Alveo X3][amd-x3]。
- **交易所接入**：[CME 接入方式][cme-connectivity]、[JPX 接入方式][jpx-connectivity]、[Coinbase Derivatives 接入][coinbase-connectivity]。
- **云上位置与实例放置**：[AWS 加密市场与共享 placement group][aws-crypto]、[AZ ID][aws-az-id]、[EC2 placement groups][aws-placement]。
- **云上实例与网络**：[EC2 突发性能实例][aws-burstable]、[网络带宽与额度][aws-network-bandwidth]、[ENA 增强型网络][aws-ena]、[ENA Express][aws-ena-express]。
- **PGO 与构建发布**：[Clang PGO][clang-pgo]、[GCC PGO][gcc-pgo]、[llvm-profdata][llvm-profdata]。

[shm]: https://man7.org/linux/man-pages/man7/shm_overview.7.html
[tcp]: https://www.rfc-editor.org/rfc/rfc9293.html
[udp-guidelines]: https://www.rfc-editor.org/rfc/rfc8085.html
[kcp]: https://github.com/skywind3000/kcp
[aeron-overview]: https://aeron.io/docs/aeron/overview/
[cpu-topology]: https://docs.kernel.org/arch/x86/topology.html
[dpdk-performance]: https://doc.dpdk.org/guides/linux_gsg/nic_perf_intel_platform.html
[numa-policy]: https://docs.kernel.org/admin-guide/mm/numa_memory_policy.html
[affinity]: https://man7.org/linux/man-pages/man2/sched_setaffinity.2.html
[cpu-isolation]: https://docs.kernel.org/admin-guide/cpu-isolation.html
[cgroup-v2]: https://docs.kernel.org/admin-guide/cgroup-v2.html
[nohz]: https://docs.kernel.org/timers/no_hz.html
[kernel-parameters]: https://docs.kernel.org/admin-guide/kernel-parameters.html
[irq-affinity]: https://docs.kernel.org/core-api/irq/irq-affinity.html
[percpu-kthreads]: https://docs.kernel.org/admin-guide/kernel-per-CPU-kthreads.html
[lockup-watchdogs]: https://docs.kernel.org/admin-guide/lockup-watchdogs.html
[cpuidle]: https://docs.kernel.org/admin-guide/pm/cpuidle.html
[cpufreq]: https://docs.kernel.org/admin-guide/pm/cpufreq.html
[mlock]: https://man7.org/linux/man-pages/man2/mlock.2.html
[hugetlb]: https://docs.kernel.org/admin-guide/mm/hugetlbpage.html
[thp]: https://docs.kernel.org/admin-guide/mm/transhuge.html
[dpdk-pmd]: https://doc.dpdk.org/guides/prog_guide/ethdev/ethdev.html
[amd-x3]: https://www.amd.com/content/dam/amd/en/documents/products/accelerators/alveo/x3/2656036-alveo-x3-product-brief.pdf
[cme-connectivity]: https://www.cmegroup.com/solutions/market-access/globex/connectivity-options.html
[jpx-connectivity]: https://www.jpx.co.jp/english/systems/connectivity/01.html
[coinbase-connectivity]: https://docs.cdp.coinbase.com/derivatives/introduction/connectivity
[aws-crypto]: https://aws.amazon.com/blogs/industries/crypto-market-making-latency-and-amazon-ec2-shared-placement-groups/
[aws-az-id]: https://docs.aws.amazon.com/ram/latest/userguide/working-with-az-ids.html
[aws-placement]: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/placement-groups.html
[aws-burstable]: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-performance-instances.html
[aws-network-bandwidth]: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-network-bandwidth.html
[aws-ena]: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/enhanced-networking.html
[aws-ena-express]: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ena-express.html
[clang-pgo]: https://clang.llvm.org/docs/UsersManual.html#profile-guided-optimization
[gcc-pgo]: https://gcc.gnu.org/onlinedocs/gcc/Optimize-Options.html#index-fprofile-use
[llvm-profdata]: https://llvm.org/docs/CommandGuide/llvm-profdata.html#merge
