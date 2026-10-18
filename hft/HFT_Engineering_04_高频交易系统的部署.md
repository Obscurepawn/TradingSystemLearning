# HFT Engineering从入门到入土（4）—— 高频交易系统的部署

同一份程序，换台服务器、换个 PCIe 插槽，甚至只是换一组 CPU，延迟都可能变化。

部署 Linux 交易系统时，行情、策略和网关怎么拆，线程放在哪些核上，服务器放在哪里，都会影响一条消息要走多远、经过多少次交接，以及途中会和谁争用资源。从行情接收到订单发出，完整路径如下：

[![交易所行情出口经过接入线路与交换设备，到达本机网卡与接收队列，再依次完成行情处理、策略、风控与订单编码，经过本机发送队列与网卡及接入线路，最终到达交易所订单入口。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-03/text-trading-path.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-03/text-trading-path.png)

这条路径既经过线路和网络设备，也经过进程、线程、内存页和 CPU 缓存。同机的策略与网关若分属两个 CPU socket，通信就要跨 socket；网卡中断会打断所在核上的交易线程，同一物理核上运行的 SMT(超线程) 兄弟线程会与它争用执行资源。

## 目录

- [HFT Engineering从入门到入土（4）—— 高频交易系统的部署](#hft-engineering从入门到入土4-高频交易系统的部署)
  - [目录](#目录)
  - [1. 先确定一个交易单元里有哪些进程](#1-先确定一个交易单元里有哪些进程)
    - [1.1 从完整的交易路径划分职责](#11-从完整的交易路径划分职责)
    - [1.2 什么时候值得拆进程](#12-什么时候值得拆进程)
    - [1.3 优先按独立状态分片](#13-优先按独立状态分片)
  - [2. 交易模块之间怎样通信](#2-交易模块之间怎样通信)
    - [2.1 同一台机器上的不同进程：共享内存 IPC](#21-同一台机器上的不同进程共享内存-ipc)
    - [2.2 不同机器之间：网络通信](#22-不同机器之间网络通信)
    - [2.3 通信方式按边界选择](#23-通信方式按边界选择)
  - [3. 先看硬件拓扑，再分配 CPU](#3-先看硬件拓扑再分配-cpu)
    - [3.1 Socket、NUMA node 和逻辑 CPU 分别表示什么](#31-socketnuma-node-和逻辑-cpu-分别表示什么)
    - [3.2 把网卡、线程和热内存放在一起](#32-把网卡线程和热内存放在一起)
    - [3.3 一种常见的核分配方法](#33-一种常见的核分配方法)
    - [3.4 独占物理核之后，还要测试整机竞争](#34-独占物理核之后还要测试整机竞争)
  - [4. 绑核之后，还要处理哪些干扰](#4-绑核之后还要处理哪些干扰)
    - [4.1 Affinity 只规定线程可以在哪里运行](#41-affinity-只规定线程可以在哪里运行)
    - [4.2 CPU isolation 是几层配置的配合](#42-cpu-isolation-是几层配置的配合)
    - [4.3 减少热路径核心上的中断与检测开销](#43-减少热路径核心上的中断与检测开销)
    - [4.4 电源设置](#44-电源设置)
  - [5. 把内存准备工作移到运行之前](#5-把内存准备工作移到运行之前)
    - [5.1 分配、触页、锁页与预热各有作用](#51-分配触页锁页与预热各有作用)
    - [5.2 开启 Huge Pages，减少TLB Cache压力](#52-开启-huge-pages减少tlb-cache压力)
  - [6. SmartNIC 和 FPGA](#6-smartnic-和-fpga)
    - [6.1 硬件可以替主机完成什么](#61-硬件可以替主机完成什么)
  - [7. 交易所接入与云上部署](#7-交易所接入与云上部署)
    - [7.1 传统交易所：靠近接入点](#71-传统交易所靠近接入点)
    - [7.2 Crypto：公有云上的低延迟部署](#72-crypto公有云上的低延迟部署)
  - [8. 用 PGO 优化上线的二进制](#8-用-pgo-优化上线的二进制)
  - [9. 把性能回退检测纳入发布流程](#9-把性能回退检测纳入发布流程)
  - [参考资料](#参考资料)

<a id="hft04-1"></a>
## 1. 先确定一个交易单元里有哪些进程

<a id="hft04-1-1"></a>
### 1.1 从完整的交易路径划分职责

能够独立接收行情、维护策略状态并提交订单的一组程序，构成一个**交易单元**。它可以服务一组标的、一个市场，或一组相互依赖的策略。哪些模块放在一起，取决于它们共享的状态和交易上的依赖关系。

一个交易单元的常见职责如下，几项职责可以由同一个进程承担：

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

<a id="hft04-1-2"></a>
### 1.2 什么时候值得拆进程

多个策略使用同一份行情时，可以共享行情进程。网关要独立维护会话，或者策略升级时要保留市场连接，就值得把网关拆成独立进程。拆分也能隔离故障，避免策略等模块崩溃时影响全局。

以行情、策略、交易网关三个进程为例：

[![行情 A/B 进入 market-data；market-data 发送行情事件到 strategy；strategy 发送订单意图到 order-gateway；order-gateway 向交易所发单，并将委托与成交回报反向传递给 strategy。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-03/text-process-trading.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-03/text-process-trading.png)

[![market-data 的数据进入独立记录路径；控制面进程向各业务进程发送配置、启停与主备身份；各业务进程向观测进程发送计数器和采样事件。这三组关系相互独立，不表示先后顺序。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-03/text-process-support.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-03/text-process-support.png)

`order-gateway` 维护订单状态和交易所连接，并在进程内完成每笔订单的风控检查。控制面按全账户交易限额分配各交易单元的额度。

<a id="hft04-1-3"></a>
### 1.3 优先按独立状态分片

当一台机器无法承载全部工作时，可以按市场、标的组、策略组或订单会话分片。每个分片尽量拥有自己的订单簿、策略状态和发单额度，让大部分消息在本分片内处理完。以延迟为唯一优化目标时，多台机器重复计算也完全可以接受。

把解码、特征计算、策略和风控逐段分散到多台机器，会让每条行情反复经过网络。只有某个阶段确实需要独立扩展、共享或专用硬件时，才值得这样拆。

按独立状态分片，是让不同事件尽量在各自的分片内完成；把**同一次事件中可以独立进行的计算**交给多个核心，则要等各分支返回后才能合并。后者的处理时间可以近似看成：

```text
串行准备
+ max(主核计算, 请求交接 + 辅助核计算 + 结果交接)
+ 合并结果
```

并行阶段的耗时由最后完成的分支决定，分配计算量时需要把请求和结果的交接成本算进去。例如，假设一次事件需要计算 64 个期权，可以先尝试主核处理 40 个、辅助核处理 24 个。主核多承担一些计算，为辅助核交接请求和结果留出时间，再按两条分支的实际完成时间调整分工。缓存行交接机制见本系列第3篇《C++线程通信与性能测量》第1.6节。

<a id="hft04-2"></a>
## 2. 交易模块之间怎样通信

同一进程内通过函数调用或线程间队列传递数据，同机不同进程可以使用共享内存，跨机则要经过网络。

<a id="hft04-2-1"></a>
### 2.1 同一台机器上的不同进程：共享内存 IPC

行情和策略拆成两个进程后，可以把同一个共享内存对象映射到各自的地址空间，在其中放置消息队列。行情进程写入事件，策略进程读取事件并更新自己的状态。数据留在本机内存中，两个进程可以独立启动、升级和重启。

这类队列常用 SPSC/SPMC RingBuffer 实现：

[![行情进程把消息写入共享内存中的队列，策略进程从同一队列读取消息；共享内存中还维护发布位置和消费位置。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-03/text-shared-memory-ipc.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-03/text-shared-memory-ipc.png)

<a id="hft04-2-2"></a>
### 2.2 不同机器之间：网络通信

行情服务与策略部署在不同机器后，数据要经过两端的网络栈或用户态收发组件、网卡和交换网络：

[![消息在主机 A 从业务线程经过发送缓冲、网络收发组件和网卡，穿过网络后，在主机 B 从网卡经过网络收发组件和接收缓冲，最终交给业务线程。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-03/text-cross-host-path.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-03/text-cross-host-path.png)

这种场景通常使用 UDP，把传输可靠性交给应用层实现。TCP 的协议设计考虑了公平性和拥塞控制：发生拥塞后限制自身的带宽占用，缓解网络拥塞。在网络大多数时候稳定、以最低传输延迟为目标的场景中，我们希望减少这些机制的约束。

开源社区也有牺牲公平性、增加带宽开销来换取低延迟的可靠传输实现，例如基于 UDP 的 [KCP][kcp]。

<a id="hft04-2-3"></a>
### 2.3 通信方式按边界选择

| 边界 | 可采用的方式 | 选择时关注什么 |
|---|---|---|
| 同一线程 | 函数调用、局部状态 | 交接次数最少，代码与状态耦合程度较高 |
| 同进程不同线程 | 有界队列、Disruptor 等 | 缓存行流转、生产消费速率、CPU 核的位置 |
| 同机不同进程 | 共享内存队列、Unix domain socket | 消息布局、生命周期、背压、恢复与维护成本 |
| 跨机点对点可靠流 | TCP | 消息边界、连接恢复、缓冲和排队 |
| 跨机数据报与组播 | UDP + 应用层协议 | 序号、丢包恢复、扇出和速率控制 |
| 跨机特定内存通信 | RDMA | 两端支持、内存注册、队列和恢复协议 |
| 管理与配置 | Unix domain socket、TCP、HTTP/RPC | 接口清晰、鉴权、超时和可观测性 |

Aeron 是一套常用的开源低延迟通信套件，同时提供本机共享内存 IPC 和跨机 UDP 传输，应用通过统一的发布订阅接口使用。[Aeron 简介][aeron-overview]

<a id="hft04-3"></a>
## 3. 先看硬件拓扑，再分配 CPU

<a id="hft04-3-1"></a>
### 3.1 Socket、NUMA node 和逻辑 CPU 分别表示什么

CPU socket 是物理处理器插槽，NUMA node 描述 CPU 与内存的访问局部性，Linux 设置线程 affinity 时使用逻辑 CPU 编号。

同一个物理核启用 SMT（超线程）后，会呈现多个逻辑 CPU。例如，一个核可以保存两套线程上下文，两者共用计算流水线等执行资源。超低延迟系统常通过关闭 SMT 来减少这种竞争。

一个 socket 可以包含多个 NUMA node 和多个 LLC（Last-Level Cache，末级缓存）。安排同 socket 内的线程时，也要看它们是否跨 NUMA、是否共享 LLC，以及网卡连接到哪一侧的 PCIe。[Linux CPU 拓扑][cpu-topology] [DPDK Linux 性能指南][dpdk-performance]

<a id="hft04-3-2"></a>
### 3.2 把网卡、线程和热内存放在一起

让网卡、处理线程和接收缓冲区位于同一 NUMA node，数据路径如下：

[![NIC 通过本地 PCIe 到达本节点的接收内存，本节点上的行情、策略和网关线程使用接收数据，并沿本地发送路径发出。](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-03/text-numa-local-path.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/b6a9497e7492436dc88c323e45b126c82b47dcc9/hft/assets/hft-03/text-numa-local-path.png)

如果网卡连在 socket 0，接收线程和缓冲区却在 socket 1，一部分数据访问就可能经过处理器之间的互联。把它们放在同一节点，可以减少这种绕行。

<a id="hft04-3-3"></a>
### 3.3 一种常见的核分配方法

假设服务器有两个 socket，每个 socket 有八个物理核，每核两个 SMT 线程，并且每个 socket 对应一个 NUMA node。CPU 拓扑如下：

每个单元格列出同一物理核的两个逻辑 CPU：

| socket 0 / node 0 | socket 1 / node 1 |
|---|---|
| CPU 0、16 | CPU 8、24 |
| CPU 1、17 | CPU 9、25 |
| CPU 2、18 | CPU 10、26 |
| CPU 3、19 | CPU 11、27 |
| CPU 4、20 | CPU 12、28 |
| CPU 5、21 | CPU 13、29 |
| CPU 6、22 | CPU 14、30 |
| CPU 7、23 | CPU 15、31 |

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

这套分配为热线程预留整个物理核，同时禁止无关任务和可迁移的中断使用 CPU 18、19、20。也可以通过固件或 Linux 的 `nosmt` 启动参数关闭 SMT，再按关闭后的在线 CPU 编号分配资源。[Linux CPU isolation][cpu-isolation]

把录包线程放到另一个 socket，能减少部分 CPU 竞争；它读取共享日志时，仍会产生跨 socket 流量。如果交易与录包都很繁忙，就在网络侧把流量分给独立记录机，例如使用交换机镜像。

<a id="hft04-3-4"></a>
### 3.4 独占物理核之后，还要测试整机竞争

不同物理核可能共享 LLC、片上互连、内存控制器和内存带宽。录包、监控或另一个策略的工作集变大后，可能挤掉交易线程的缓存行，或让访存请求等待更久。

可以让多个 worker 各自随机访问独立数组，逐步扩大每个 worker 的工作集，观察吞吐怎样变化。即使数组之间没有共享数据，worker 仍会争用缓存容量、互连和内存带宽；工作集扩大到需要频繁访问下一级存储时，竞争就可能加剧。

核分配方案可以按下面三组条件验收。各组使用相同的行情回放、交易二进制和交易核位置，每组重复运行：

| 测试条件 | 要回答的问题 |
|---|---|
| 交易进程单独运行，仅保留必要系统服务 | 当前配置下的独立运行基线是多少？ |
| 同时运行预期的行情、录包、监控与其他策略 | 按预定方案部署在同一台机器上，是否已让延迟分布变差？ |
| 逐步提高某个同机进程的负载或工作集 | 哪类同机进程的负载会先影响交易路径，容量余量有多大？ |

记录处理时间和到达至完成的延迟分布，以及队列积压、丢弃数量。随后比较调整核心位置、减少共享数据访问、拆到其他 NUMA node 或独立机器的效果，并计入跨节点传输成本。

<a id="hft04-4"></a>
## 4. 绑核之后，还要处理哪些干扰

<a id="hft04-4-1"></a>
### 4.1 Affinity 只规定线程可以在哪里运行

Linux 的 CPU affinity 是线程属性，需要为每个已有线程分别设置。新线程继承创建者的掩码，也可以在创建后单独调整。[sched_setaffinity 手册][affinity]

<a id="hft04-4-2"></a>
### 4.2 CPU isolation 是几层配置的配合

| 配置层 | 处理的干扰 | 配套配置 |
|---|---|---|
| 线程 affinity | 线程迁移、执行位置不确定 | 阻止无关任务进入相同 CPU |
| isolated cpuset partition | 普通任务调度与负载均衡 | IRQ、内核线程、定时器另行核对 |
| `nohz_full` | 满足条件时减少周期性调度 tick | 热核上保持合适的可运行任务数量，减少内核往返 |
| RCU callback offload | 把 RCU 回调移出指定 CPU | 为回调执行线程安排 housekeeping CPU |
| IRQ affinity | 设备中断的投递位置 | 同时查看队列、softirq、驱动与 managed IRQ 行为 |
| workqueue 与后台线程配置 | 其他内核及用户态工作 | 留足系统资源并检查实际运行位置 |

`nohz_full` 适合线程长时间停留在用户态、每核只有一个可运行任务的模式。内核会自动把 `nohz_full` CPU 上的 RCU 回调迁走，也可以通过 `rcu_nocbs` 单独指定需要迁走回调的 CPU。[NO_HZ 文档][nohz] [内核启动参数][kernel-parameters]

迁走的工作由 housekeeping CPU 承担。算力不足时，资源回收、网络管理和设备维护会积压，最终拖慢交易进程。

<a id="hft04-4-3"></a>
### 4.3 减少热路径核心上的中断与检测开销

如果交易线程独占一个物理核，始终在用户态忙轮询，并通过 kernel bypass 直接轮询设备队列，就可以减少时间片切换和收包通知带来的干扰。设备 IRQ、定时器和内核检测各有用途，需要分别配置；可迁移的工作交给 housekeeping 核：

| 中断或事件来源 | 在这个场景下怎样处理 | 部署要点 |
|---|---|---|
| **周期性调度 tick** | 在交易核上停止 | 内核启用 `CONFIG_NO_HZ_FULL`，再配置 `nohz_full`，保持只有一个可运行任务，并避免 POSIX CPU 时间定时器等依赖；`CONFIG_NO_HZ_IDLE` 只处理空闲核，对持续忙轮询不够 |
| **管理网口、NVMe、SATA、USB 等设备 IRQ** | 设备还在使用时迁到 housekeeping 核 | 交易核不承担普通 socket、文件写入等 I/O，设备 IRQ 的 affinity 也要避开它 |
| **网卡链路变化、设备移除及其他控制事件** | 保留必要通知，移到 housekeeping 核处理 | 将驱动和通信框架的控制线程安排到 housekeeping 核 |
| **Soft/Hard Lockup watchdog** | 按检测需求决定是否排除热核 | `nohz_full` 核默认不运行这些 watchdog，也可通过 `kernel.watchdog_cpumask` 调整；排除后，该核进入内核时发生的相应锁死可能无法被这些检测器发现。[Lockup watchdogs][lockup-watchdogs] |
| **处理器间中断 IPI** | 检查来源，减少可避免的触发 | IPI 用于调度等内核工作；避免其他任务在热核唤醒，可减少调度 IPI。[每 CPU 内核线程][percpu-kthreads] |

<a id="hft04-4-4"></a>
### 4.4 电源设置

固件和电源设置影响 CPU 从空闲到运行、从短时负载到持续负载的表现。C-state 表示 CPU 的空闲状态，限制较深的 C-state 可以减少唤醒等待，代价是更高的功耗和温度。相关配置见 [CPUIdle][cpuidle] 和 [CPUFreq][cpufreq]。

<a id="hft04-5"></a>
## 5. 把内存准备工作移到运行之前

<a id="hft04-5-1"></a>
### 5.1 分配、触页、锁页与预热各有作用

程序启动后的准备顺序如下：

1. 确定线程 affinity 与 NUMA 分配策略。
2. 确认大页策略或预留页池已经就绪，再分配订单簿、订单池、队列和网络缓冲区。
3. 在目标节点触及将要使用的内存页，包括必要的栈空间。
4. 按资源限制锁定需要常驻的内存，并检查调用结果。
5. 加载配置和模型，建立连接，预热解析、策略与编码路径。
6. 确认行情、订单和风险状态已经恢复，再开放发单。

<a id="hft04-5-2"></a>
### 5.2 开启 Huge Pages，减少TLB Cache压力

大页（Huge Pages）可以用于较大的订单簿、订单池和网络缓冲区，也可用于其他提前预留的内存。

TLB 缓存虚拟地址到物理地址的映射。使用 2 MiB 大页后，一个条目就能覆盖原本需要 512 个 4 KiB 页面映射的地址范围。预触页（Prefault）把首次触页的工作移到初始化阶段，大页则减少运行期间地址映射占用的 TLB 条目。[THP 原理][thp]

<a id="hft04-6"></a>
## 6. SmartNIC 和 FPGA

<a id="hft04-6-1"></a>
### 6.1 硬件可以替主机完成什么

SmartNIC 覆盖的功能很多，选型时先看它承担哪部分工作：

| 目标 | 典型数据路径 | 对部署的影响 |
|---|---|---|
| 缩短主机收发路径 | 网卡 → 用户态缓冲/协议栈 → CPU 程序 | 配置 CPU、PCIe、NUMA 和用户态栈 |
| 在 FPGA 上执行交易相关逻辑 | 报文 → 卡上解析/计算/检查 → 卡上构造输出 | 把部分路径移出 CPU |

ExaNIC 这类 FPGA 网卡可以在数据到达时逐段解析，让接收、字段提取和部分计算重叠。利用这种流式接口，特定设计在所需字段到齐、处理条件满足后，就可以在整帧收齐前启动响应；依赖帧尾字段或完整帧校验的处理则等待后续数据。[Cisco FDK 接口与示例](https://www.cisco.com/c/en/us/td/docs/dcn/nexus3550/fdk/user-guide/cisco-nexus-3550-fdk-user-guide/exanic-fdk.html)、[Cisco SmartNIC 延迟测量说明](https://www.cisco.com/c/en/us/td/docs/dcn/nexus3550/smartnic/sw/user-guide/cisco-nexus-smartnic-user-guide/benchmarking.html)

<a id="hft04-7"></a>
## 7. 交易所接入与云上部署

<a id="hft04-7-1"></a>
### 7.1 传统交易所：靠近接入点

传统交易所通常提供机房托管（Colocation）或邻近托管服务。把服务器放进指定机房，通过机房内交叉连接或专线接入，能缩短行情和订单的网络路径。CME、JPX 都提供这类接入方式。[CME 接入方式][cme-connectivity] [JPX 接入方式][jpx-connectivity]

按行情出口、订单入口和可用线路选择机房，比较线路、交换设备及排队带来的延迟，并安排独立的备用接入路径。

<a id="hft04-7-2"></a>
### 7.2 Crypto：公有云上的低延迟部署

很多 Crypto 交易所的系统或接入网关部署在 AWS 等公有云上。交易程序也往往跟着部署到相应的云区域，缩短到交易所接口的网络路径。云上除了调整主机配置，还要选择实例的位置和网络接入方式。[AWS 加密市场交易部署介绍][aws-crypto]

以 AWS 为例：

| 部署项 | 具体做法 |
|---|---|
| **选择 region 和可用区（AZ）** | 先查交易接口的官方接入位置，再从候选 region、AZ 测量行情接收和订单响应。跨账户核对位置用 AZ ID：不同账户中的 `us-east-1a` 可能对应不同物理可用区。[AZ ID][aws-az-id] |
| **选择实例规格** | 持续忙轮询的热线程优先使用非突发性能实例；突发性能实例需考虑 CPU credit 耗尽后的限速或额外费用。除了 vCPU 数量与标称带宽，选型还要看单核性能、每秒包数（PPS）、网络基线和突发额度。[EC2 突发性能实例][aws-burstable] [网络带宽与额度][aws-network-bandwidth] |
| **安排内部热路径** | 交互频繁的模块优先放在同一实例或同一 AZ。必须跨实例时，使用 cluster placement group，让实例在同一 AZ 内靠近放置，降低组内通信延迟。[Placement groups][aws-placement] |
| **选择交易所接入方式** | 交易所提供 PrivateLink 时，比较它与公网入口的延迟和稳定性，再决定使用哪种；Coinbase Derivatives 就提供这种接入。要让交易实例与交易所网关共同放置，需要交易所配合共享 placement group。[Coinbase Derivatives 接入][coinbase-connectivity] [共享 placement group][aws-crypto] |
| **配置实例网络** | 使用支持 ENA 增强型网络的实例和驱动，减少虚拟网络开销。内部 EC2 通信可以试用 ENA Express，前提是两端都支持并启用。低负载时中位延迟可能增加，要同时比较 p50 与尾延迟。[ENA][aws-ena] [ENA Express][aws-ena-express] |
| **准备实例内资源** | 按实例暴露的 CPU 拓扑设置线程 affinity，把日志、监控和系统工作移出交易 vCPU。预分配、触页、锁页和大页准备仍按前面的步骤完成。 |

<a id="hft04-8"></a>
## 8. 用 PGO 优化上线的二进制

部署还包括选择上线的二进制。PGO（Profile-Guided Optimization，剖面引导优化）先收集程序在代表性负载下的运行剖面，再用这些数据指导重新编译。编译器根据函数调用次数、分支走向和执行频率识别热点，调整函数内联、基本块布局等，让生成的机器码更适合实际交易路径。Clang 和 GCC 都支持这种优化。[Clang PGO][clang-pgo] [GCC PGO][gcc-pgo]

Jane Street 内部 OCaml 的 FDO（Feedback-Directed Optimization，反馈引导优化）项目通过采集运行剖面，调整函数和基本块的排列，早期在多个重要程序上取得了约 10%、15%、20% 的运行时间改善。FDO 还消除了这些程序中无关库变更引起的代码布局和性能波动。

PGO 可能对训练负载过拟合。把它纳入部署流程时，将生成 profile 的行情与验收行情分开，用不同交易日、不同节奏的行情检查性能收益是否稳定。

<a id="hft04-9"></a>
## 9. 把性能回退检测纳入发布流程

程序上线后，代码、工作负载和运行环境还会持续变化。在发布验收和运行监控中保留以下记录，才能在性能变差时分清原因：

| 记录内容 | 需要保留什么 |
|---|---|
| 程序与构建 | 代码版本、编译器、优化与链接选项，使用 PGO 时的 profile 版本 |
| 输入与节奏 | 回放数据版本、标的和更新分布、到达速率，以及稳定输入或突发输入 |
| 硬件与部署 | CPU 型号、Microcode、频率策略、核与 NUMA 布局、内存配置、同机进程及其负载 |
| 测量与统计 | 计时起止位置、时钟与插桩方式、统计时间窗口、样本量、分位数、直方图范围与溢出数量 |
| 结果与目标 | 绝对预算、可比较的历史基线、延迟分布、吞吐、积压和丢弃数量 |

<a id="hft04-references"></a>
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
- **量化机构的 PGO 实践与评价**：[Jane Street 编译器优化访谈][janestreet-fdo]、[G-Research PGO 分享][gresearch-pgo]、[Optiver 工程师演讲稿][optiver-pgo]。
- **整机竞争与性能回退**：[When Nanoseconds Matter 官方讲义][when-nanoseconds-slides]、[The Latency Slippage 官方讲义][latency-slippage-slides]、[The Latency Slippage 作者实验源码](https://github.com/0xsanchit/CacheLatency/blob/5ea23c07d524a057179e6db52b8038131927ffd1/src/exp/10g_options_clwb.cpp)。

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
[janestreet-fdo]: https://signalsandthreads.com/compiler-optimization/
[gresearch-pgo]: https://www.gresearch.com/news/how-profile-guided-optimisation-makes-your-code-faster/
[optiver-pgo]: https://meetingcpp.com/files/mcpp/2016/Carl%20Cook%20-%20The%20Speed%20Game-%20Automated%20trading%20in%20C%2B%2B.pdf#page=71
[when-nanoseconds-slides]: https://github.com/CppCon/CppCon2024/blob/main/Presentations/When_Nanoseconds_Matter.pdf
[latency-slippage-slides]: https://github.com/CppCon/CppCon2026/blob/6f687701cdb4099eae39011485065b52ab7da0e9/Presentations/The_Latency_Slippage.pdf
