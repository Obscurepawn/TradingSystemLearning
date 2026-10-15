# HFT Engineering从入门到入土（3）—— 高频交易系统的部署

上一篇主要讨论进程内的计算、访存和线程间通信。

这一篇把视角移到机器和机房：同一份程序，换一台服务器、一个 PCIe 插槽，或者一组 CPU，对延迟的影响有多少？行情、策略和交易网关应该放在一个进程里，还是拆开部署？服务器搬到交易所附近之后，还有哪些地方值得继续优化？

部署方式决定了一条消息的传递需要走多远、经过多少次交接，以及途中会和谁争用资源。讨论 HFT deployment，可以沿着这条路径展开：

```text
交易所行情出口
  → 接入线路与交换设备
  → 本机网卡与接收队列
  → 行情处理 → 策略 → 风控与订单编码
  → 本机发送队列与网卡
  → 接入线路
  → 交易所订单入口
```

这里既有地理距离和网络设备，也有进程、线程、内存页和 CPU 缓存。优化时要把它们连起来看：策略线程与交易网关虽然部署在同一台机器上，通信仍可能跨 CPU socket；进程虽然绑了核，网卡中断和它的 SMT 兄弟线程仍可能抢占同一组硬件资源。

本文以 Linux 上的行情驱动交易系统为主，先给出进程布局，再讨论主机、网络、机房和运维。文中的核号和进程数是一套可用于设计与实验的示例。公开资料核对截至 **2026 年 10 月 15 日**；交易所接入产品、硬件能力和软件配置分别引用对应的一手资料。

**阅读导航**

1. [进程划分](#1-先确定一个交易单元里有哪些进程)
2. [共享内存与 Aeron](#2-进程之间怎样通信)
3. [CPU、NUMA 与网卡布局](#3-先看硬件拓扑再分配-cpu)
4. [绑核、中断与 CPU isolation](#4-绑核之后还要处理哪些干扰)
5. [内存准备与资源竞争](#5-把内存准备工作移到开盘之前)
6. [网络队列与收发 API](#6-网络部署要细到队列和收发-api)
7. [低延迟网卡、SmartNIC 与 FPGA](#7-低延迟网卡smartnic-和-fpga-怎样放进系统)
8. [机房、线路与交换设备](#8-从机房位置到交换设备)
9. [时间同步与观测](#9-把时间同步和观测也作为部署的一部分)
10. [主备与状态恢复](#10-主备切换围绕出单权限和状态恢复设计)
11. [裸机、容器与发布](#11-裸机容器和发布流程)
12. [部署示例与验收](#12-把方案收敛成可以验收的部署)

## 1. 先确定一个交易单元里有哪些进程

### 1.1 从完整的交易路径划分职责

这里把能够独立接收行情、维护策略状态并提交订单的一组程序称为一个**交易单元**。它可以服务一组标的、一个市场或一组相互依赖的策略，边界由状态和交易关系决定。

一个交易单元通常需要完成以下工作，但这些职责不必各自对应一个进程：

| 职责 | 需要维护的主要状态 | 与部署有关的问题 |
|---|---|---|
| 行情接收与恢复 | 行情序列号、A/B 路去重状态、快照与增量衔接 | 哪个线程拥有接收队列；丢包恢复是否阻塞正常行情 |
| 订单簿与策略 | 本地订单簿、特征、报价状态 | 哪些状态必须由同一个线程更新；多个策略如何共享行情 |
| 发单前风控与交易网关 | 风险额度、订单编号、连接与会话序列号 | 谁最终批准发单；谁拥有交易所连接 |
| 私有回报处理 | 委托、成交、撤单与持仓状态 | 回报如何回到策略；重连后如何补齐状态 |
| 记录与观测 | 原始报文、业务事件、延迟样本、故障信息 | 磁盘或监控变慢时，会不会拖住交易 |
| 管理与恢复 | 配置版本、启动代次、主备身份、发布状态 | 谁能启停交易；如何阻止两个实例同时发单 |

“一般有多少个进程”可以先落到几个具体方案上：

| 布局 | 热路径进程数 | 主要收益 | 付出的代价 |
|---|---:|---|---|
| 行情、订单簿、策略、风控、网关集成 | 1 | 可以在一个事件循环里连续处理，减少交接 | 故障和发布范围较大；多个策略容易互相影响 |
| 共享行情进程 + 集成策略与交易进程 | 2 | 多个交易单元可复用行情接入；交易状态较集中 | 行情进入策略前增加一次 IPC |
| 行情进程 + 策略进程 + 风控/网关进程 | 3 | 行情、策略、交易连接有清晰的故障与发布边界 | 行情到发单至少经过两次 IPC；回报还要反向传递 |
| 多策略共享行情和多个交易网关 | 随分片增加 | 可以按标的、市场、账户与会话扩展 | 排队、共享状态和风险额度协调更复杂 |

这几个数字描述的是**一个交易单元的热路径**。外置 Aeron Media Driver、记录器、监控代理、管理进程和备用实例需要另外计数。直接把服务器上的全部进程相加，不能反映一笔订单经过了多少个进程。

进程数也不等于核数。一个进程可能有多个忙轮询线程；一个 Aeron 客户端还可能带有后台 conductor 线程。实际部署清单应精确到线程，列出它的职责、CPU、内存节点、队列和空闲策略。

### 1.2 什么时候值得拆进程

如果同一个线程已经能够处理峰值行情，顺序执行“解码、更新订单簿、策略、风控、编码”是很好的延迟基线。拆进程的理由通常来自共享和隔离：多个策略需要同一份行情，网关需要独立维护会话，或者策略升级不应重启全部市场连接。

每增加一次交接，就要支付发布消息、传递缓存行、等待消费者和读取消息的成本。共享内存省去了某些内核路径，但消费者忙不过来时，消息仍要排队。拆分后是否更快，要看隔离带来的收益是否超过交接成本。

以下采用三个热路径进程，便于展开部署细节：

```text
行情 A/B ──→ market-data ──行情事件──→ strategy ──订单意图──→ order-gateway
                │                       ↑                       │   │
                │                       └────委托/成交回报────────┘   └──→ 交易所
                │
                └──────────→ 独立记录路径

管理进程 ──配置、启停、主备身份──→ 各业务进程
观测进程 ←─计数器、采样事件───── 各业务进程
```

在这个例子中，`order-gateway` 同时拥有发单前检查、订单状态和交易所连接。把风控放在进程内，可以让每笔订单在同一条执行路径上完成检查。全账户限额则由管理侧分配额度，网关在分配到的额度内执行；如何防止多个网关重复使用额度，会在主备与恢复部分继续讨论。

### 1.3 优先按独立状态分片

当一台机器装不下全部工作时，可以按市场、标的组、策略组或订单会话分片。理想的分片拥有自己的订单簿、策略状态和发单额度，使大部分消息在本分片内完成处理。

把“解码、计算特征、策略、风控”逐段分散到多台机器，会让每条行情反复经过网络。只有某个阶段确实需要独立扩展、共享或专用硬件时，这种流水线才有充分理由。跨市场策略尤其需要先画出依赖关系：如果每次决策都要等待远端状态，本地机器再快也无法消除这段等待。

## 2. 进程之间怎样通信

### 2.1 共享内存里真正共享的是什么

两个进程可以把同一个共享内存对象映射到各自的虚拟地址空间。它们看到的虚拟地址可能不同，底层对应的内存页可以相同。生产者写入消息并发布进度后，消费者通过自己的映射读取这些页。[Linux POSIX 共享内存][shm]

```text
进程 A 的虚拟地址                进程 B 的虚拟地址
       │                               │
       └────────── 同一组内存页 ──────────┘
                   │
          消息槽位、发布位置、消费位置
```

如果使用自建的单生产者单消费者环形队列，部署协议至少需要确定：

- **布局**：字段大小、对齐、版本和容量；跨进程使用偏移量或索引表达引用。
- **所有权**：谁写消息、谁发布位置、谁决定槽位可以复用；原子操作与内存序在目标 ABI 和实现上是否支持进程间共享。
- **队列满的处理**：等待、拒绝、丢弃还是进入保护状态；订单和诊断样本应分别设计。
- **重启**：谁创建和清理共享区，如何识别旧进程留下的数据，如何区分不同启动代次。

进程 A 崩溃时可能已经写了一半消息，也可能已经发送订单但尚未记录结果。共享区还在，并不意味着里面的状态可以直接继续使用。可用发布序列号识别完整消息，用启动代次区分旧实例，再通过业务恢复流程处理未确认订单。

这里还要区分两个成本：一次小消息复制，以及两个核之间的缓存行所有权交接。小消息留在一块不断争用的“零拷贝”共享结构里，未必比复制到消费者自己的局部状态更快。这个取舍应使用实际消息大小、生产速率和核间距离测试。

### 2.2 Aeron IPC 的数据路径

**Aeron Client 是链接到应用里的客户端库；Media Driver 是管理通信资源和网络 I/O 的运行组件。** Driver 可以嵌入应用进程，也可以独立启动。多个本机客户端通过相同的 Aeron 目录连接同一个 Driver 实例。[Aeron 架构说明][aeron-architecture]

使用 `aeron:ipc` 时，路径可以简化为：

```text
进程 A：Publication.offer()
              │ 写入并发布
              ▼
       共享的 publication log buffer
              │ 读取
              ▼
进程 B：Subscription.poll()

Media Driver：注册、资源与位置管理、生命周期维护
```

这条 IPC 数据路径中，订阅者直接读取共享 log buffer，消息无需由一个 Driver 转发线程逐条搬运。Driver 仍须运行，负责控制与维护工作。文件映射到内存的 log buffer 也不等于已经持久化的业务日志。[Aeron 共享内存设计][aeron-design]

部署时需要一起确定 Driver 目录、访问权限、共享内存容量和清理规则。Linux 上可以把运行目录放在容量充足的内存文件系统中；若放在容器里，还要核对 `/dev/shm` 或共享挂载的大小。不同 Driver 实例使用不同目录，需要通过 IPC 通信的客户端连接同一个 Driver 目录。清理旧目录之前，先确认依赖它的实例已经退出。

### 2.3 Driver 的线程模型影响核预算

Java Media Driver 把发送、接收和资源管理分别交给 Sender、Receiver、Driver Conductor 三类 agent。线程模式决定这些 agent 如何组合运行：[Aeron 线程模型][aeron-threading]

| 模式 | 核心 agent 的线程分组 | 部署含义 |
|---|---|---|
| `DEDICATED` | 三者分别运行 | 能独立安排收、发与管理工作，核预算较高 |
| `SHARED_NETWORK` | Sender 与 Receiver 共用线程，Conductor 单独运行 | 减少一个核心 agent 线程 |
| `SHARED` | 三者共用一个线程 | 适合核预算有限、负载允许的场景 |
| `INVOKER` | 由调用者驱动工作循环 | 应用负责及时调用，不能把维护工作长期饿死 |

表里统计的是核心 agent 的分组。实际线程还包括客户端、应用和运行时线程，部分版本与配置也会增加辅助线程。因此，部署应以启动后的线程清单验收。只做本机 IPC 时，也没有必要仅因为系统追求低延迟，就照搬 UDP 场景下的独占收发线程配置。

**空闲策略决定了这些线程在没有消息时做什么。** Busy spin 持续轮询，适合拥有独立物理核的关键线程；yield、退避和休眠减少 CPU 占用，但会改变再次取得消息的时机。把两个忙轮询线程放到同一逻辑 CPU，或者同一物理核的两个 SMT 线程上，很容易抵消 busy spin 的收益。

### 2.4 跨机器的 Aeron UDP

跨机器时，数据路径多了网络收发：

```text
主机 A                                         主机 B
Publication → 本地 log → Driver Sender → UDP → Driver Receiver → 接收 log → Subscription
```

Aeron 在 UDP 上实现自己的流控与可靠传输机制。接收方通过状态消息报告接收窗口，缺口可触发 NAK 和重传；发布者也需要处理背压。`offer()` 成功表示消息进入本地 publication log，并未确认远端业务已经处理。[Aeron 协议][aeron-protocol] [Publication API][aeron-publication]

录制和回放可使用 Aeron Archive，但要单独设计记录器的资源和落后策略。如果慢记录器参与了交易流的消费进度约束，磁盘变慢就可能把背压传回交易。可以建立独立记录流，也可以让关键日志同步进入可靠记录路径；选择取决于故障后允许丢失什么，以及交易是否必须等待记录完成。[Aeron Archive][aeron-archive]

独立记录流还需要配套的满队列策略。交易线程如果反复重试向记录流发布，仍会停在记录工作上。诊断样本可以丢弃并计数；必须完整保留的业务记录，则需要按设计限流、停单或进入恢复流程。[Aeron 流控与背压][aeron-flow]

### 2.5 通信方式按边界选择

| 边界 | 可采用的方式 | 选择时关注什么 |
|---|---|---|
| 同一线程 | 函数调用、局部状态 | 最少的交接，代码与状态耦合程度较高 |
| 同机不同线程 | 有界队列、Disruptor 等 | 缓存行流转、生产消费速率、核的位置 |
| 同机不同进程 | 自建共享内存队列、Aeron IPC | ABI、生命周期、背压、恢复与维护成本 |
| 跨机可靠流 | TCP、Aeron UDP | 端点数量、扇出、丢包行为、运维与库支持 |
| 跨机特定内存通信 | RDMA | 两端支持、内存注册、队列和恢复协议 |
| 管理与配置 | Unix domain socket、TCP、HTTP/RPC | 接口清晰、鉴权、超时和可观测性 |

内部系统可以选择 Aeron 或其他通信方式，连接交易所时则要使用对方提供的协议和接入方式。交易所要求 TCP 的订单会话，不能由客户端自行替换成 Aeron UDP。RDMA 也适用于双方都支持的内部链路，需要完整的业务协议来管理状态和故障。

## 3. 先看硬件拓扑，再分配 CPU

### 3.1 Socket、NUMA node 和逻辑 CPU 分别表示什么

CPU socket 是物理处理器插槽；NUMA node 描述 CPU 与内存的访问局部性；Linux 的 CPU 编号通常指一个逻辑 CPU。同一个物理核启用 SMT 后会呈现多个逻辑 CPU，它们共享部分执行资源。

一个 socket 可以包含多个 NUMA node，也可能有多个末级缓存域。因此，“部署在同 socket”只是第一步。还要看线程是否跨 NUMA、是否跨 LLC（末级缓存）域，以及网卡的 PCIe 路径属于哪里。[Linux CPU 拓扑][cpu-topology] [DPDK Linux 性能指南][dpdk-performance]

在目标 Linux 服务器上先查看：

```bash
lscpu -e=CPU,NODE,SOCKET,CORE,ONLINE
numactl --hardware
lstopo-no-graphics
lspci -tv
```

`lstopo-no-graphics` 来自 hwloc。再针对实际网卡查询其 PCIe 位置和本地 CPU：

```bash
# 把 enp65s0f0 替换为实际网络接口。
nic=enp65s0f0
ethtool -i "$nic"
cat "/sys/class/net/$nic/device/numa_node"
cat "/sys/class/net/$nic/device/local_cpulist"
cat /sys/devices/system/cpu/cpu2/topology/thread_siblings_list
```

若 `numa_node` 返回 `-1`，表示系统没有提供明确的节点归属，需要继续核对 PCIe 和主板拓扑。使用用户态驱动接管的设备可能没有对应 netdev，这时从 PCI 地址进入 `/sys/bus/pci/devices/` 查询。

### 3.2 把网卡、线程和热内存放在一起

期望的数据路径是：

```text
NIC ──本地 PCIe──→ 本节点的接收内存
                         │
               本节点上的行情/策略/网关线程
                         │
                    本地发送路径
```

如果网卡连在 socket 0，而接收线程和缓冲区在 socket 1，一部分数据访问就可能跨处理器互联。只把线程移到 socket 0，还可能留下此前分配在远端的内存页。部署亲和性需要同时处理设备、执行位置和内存位置。[DPDK Linux 性能指南][dpdk-performance]

共享内存同样有 NUMA 位置。要在目标节点设置分配策略、创建并触及热页，再验证实际分布。某个后台线程如果先把共享区清零，可能改变页面最初分配的位置；已经存在的文件映射页也不会因新进程设置了 CPU affinity 就自动搬迁。[Linux NUMA 内存策略][numa-policy]

### 3.3 一份具体的核分配示例

假设服务器有两个 socket，每个 socket 有八个物理核，每核两个 SMT 线程，并且本例中每个 socket 对应一个 NUMA node。**假设**系统报告的编号如下：

```text
socket 0 / node 0：物理核的逻辑 CPU 对为 (0,16)、(1,17)、…、(7,23)
socket 1 / node 1：物理核的逻辑 CPU 对为 (8,24)、(9,25)、…、(15,31)
```

两张交易网卡连接 node 0，可以这样安排：

| 资源 | 放置位置 | 说明 |
|---|---|---|
| 系统、管理线程、必要内核工作 | CPU 0、1、16、17 | 保留 housekeeping 资源，处理系统杂务与允许的 IRQ |
| 行情热线程 | CPU 2 | 独占物理核；兄弟 CPU 18 留空并从普通任务池排除 |
| 策略热线程 | CPU 3 | 兄弟 CPU 19 同样保留 |
| 网关热线程 | CPU 4 | 兄弟 CPU 20 同样保留 |
| `SHARED` 模式外置 IPC Driver 的核心工作线程 | CPU 5 | 若采用忙轮询，兄弟 CPU 21 也保留 |
| 客户端 conductor 等辅助线程 | CPU 6、7 的辅助资源池 | 根据实际线程与空闲策略安排，避免落到热核 |
| 订单簿、队列、接收缓冲区 | node 0 内存 | 启动时分配、触页并验收 |
| 录包、压缩、非关键分析 | node 1 或独立记录机 | 控制跨 socket 读取交易内存的频率 |

这里预留的是物理资源，所以 CPU 18、19、20 没有运行交易线程，也不能被普通任务随意占用。也可以在固件中关闭 SMT，或在系统层面下线相应逻辑 CPU；选择哪种方式，应与机器其余用途一起确定。

录包放在另一个 socket 能减少部分 CPU 竞争，但远端记录器读取共享日志仍会产生跨 socket 流量。如果交易与录包都很繁忙，更清晰的方案是网络侧分流到独立记录机。

Node 1 也应保留适当的本地 housekeeping 资源，剩余核再分给后台任务。表中的分工是一份资源预算，实际需要用线程、IRQ 和内存页分布确认它已经生效。

## 4. 绑核之后，还要处理哪些干扰

### 4.1 Affinity 只规定线程可以在哪里运行

Linux 的 CPU affinity 是线程属性。给进程主线程设置掩码，不能代替对已经存在的所有线程进行检查；线程创建时也要考虑继承的掩码。应用可以在创建线程后设置各自的 affinity，或者由启动和管理脚本配合完成。[sched_setaffinity 手册][affinity]

```bash
# 查询一个进程的全部线程；12345 为示例 PID。
taskset -apc 12345
ps -L -p 12345 -o pid,tid,psr,comm
```

`psr` 是线程最近运行的逻辑 CPU，适合辅助观察；允许范围要看 affinity/cpuset。还需要检查其他线程是否被允许运行在热核上。把交易线程固定到 CPU 2，只完成了“它不迁走”，尚未完成“别人不进来”。

### 4.2 CPU isolation 是几层配置的配合

| 配置层 | 处理的干扰 | 仍需配合的工作 |
|---|---|---|
| 线程 affinity | 线程迁移、执行位置不确定 | 阻止无关任务进入相同 CPU |
| isolated cpuset partition | 普通任务调度与负载均衡 | IRQ、内核线程、定时器另行核对 |
| `nohz_full` | 满足条件时减少周期性调度 tick | 热核上保持合适的可运行任务数量，减少内核往返 |
| RCU callback offload | 把 RCU 回调移出指定 CPU | 为回调执行线程安排 housekeeping CPU |
| IRQ affinity | 设备中断的投递位置 | 同时查看队列、softirq、驱动与 managed IRQ 行为 |
| workqueue 与后台线程配置 | 其他内核及用户态工作 | 留足系统资源并检查实际运行位置 |

Linux 的 CPU isolation 文档建议新部署优先考虑 cgroup v2 的 **isolated cpuset partition**，它可以在运行时调整。通过传统 `isolcpus=domain` 启动参数移出调度域的 CPU，不能在本次启动期间恢复到原调度域。[Linux CPU isolation][cpu-isolation] [cgroup v2 cpuset][cgroup-v2] [内核启动参数][kernel-parameters]

`nohz_full` 主要适合长时间停留在用户态、每核只有一个可运行任务的工作模式；它不会消除系统调用、设备中断和其他全部干扰。当前内核会把 `nohz_full` CPU 上的 RCU 回调自动 offload，`rcu_nocbs` 仍是可单独使用的控制项。具体启动参数与行为应对照所用内核版本。[NO_HZ 文档][nohz] [内核启动参数][kernel-parameters]

Housekeeping CPU 需要承担这些被迁走的工作。把所有核都划给交易，或者只留下一个已经饱和的系统核，会让资源回收、网络管理和设备维护积压，最终影响交易进程。

### 4.3 “关中断”要落实到具体路径

在低延迟系统里，更常见的目标是让**无关中断离开热核**，并为关键收包路径选择合适的工作方式。

使用普通 Linux socket 时，网卡队列与 NAPI、softirq 参与报文处理。可通过 RSS、队列数量和 IRQ affinity 把相关工作放到指定 CPU，再比较两种布局：网络处理与应用同核，或者网络处理占用相邻的独立核。前者可能减少跨核交接，后者把处理压力分开；实际收益取决于收包负载和应用工作量。[Linux 网络扩展机制][network-scaling] [NAPI 文档][napi]

如果启用了 RPS（Receive Packet Steering），协议栈处理还可能被软件转移到另一 CPU。IRQ 的投递位置和最终处理位置需要一起核对，查看各 RX 队列的 `rps_cpus` 即可确认相应配置。

使用 DPDK Poll Mode Driver 等用户态轮询路径时，关键收发可以不依赖每包中断，但链路事件、错误处理和其他设备仍有中断。IRQ 分配还可能受到 `irqbalance`、驱动重载和 managed IRQ 的影响，应核对实际生效的 CPU 集合。[DPDK PMD][dpdk-pmd] [IRQ affinity][irq-affinity]

```bash
cat /proc/interrupts
cat /proc/softirqs
# 123 是示例 IRQ 编号，使用上面查到的真实编号。
cat /proc/irq/123/smp_affinity_list
cat /proc/irq/123/effective_affinity_list
```

普通进程不能靠关闭整台机器的中断获得稳定低延迟。固件产生的 SMI（System Management Interrupt）甚至不由 Linux 的普通 IRQ 机制处理。若出现无法由调度和设备活动解释的长停顿，可以在离线验收中使用 `hwlat_detector` 检查硬件/固件停顿；该工具会占用 CPU 并关闭本地中断，不适合在生产交易期间运行。[硬件延迟检测器][hwlat]

### 4.4 实时优先级、PREEMPT_RT 和电源设置

`SCHED_FIFO` 等实时调度策略主要改变可运行线程之间的优先级，PREEMPT_RT 主要改善内核可抢占性。对于已经独占核、长时间在用户态轮询的线程，它们是否有收益，需要看仍存在哪些调度等待。实时线程也可能饿死低优先级的 Driver 或维护线程，因此需要一起设计线程优先级、阻塞关系和系统保留资源。[Linux 调度策略][sched]

固件与电源设置则影响核心从空闲到运行、从短时到持续负载的行为：

- **C-state** 描述空闲深度。限制较深的空闲状态可以减少唤醒等待，也会提高功耗和温度。
- **P-state、频率策略与 Turbo** 影响运行频率。固定较低频率可能减小变化，同时也可能让每条消息都更慢；应比较持续负载下的延迟和频率。
- **散热与功率预算** 决定机器能否长期保持目标状态。短暂 benchmark 中的频率不能代表开盘高负载持续运行后的频率。
- **BIOS、BMC、微码和 NIC 固件** 应纳入版本清单。固件升级会改变设备与平台行为，升级后需要重新验收。

这些设置可以通过平台提供的性能配置和系统接口逐项验证，记录每项变更对应的延迟、稳定性和资源变化。[CPUIdle][cpuidle] [CPUFreq][cpufreq]

## 5. 把内存准备工作移到开盘之前

### 5.1 分配、触页、锁页与预热各有作用

程序启动后可以先完成以下步骤，再进入允许交易的状态：

1. 确定线程 affinity 与 NUMA 分配策略。
2. 分配订单簿、订单池、队列和网络缓冲区。
3. 在目标节点触及将要使用的内存页，包括必要的栈空间。
4. 按资源限制锁定需要常驻的内存，并检查调用结果。
5. 加载配置和模型，建立连接，预热解析、策略与编码路径。
6. 确认行情、订单和风险状态已经恢复，再开放发单。

`mlock`/`mlockall` 用于防止所锁页面被换出，并不能把这些页面锁在 CPU cache 里。预分配、触页和足够的 memlock 限额需要配合；未来映射、栈增长及写时复制也要纳入启动设计。[mlock 手册][mlock]

NUMA 内存策略主要控制分配位置，对已经存在的共享文件页还要检查实际页分布。可使用 `numastat -p PID` 和 `/proc/PID/numa_maps` 验收，避免只看启动命令里是否写了 `--membind`。[NUMA 内存策略][numa-policy]

### 5.2 大页减少地址翻译压力，也需要管理

大页让一个 TLB 条目覆盖更多地址，适合较大的、反复访问的订单簿和缓冲区。部署上要区分预留的 HugeTLB 页面与 Transparent Huge Pages（THP，透明大页）：前者显式管理页池，后者由内核按策略分配、合并和拆分。[HugeTLB][hugetlb] [THP][thp]

对延迟敏感的固定工作集，可以在启动前或初始化阶段准备大页、按 NUMA node 保留容量，再确认应用实际使用了它们。THP 的整理与合并策略要结合工作负载选择，避免运行中临时进行昂贵的内存整理。小工作集未必能从大页得到明显收益，也不需要为所有进程统一使用相同页大小。

### 5.3 控制缓存和内存带宽竞争

绑到不同物理核的任务仍可能共享 LLC 和内存控制器。大规模扫描、压缩和日志处理可能挤占交易线程的数据缓存，或消耗所在 socket 的内存带宽。

可先把这些工作移到其他 NUMA node 或机器；确实需要同机运行时，再研究硬件支持的缓存分配与带宽控制。Linux `resctrl` 可提供缓存分配（CAT）、内存带宽控制（MBA）等能力，具体功能和粒度取决于硬件。缓存分配约束的是可用资源，不等于把指定订单簿永久锁在缓存中。[resctrl 文档][resctrl]

## 6. 网络部署要细到队列和收发 API

### 6.1 先选清楚收包和发单路径

| 路径 | 应用使用的接口 | 部署重点 |
|---|---|---|
| Linux socket | `recv`/`send` 等 | 驱动、NAPI、IRQ、socket 缓冲、队列与应用调度 |
| 加速 socket 栈 | 厂商兼容接口或用户态 TCP/UDP 栈 | 网卡与驱动兼容性、实际是否进入加速路径、连接与线程归属 |
| DPDK、厂商低层 API | 直接处理报文与队列 | 独占轮询核、DMA 缓冲、队列所有权、协议实现 |
| AF_XDP | XDP 重定向到用户态 UMEM 与 ring | 队列绑定、驱动支持、copy/zero-copy 模式和唤醒机制 |
| FPGA 数据路径 | 固件接口、主机控制接口 | 哪些阶段在卡上完成，主机何时介入 |

DPDK 提供报文 I/O 能力，订单连接若使用 TCP，还需要相应的协议栈或其他支持路径。AF_XDP 的零拷贝能力也需要驱动支持，部署验收应核对实际协商的模式。[DPDK PMD][dpdk-pmd] [AF_XDP][af-xdp]

网卡选型要确定行情与订单使用的 API、硬件时间戳的获取位置，以及这些路径能否同时使用。

### 6.2 RSS 和队列分配应保留消息归属

RSS（Receive Side Scaling）通过哈希等机制把流量分到多个接收队列。它有助于把不同流交给不同核，但不能保证一个高流量的行情通道会自动均匀分摊到多个核，也不能理解业务消息之间的顺序关系。[Linux 网络扩展机制][network-scaling]

一个明确的部署方案会把“市场通道—网卡端口—RX queue—接收线程—CPU”列成映射表。对于 A/B 两路行情，可以由同一线程轮询两个队列，减少合并交接；也可以由两个接收线程交给合并器，获得更独立的接收能力。后者要计入合并队列的等待与顺序恢复成本。

发送侧同样需要所有权。多个线程争用同一 TX queue 可能引入同步；随意把一个有序会话分散到多队列，也需要检查发送次序。应把发送队列与连接或生产者的关系写进部署配置。

### 6.3 批处理、合并和队列容量的取舍

中断合并、接收聚合（GRO/LRO）、发送分段卸载（TSO/GSO）、批量收发和更大的缓冲区，分别在不同位置改变报文处理方式。吞吐测试中的优选参数，可能让小消息等待更久。反过来，把所有批处理都去掉，也可能降低处理能力，在峰值时制造更长队列。

可以从以下顺序调试：先确定持续和突发到达率，再选择一次轮询的处理上限、是否等待凑批、队列容量以及各类 offload。不同 offload 要分别判断，校验和卸载与等待报文聚合并不是同一个机制。

```bash
nic=enp65s0f0
ethtool -l "$nic"   # 队列/通道配置
ethtool -x "$nic"   # RSS indirection table；取决于设备支持
ethtool -c "$nic"   # 中断合并
ethtool -k "$nic"   # offload 状态
ethtool -g "$nic"   # ring 参数
ethtool -S "$nic"   # 设备统计
cat /sys/class/net/"$nic"/queues/rx-*/rps_cpus
```

队列容量也可以先做一个简单估算。假设一个突发期间到达率为每秒 200 万条、处理能力为每秒 150 万条，持续 2 毫秒，那么这段时间会额外积压约：

```text
(2,000,000 - 1,500,000) × 0.002 = 1,000 条
```

这是匀速模型下的容量估算，还没有包含原有积压和更细粒度的峰值。扩大 ring 能容纳积压，却不会让消息更及时；部署指标应同时包含队列占用、消息年龄和丢包，而不仅是 CPU 利用率。

### 6.4 MTU、包长和协议边界

链路速率影响序列化时间。忽略前导码、帧间隙和编码等额外开销，1,500 字节在线速 10 Gb/s 上占用约 `1.2 μs`，64 字节约 `51.2 ns`。这些是字节数除以线速的计算，不包含交换、排队与应用处理。

因此，Jumbo frame 对大吞吐传输的收益，不能直接套到小订单上。行情、交易和记录网络可以采用不同的 MTU 与批处理策略，但端到端路径必须匹配，避免意外分片或丢包。网卡、交换机和交易所接口都应进入同一份网络配置清单。

## 7. 低延迟网卡、SmartNIC 和 FPGA 怎样放进系统

### 7.1 先明确希望硬件替主机完成什么

SmartNIC 是一个很宽的名称。部署选型时，可以先区分三类目标：

| 目标 | 典型数据路径 | 对部署的影响 |
|---|---|---|
| 缩短主机收发路径 | 网卡 → 用户态缓冲/协议栈 → CPU 程序 | 仍需处理 CPU、PCIe、NUMA 和用户态栈配置 |
| 在 FPGA 上执行交易相关逻辑 | 报文 → 卡上解析/计算/检查 → 卡上构造输出 | 把部分路径移出 CPU，需要管理固件、状态和主机控制接口 |
| 卸载基础设施工作 | 网卡/DPU 处理网络、存储、安全或管理任务 | 减少主机承担的工作，评估新增路径及隔离效果 |

例如 AMD Alveo X3 的官方资料分别列出了 Onload 的 socket 加速、TCPDirect、`ef_vi` 原始以太网接口和可编程 FPGA 产品。这些接口对应不同的数据路径，不能只看卡上是否存在 FPGA。[AMD Alveo X3][amd-x3] NVIDIA BlueField 则结合网络接口、Arm 核和加速引擎，主要面向基础设施卸载；DPU 的类别本身不能推出订单处理更快。[BlueField 产品说明][bluefield]

### 7.2 主机与网卡之间仍有明确的边界

多数主机收包路径会让网卡通过 DMA 将数据写到主机内存，CPU 再读取。应用直接访问该缓冲区，可以减少复制或协议栈处理；缓冲区的位置、生命周期和缓存行为仍然重要。

CPU 通过 BAR/MMIO 访问设备控制寄存器或卡上内存，是另一条方向的访问。DMA 缓冲、描述符和 doorbell 的具体布局由网卡及 API 决定，不能把“映射到用户态”统一理解成访问普通主机 DRAM。采购和部署时应核对所用 API 的真实收发路径。[Linux DMA 地址与映射][dma] [设备 I/O 访问][device-io]

PCIe 还应检查实际协商的代际、宽度和连接层级。网卡装进一个外形为 x16 的插槽，不代表它一定获得 x16 电气连接；共享 PCIe switch、错误重传和平台节能状态也会影响设备路径。可以用 `lspci -vv` 检查链路状态，再与主板手册核对。[DPDK Linux 性能指南][dpdk-performance]

### 7.3 FPGA 部署增加了另一套发布与恢复工作

如果 FPGA 负责行情解析、特征计算或订单构造，部署清单需要加入 bitstream、协议版本、参数表、风险限额和复位行为。主机如何更新参数、更新是否原子生效、卡重置后保留哪些状态，都需要定义。

FPGA 可以在流水线中提早处理已到达的字段，但“开始解析”与“允许发单”要分别设定完成条件。例如，是否需要等完整报文、校验结果和必要的序列号检查完成。错误报文处理、行情恢复与人工停单应和正常路径一起实现。

上线前应分别验证卡上数据路径和主机控制路径。固件回滚还要兼容当前的共享状态与订单会话，否则只替换旧 bitstream 并不能恢复到旧系统的运行状态。

## 8. 从机房位置到交换设备

### 8.1 “离交易所近”需要落实到接入产品

| 部署方式 | 大致位置 | 需要进一步确认的内容 |
|---|---|---|
| Co-location，通常简称 colo | 交易所提供或指定的托管设施 | 机柜、接入交换机、cross connect、行情与订单端口 |
| Proximity hosting | 交易所附近的第三方设施 | 实际线路长度、接入节点、中间设备和运营商路由 |
| 公有云 Region/AZ | 某地理区域中的云资源 | 撮合设施位置、AZ、专线入口、虚拟化与共享网络路径 |
| 交易所配套的专用云设施 | 与特定市场接入共同设计的设施 | 对应产品的物理拓扑、开放时间、性能与接入约束 |

Cross connect 通常指设施内连接双方端口的线路。进入同一座机房后，仍要购买和配置相应连接；机柜位置也不必然决定到交易所的线缆长度。例如 CME GLink 对客户空间采用等距离 cross connect，以提供位置中立的接入。[CME Connectivity Options][cme-connectivity]

JPX 的服务目录把 colo、Kansai proximity、交易 unicast、行情 multicast、L1 multicast 和时间同步连接分别列出。实际部署时，机柜、电力、交易线路、行情线路、时间源和测试环境连接都要逐项落实。[JPX Connectivity Services][jpx-connectivity]

云环境中的 Region 是粗粒度地理位置，AZ 则是 Region 内隔离的部署位置，可能包含一个或多个数据中心。选中与交易所同城的 Region，仍需要确认报文从实例到交易入口的真实路径。[AWS Region 与 AZ][aws-regions]

这一领域也在变化。CME 在 2026 年 10 月 12 日公告中表示，Aurora Cloud Region 与 colo 设施计划于 2027 年中供客户测试，Livestock 市场计划于 2027 年底迁入 Aurora。这是已公布的迁移计划。评估时应分别比较普通云实例、邻近托管和交易所专用低延迟云产品，不能仅用“是否在云上”代替网络拓扑判断。[CME 云迁移公告][cme-cloud]

### 8.2 用物理距离建立延迟尺度

以普通单模光纤的群折射率约 `1.4682` 计算，传播时间为：

```text
t = 折射率 × 实际光纤长度 / 真空光速
  ≈ 4.9 μs / km，单程
```

这个折射率取自 Corning SMF-28 Ultra 在 1550 nm 下的典型参数。[Corning 光纤规格][corning]

| 实际光纤长度 | 只计算传播的单程时间 |
|---:|---:|
| 100 m | 约 0.49 μs |
| 1 km | 约 4.9 μs |
| 10 km | 约 49 μs |
| 100 km | 约 490 μs |

计算对象是光纤沿途的实际长度，还要另加设备处理、排队和两端系统的时间。地图上的直线距离通常不足以描述线路路径。由此也容易看出，几公里的绕行可能超过许多进程内优化累计获得的收益。

跨城市的多市场策略，可以把接入与本地执行单元放在各市场附近，只在站点间传递必要的信号、状态和额度。远距离链路则比较路由、可用性、带宽和传播介质；涉及专用无线链路时还需考虑路径条件和备用线路。此类优化解决的是站点间信息到达时间，不会让一套集中部署的状态同时靠近所有市场。

### 8.3 特殊交换机分别缩短哪一段

**Cut-through switch** 在取得足够的转发信息后开始发送，store-and-forward 则接收整帧再转发。实际模式受到设备、端口速率和启用功能影响。例如 Cisco Nexus 3000 文档明确讨论了入口和出口速率不同时的转发方式。[Cisco switching modes][cisco-switching]

**L1 matrix / fanout** 更适合固定连接和一入多出的复制。它可以把行情流同时送到交易主机、备机和采集设备。Arista 7130B 的 L1 路径不做缓存和排队；同一平台也可以提供经过交换芯片的 L2/L3 路径，两者应分别看待。[Arista 7130B][arista-l1]

**订单汇聚设备** 需要处理多入一出的竞争。**TAP** 把链路流量复制到观测端口；**packet broker** 再按需要过滤、复制或汇聚这些观测流量。设备可以在同一个机箱里提供这些能力，但流量经过的模块、缓冲和时间戳位置可能不同。Arista 7130 文档把 MetaMux、MetaWatch 和 SwitchApp 等功能分别说明，正好体现这种区别。[Arista 7130 功能划分][arista-platform]

```text
行情一入多出：                         订单多入一出：
                  ┌→ 主交易机          策略机 1 ─┐
行情 → L1 fanout ─┼→ 备用机            策略机 2 ─┼→ 汇聚/交换 → 交易所端口
                  └→ 采集机            策略机 3 ─┘
```

第二种结构需要共享一个出口。即使交换逻辑很快，出口正在发前一个包时，后一个包仍然要等待。

假设 10 Gb/s 的出口前已经积压 64 KiB 数据，仅发送这些字节就需要约：

```text
65,536 × 8 / 10,000,000,000 ≈ 52.4 μs
```

这是排队示例，未包含以太网额外开销。它解释了为什么微突发、出口超售和后台流量可能比空载转发时间更影响尾延迟。深缓冲能够降低突发时的丢包，却会保留等待中的消息。网络验收应同时看队列、丢包和端到端延迟。

### 8.4 A/B 线路要一直检查到故障域

行情 A/B 常见的使用方式是并行接收、按序列号仲裁，先取得可用消息的一路推进状态，另一路的重复消息丢弃。CME MDP 3.0 的官方建议就是同时处理两路增量行情；两路都缺失时再进入恢复流程。[CME Feed Arbitration][cme-feed]

部署上需要继续追问“两路”隔离到了哪一层：

| 冗余安排 | 仍然可能共享的故障点 |
|---|---|
| 两个 multicast 地址 | 上游线路、交换机、网卡与主机 |
| 同一网卡的两个端口 | 板卡、PCIe 链路、驱动与主机 |
| 同一主机的两张网卡 | 主板、操作系统、电源与机柜 |
| 两台主机 | 上游交换机、PDU、机柜与站点 |
| 不同站点 | 运营商路由、控制系统、配置与软件缺陷 |

因此，A/B 行情、交易主备和时间源各自需要一张故障域图。把两台服务器画成两个框，还不足以判断它们是否会同时失效。

## 9. 把时间同步和观测也作为部署的一部分

### 9.1 为每个测量点说明时钟来源

沿用前一章的边界，本机从收到行情到发出订单可以记为：

```text
RX 硬件时间 H0 → 应用取得行情 C0 → 业务处理 → 编码完成 C4 → 提交发送 → TX 硬件时间 H1
```

`C4 - C0` 描述应用处理区间；`H1 - H0` 描述网卡边界之间的本机路径。Linux 的 `SO_TIMESTAMPING` 区分硬件、软件等时间戳，TX 硬件时间戳可以通过错误队列异步取得。部署要验证驱动支持、时间戳配置和报文关联，不能把提交发送或完成通知当成线上实际发送时间。[Linux Timestamping][timestamping]

两块网卡可能各有自己的 PHC（PTP Hardware Clock）。即使 RX、TX 都叫硬件时间戳，也要确认两端来自同一时钟，或者已经建立同步与误差估计。CPU TSC 与 PHC 的对应关系同样需要建立；同 socket 部署并不会自动消除所有时钟域问题。

### 9.2 PTP 服务和时间网络怎样安排

PTP（Precision Time Protocol）用于同步时钟。Linux 上的 `ptp4l` 参与 PTP 协议与硬件时钟同步，`phc2sys` 可以同步 PHC 和系统时钟。部署时要明确源、方向和时间尺度，避免多个服务同时调整同一时钟。[ptp4l][ptp4l] [phc2sys][phc2sys]

时间服务和驱动辅助线程需要稳定的 CPU 资源，可放在 housekeeping 池中。监控应记录同步偏差、源切换、失锁和保持状态。PTP 协议运行正常，只说明同步机制在工作；是否满足测量精度，还要看路径不对称、时间戳能力和时钟源质量。

如果跨机测得的差值已经接近同步误差，数据就难以区分网络与时钟的贡献。此时可以优先使用同机同钟的收发测量、往返测量，或由独立采集设备在同一时钟域对两条线路打点。

### 9.3 让记录系统拥有自己的资源预算

可以把记录分为三类：

- **业务记录**：订单、成交、风险和恢复需要的事件，定义完整性、落盘与故障策略。
- **原始报文记录**：保留线路、方向、时间戳和缺包信息，用于复盘协议与网络问题。
- **诊断样本**：阶段耗时、调试事件等，允许按预定策略采样或丢弃，并统计丢失数量。

网络侧 TAP 或 L1 复制可以把流量送到独立采集机，减少交易线程上的全量抓包工作。采集机仍需要满足瞬时流量、存储和时间戳要求；多路汇聚到一个端口时也可能丢包。文件里应记录采集丢包计数，避免把采集缺口误判为交易主机缺包。

回放还要保留原始到达节奏或明确使用的加速方式。单纯以磁盘最大吞吐重放，与开盘时的微突发和间歇到达是不同的负载。业务处理变慢时，测试发生器仍应按计划投递，并记录计划到达与实际完成，才能观察真实积压。

## 10. 主备切换围绕出单权限和状态恢复设计

### 10.1 热备可以同时算，但要控制谁能发

备用机可以持续接收行情、维护订单簿并计算候选订单，使接管时不必从零预热。对于同一组订单与风险额度，需要有明确的有效出单者。

```text
                    ┌→ 主实例：接收行情、计算、拥有出单权限
行情 A/B ───────────┤
                    └→ 备实例：接收行情、计算、恢复状态

接管：发现异常 → 隔离旧出单者 → 恢复并核对交易状态 → 授予新出单权限
```

这里的隔离通常称为 **fencing**：在网关、会话权限或可控出口上，让旧实例不能继续提交有效订单。只修改数据库中的 `primary=true` 标志，而旧实例仍可以使用原连接发单，无法解决网络分区后的双主问题。

CME 的 iLink 故障容错资料要求相应主备维护一致的会话序号，并规定业务消息通过有效主连接发送。具体的连接和恢复规则应按所接入交易所实现。[CME Fault Tolerance][cme-failover]

### 10.2 接管时要恢复的是交易事实

恢复流程需要覆盖未完成委托、部分成交、待撤订单、会话序号、客户订单编号和风险额度。尤其要处理一种中间状态：订单已经发出，但主机在收到确认前失效。

备用机不能仅根据“本地没有确认”就认定订单未到交易所，也不能直接把所有未确认请求重新提交。它应按接口提供的恢复方式补回报、查询订单或进行对账，再决定后续动作。TCP 重连与进程拉起只是恢复步骤的一部分。

Cancel on Disconnect（COD，断连撤单）可以帮助控制断连后的挂单，覆盖范围由具体市场规定。例如 CME 针对期货与期权的 COD 不覆盖 GTC/GTD 订单；完整执行正常 logout，以及该文档所述的 Fault Tolerance 主备切换，也不会触发 COD。接管流程应根据实际回报恢复挂单状态。[CME Cancel on Disconnect][cme-cod]

### 10.3 复制与持久化放在哪里，决定故障后的边界

| 方案 | 正常交易路径 | 故障后需要处理的状态 |
|---|---|---|
| 异步记录/复制 | 不等待记录器或备机完成 | 末尾未复制状态，需要靠交易所回报、独立记录或查询恢复 |
| 发单前同步记录 | 等待规定的记录完成点 | 已记录但尚未发送的意图，以及发送与确认之间的不确定状态 |
| 发单前同步复制 | 等待备机达到规定的确认点 | 已复制但尚未发送、已发送未确认的状态仍要区分 |

同步确认应说明到底是“进入备机内存”还是“完成所要求的持久化”。多一份副本和断电后可恢复，是两个不同的目标。业务要求决定确认边界，再把它计入订单延迟预算。

多个网关共享总风险额度时，可以分配互不重叠的额度，使各自完成本地检查。网关失效后，应先确认旧实例已被隔离、已发订单与在途风险得到核对，再重新分配额度。心跳超时本身不足以证明额度可以立即复用。

主备放在不同主机可以覆盖主机故障；独立网卡、交换机、电源和站点进一步扩大可覆盖的故障范围。同站点热备与异地灾备承担不同任务，前者强调快速接管，后者还要处理线路、会话、状态和运营流程的整体迁移。

## 11. 裸机、容器和发布流程

### 11.1 裸机便于直接掌握资源布局

对固定核、固定队列和固定设备的交易单元，专用裸机是一个清晰的起点：硬件拓扑、内核版本、启动参数和设备归属都容易追踪。进程可由 systemd 等服务管理器启动，应用完成细粒度线程绑核和状态就绪检查。

容器可以提供可复现的用户态环境，但共享宿主内核和物理资源。需要显式配置 CPU、内存节点、HugeTLB、memlock、设备权限、共享内存挂载和网络路径。采用 host networking 可以减少一层容器虚拟网络路径，仍需处理宿主机调度、IRQ 与资源竞争。

### 11.2 Kubernetes 可以管理资源，但需要对应的策略

若使用 Kubernetes，低延迟部署至少要核对以下几项：

| 能力 | 用途 | 需要检查的条件 |
|---|---|---|
| CPU Manager `static` | 给符合条件的容器分配独占 CPU | `Guaranteed` Pod、整数 CPU request，以及系统保留 CPU |
| `full-pcpus-only` 等策略 | 按完整物理核分配 | 版本支持、SMT 数量、请求是否匹配 |
| Topology Manager | 对齐 CPU、设备等资源的 NUMA 提示 | `single-numa-node` 策略与正确的 scope |
| Memory Manager | 为符合条件的 Pod 管理 NUMA 内存 | Linux `Static` 策略与节点内存预留 |
| Device Plugin / 设备分配 | 交付所需 NIC、VF 或加速设备 | 设备实际 NUMA 位置、驱动和共享资源 |

上述语义分别见 Kubernetes 的 [CPU Manager][k8s-cpu]、[Topology Manager][k8s-topology] 和 [Memory Manager][k8s-memory] 文档。若多个紧耦合容器必须一起落在同一 NUMA node，应检查 Pod scope；默认按容器对齐，并不等于整个交易单元已经共同放置。

这些设置还需要与宿主 IRQ、housekeeping、后台 DaemonSet 和监控代理配合。`Guaranteed` 是资源服务质量类别，不能替代延迟验收；设备直通或 SR-IOV 也需要检查底层端口、固件和带宽是否共享。

交易单元存在会话和风险状态，不适合只凭 CPU 利用率随时水平扩缩。编排器应理解哪些实例只能观测、哪些实例拥有出单权限，以及重调度后必须完成哪些恢复步骤。

### 11.3 发布单位应包括配置与硬件状态

一个可复现的版本至少应记录：

| 层次 | 记录内容 |
|---|---|
| 应用 | 二进制摘要、编译器与编译选项、配置和模型版本 |
| 通信 | 消息 schema、共享内存布局、Aeron/网络库版本与目录 |
| 操作系统 | 内核、启动参数、cpuset、IRQ、锁页与大页配置 |
| 平台 | 服务器型号、BIOS、微码、NIC 固件与驱动 |
| 可编程硬件 | FPGA bitstream、参数表和恢复方式 |
| 网络 | 交换机软件、端口映射、速率、MTU、过滤和组播配置 |
| 运行状态 | 主备身份、会话归属、风险额度、启动代次 |

发布前可以让新实例进入只计算、不拥有生产出单权限的 shadow 模式，对比行情状态、决策和延迟。准备切换时，先停止旧实例产生新的交易意图，按业务流程处理现有订单，再转移权限并验收新实例。需要灰度时，按独立的标的、账户、会话或交易单元划分，而不是让新旧程序对同一份额度重复出单。

回滚也要检查状态兼容。旧程序可能不认识新共享内存布局、订单状态或协议字段，恢复旧二进制前需要决定哪些状态可保留、哪些必须重建。版本包中应保留配置快照和恢复步骤。

### 11.4 给管理与维护留下独立路径

独立管理网络可以连接 BMC、交换机管理口、远程电源和管理服务。交易网卡被用户态驱动接管、交易链路失效或热核陷入异常时，仍有另一条通道查看机器和执行恢复。

历史回放、镜像拉取、日志上传、备份和压缩应有自己的网络与资源预算。即使它们运行在其他 CPU 上，仍可能争用 PCIe、内存控制器或网络出口。机柜供电与散热也要按持续忙轮询和 FPGA 负载验收，不能只用机器空闲功率做容量规划。

## 12. 把方案收敛成可以验收的部署

### 12.1 一套可以起步的配置

对于单市场、CPU 为主、希望保留进程隔离的系统，可以从下面这套布局开始：

| 层次 | 起步方案 |
|---|---|
| 位置 | 使用明确的交易所接入线路；按延迟目标选择 colo 或邻近托管 |
| 主机 | 主、备使用独立机器，先让一个交易单元落在一个 NUMA node |
| 进程 | 行情、策略、风控/网关三个热路径进程；记录与管理单独运行 |
| 本机通信 | Aeron IPC 或经过验证的固定布局共享内存队列 |
| 线程 | 每条持续忙轮询的热线程独占物理核；辅助线程另有资源池 |
| 内存 | 在目标 node 分配、触页、预热和按需锁页；按工作集评估大页 |
| NIC | 收发路径与 TCP/UDP 协议匹配，队列、线程和 NUMA 位置明确 |
| 网络 | A/B 并行接收与序号仲裁；记录流量和管理流量单独规划 |
| 时间 | 核对 NIC 时间戳能力、PHC 同步和测量边界 |
| 恢复 | 实际生效的出单 fencing、会话恢复、订单与额度核对 |
| 发布 | 固定版本包、shadow 验证、按交易单元切换、状态兼容的回滚 |

如果三进程之间的交接已成为主要延迟来源，可以合并策略与网关，或者把整个关键路径收进一个事件循环。相反，如果共享行情或网关形成拥堵，可以按独立状态分片。进程边界随着测量结果调整，物理部署也应一起变化。

### 12.2 启动顺序应表达业务就绪

```text
硬件、网络和系统配置核验
  → 分配 CPU/设备/内存，启动通信组件
  → 初始化共享区与业务进程，完成预热
  → 接入行情，完成快照与增量衔接
  → 恢复订单会话、委托、成交、持仓与风险额度
  → 确认时间同步、观测和主备身份
  → 由拥有权限的网关开放交易
```

进程启动、端口可连接和可以交易，是不同的就绪条件。管理系统应能看到当前停在哪一步，发生异常时也应按状态收回交易权限，而不是只依靠进程是否存活。

### 12.3 验收要覆盖峰值、干扰和恢复

| 测试 | 主要观察项 |
|---|---|
| 稳态行情与正常发单 | 本机 tick-to-trade、应用分段、队列占用、p50/p99/p99.9 |
| 开盘与突发回放 | 消息年龄、最大积压、丢包、恢复触发、持续处理能力 |
| 后台上传、录包和压缩 | 与热路径共享资源是否引入尾延迟 |
| Driver 或消费者变慢 | 背压传递、队列满策略、是否阻塞交易 |
| 单路行情或设备故障 | A/B 仲裁、缺口检测、恢复与停单条件 |
| 主实例失效与网络分区 | 旧实例是否被隔离，是否发生重复出单或额度复用 |
| 时钟源切换 | 同步误差、测量可用性和告警 |
| 持续高负载与固件升级 | 频率、温度、设备错误、延迟分布与配置变化 |

实验记录应固定消息大小、到达节奏、速率、线程数、核与 NUMA 映射、软硬件版本、预热过程和测量边界。既看分位数，也保留最慢事件附近的队列、丢包、调度和设备信息。多段延迟先按同一事件求和，再统计总分位数。

恢复时间也要定义终点：从故障发生算到进程重新启动，和算到订单、持仓、风险都核对完成并重新开放交易，回答的是不同问题。

### 12.4 一次只读核验

下面补充前面未列出的 Linux 检查项。示例 PID 和 cgroup 路径需要替换成目标机的实际值；命令用于检查状态，本文没有在目标交易服务器上执行调优或测量延迟。

```bash
trade_pid=12345

# 进程允许使用的资源、锁页情况和实际页分布。
rg 'Cpus_allowed_list|Mems_allowed_list|VmLck' "/proc/$trade_pid/status"
numastat -p "$trade_pid"
cat "/proc/$trade_pid/numa_maps"
cat "/proc/$trade_pid/limits"
rg 'KernelPageSize|MMUPageSize|AnonHugePages|Hugetlb|Locked' "/proc/$trade_pid/smaps"

# 启动参数和 full-dynticks CPU。
cat /proc/cmdline
cat /sys/devices/system/cpu/nohz_full

# 先找到进程的实际 cgroup 路径。
cat "/proc/$trade_pid/cgroup"

# 将下面的示例路径替换为实际 cpuset 分区路径。
trade_cgroup=/sys/fs/cgroup/trading
cat "$trade_cgroup/cpuset.cpus.partition"
cat "$trade_cgroup/cpuset.cpus.effective"
cat "$trade_cgroup/cpuset.mems.effective"

# 核对网卡的硬件时间戳与 PTP 时钟支持。
ethtool -T enp65s0f0
```

目标隔离分区的 `cpuset.cpus.partition` 应读回 `isolated`，没有 `invalid` 标记；`effective` 文件说明实际生效的集合。只保留一份曾经执行过的调优脚本，难以发现设备重载、重启或配置变更后产生的偏差；验收结果应和版本包一起保存。

## 参考资料

- **进程通信**：[Aeron Media Driver][aeron-architecture]、[传输协议][aeron-protocol]、[固定版本的 IPC publication 源码][aeron-design]、[Publication/Subscription][aeron-publication]、[Aeron Archive][aeron-archive]。
- **CPU 与内存部署**：[Linux CPU isolation][cpu-isolation]、[cgroup v2][cgroup-v2]、[内核参数][kernel-parameters]、[NUMA 内存策略][numa-policy]、[HugeTLB][hugetlb]、[THP][thp]、[resctrl][resctrl]。
- **网络路径**：[Linux Scaling][network-scaling]、[NAPI][napi]、[AF_XDP][af-xdp]、[DPDK PMD][dpdk-pmd]、[DPDK 硬件布局指南][dpdk-performance]。
- **交易所接入与恢复**：[CME 接入][cme-connectivity]、[JPX 接入][jpx-connectivity]、[CME 行情仲裁][cme-feed]、[iLink 故障容错][cme-failover]、[COD][cme-cod]。
- **设备与时间**：[AMD Alveo X3][amd-x3]、[Arista 7130B][arista-l1]、[Cisco 交换模式][cisco-switching]、[Linux 时间戳][timestamping]、[LinuxPTP][ptp4l]。
- **容器资源管理**：[Kubernetes CPU Manager][k8s-cpu]、[Topology Manager][k8s-topology]、[Memory Manager][k8s-memory]。

[aeron-architecture]: https://aeron.io/docs/aeron/media-driver/
[aeron-design]: https://github.com/aeron-io/aeron/blob/1.53.0/aeron-driver/src/main/java/io/aeron/driver/IpcPublication.java
[aeron-threading]: https://aeron.io/docs/aeron/media-driver/
[aeron-protocol]: https://github.com/aeron-io/aeron/wiki/Transport-Protocol-Specification
[aeron-publication]: https://aeron.io/docs/aeron/publications-subscriptions/
[aeron-archive]: https://github.com/aeron-io/aeron/wiki/Aeron-Archive
[aeron-flow]: https://github.com/aeron-io/aeron/wiki/Flow-and-Congestion-Control
[shm]: https://man7.org/linux/man-pages/man7/shm_overview.7.html
[cpu-topology]: https://docs.kernel.org/arch/x86/topology.html
[dpdk-performance]: https://doc.dpdk.org/guides/linux_gsg/nic_perf_intel_platform.html
[numa-policy]: https://docs.kernel.org/admin-guide/mm/numa_memory_policy.html
[affinity]: https://man7.org/linux/man-pages/man2/sched_setaffinity.2.html
[cpu-isolation]: https://docs.kernel.org/admin-guide/cpu-isolation.html
[cgroup-v2]: https://docs.kernel.org/admin-guide/cgroup-v2.html
[nohz]: https://docs.kernel.org/timers/no_hz.html
[kernel-parameters]: https://docs.kernel.org/admin-guide/kernel-parameters.html
[network-scaling]: https://docs.kernel.org/networking/scaling.html
[napi]: https://docs.kernel.org/networking/napi.html
[dpdk-pmd]: https://doc.dpdk.org/guides/prog_guide/ethdev/ethdev.html
[irq-affinity]: https://docs.kernel.org/core-api/irq/irq-affinity.html
[hwlat]: https://docs.kernel.org/trace/hwlat_detector.html
[sched]: https://man7.org/linux/man-pages/man7/sched.7.html
[cpuidle]: https://docs.kernel.org/admin-guide/pm/cpuidle.html
[cpufreq]: https://docs.kernel.org/admin-guide/pm/cpufreq.html
[mlock]: https://man7.org/linux/man-pages/man2/mlock.2.html
[hugetlb]: https://docs.kernel.org/admin-guide/mm/hugetlbpage.html
[thp]: https://docs.kernel.org/admin-guide/mm/transhuge.html
[resctrl]: https://docs.kernel.org/filesystems/resctrl.html
[af-xdp]: https://docs.kernel.org/networking/af_xdp.html
[dma]: https://docs.kernel.org/core-api/dma-api-howto.html
[device-io]: https://docs.kernel.org/driver-api/device-io.html
[amd-x3]: https://www.amd.com/content/dam/amd/en/documents/products/accelerators/alveo/x3/2656036-alveo-x3-product-brief.pdf
[bluefield]: https://networking-docs.nvidia.com/bluefield2vpihw/introduction
[cme-connectivity]: https://www.cmegroup.com/solutions/market-access/globex/connectivity-options.html
[jpx-connectivity]: https://www.jpx.co.jp/english/systems/connectivity/01.html
[aws-regions]: https://docs.aws.amazon.com/global-infrastructure/latest/regions/aws-regions-availability-zones.html
[cme-cloud]: https://www.cmegroup.com/media-room/press-releases/2026/10/12/cme_group_announcescloudmigrationmilestones.html
[corning]: https://www.corning.com/content/dam/corning/media/worldwide/coc/documents/Fiber/product-information-sheets/PI-1424-AEN.pdf
[cisco-switching]: https://www.cisco.com/c/en/us/td/docs/switches/datacenter/nexus3000/sw/layer2/92X/configuration/guide/b_Cisco_Nexus_3000_Layer_2_Switching_Config_9x/b_Cisco_Nexus_3000_Layer_2_Switching_Config_9x_chapter_0110.html
[arista-l1]: https://www.arista.com/assets/data/pdf/Datasheets/7130B-Datasheet.pdf
[arista-platform]: https://www.arista.com/en/um-eos/eos-7130-platform-layer-1-configuration
[cme-feed]: https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457672396/MDP%2B3.0%2B-%2BIncremental%2BFeed%2BArbitration
[timestamping]: https://docs.kernel.org/networking/timestamping.html
[ptp4l]: https://www.linuxptp.org/documentation/ptp4l/
[phc2sys]: https://www.linuxptp.org/documentation/phc2sys/
[cme-failover]: https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457671413/Fault%2BTolerance
[cme-cod]: https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457216487/
[k8s-cpu]: https://kubernetes.io/docs/tasks/administer-cluster/cpu-management-policies/
[k8s-topology]: https://kubernetes.io/docs/tasks/administer-cluster/topology-manager/
[k8s-memory]: https://kubernetes.io/docs/tasks/administer-cluster/memory-manager/
