# HFT Engineering从入门到入土（7）开源项目学习之Aeron与Chronicle Queue —— 进程间的低延迟通信与事件落盘

一套生产级别的交易系统几乎是不会一个进程集成所有能力的。接入进程收行情，策略进程算信号，风控进程把关，记录进程留档，这些角色拆开之后立刻冒出两个问题。第一个是同机的两个进程怎么把消息递到对方手里最快：走 loopback 的 UDP、走 Unix domain socket，还是共享一块内存？第二个是当天的成交和决策怎么可靠地写到磁盘上，明天程序重启、甚至机器掉电之后，还能从某一条消息接着往下回放。

这一篇只讲两个把这两件事做得很彻底的开源组件：Real Logic 的 Aeron 解决分发，OpenHFT 的 Chronicle Queue 解决事件记录。

Aeron 是 Real Logic 开源的高吞吐低延迟传输库，原生支持共享内存（同机 IPC）和 UDP（跨机），Java 和 C 都有实现，C Media Driver 把性能敏感的收发移出了 JVM。Chronicle Queue 是 OpenHFT 的开源 Java 持久化队列，没有 broker，直接借助操作系统的内存映射文件工作。

## 1. Aeron：通过维护一条有序流实现高效跨进程通信

### 1.1 它要解决什么问题

先看跨机这一侧。UDP 不保证到达，不保证顺序，也不保证不重复，直接拿裸 UDP 写行情分发，丢包重传、乱序归位、慢消费者拖住快发送者这些活全得自己写。TCP 反过来帮你做了这些，代价是阻塞重传、队头阻塞和内核缓冲区里的多次复制，对延迟敏感的链路来说这些开销不划算。Aeron 的做法是保留 UDP 这个轻量载体，把可靠传输的逻辑上移到用户态：接收方发现缺口自己去要，发送方只在接收方允许的窗口内发，整个过程不经过 TCP 的重传队列。

同机进程则是让两个进程映射同一块物理内存，写者追加、读者按位置读，省掉内核缓冲区和一次拷贝。这就是常说的零拷贝：数据只在最终位置写一次，不在发送缓冲区和接收缓冲区之间搬第二趟。Aeron 的 IPC 通道走的就是这条路，而且它把跨机 UDP 的那套位置、窗口、重传语义，在共享内存上复用了一遍，同机通信和跨机通信用的是同一套 API。

### 1.2 Media Driver：一个独立的收发管家

应用程序不直接碰网卡，也不直接管理共享内存文件。Aeron 把收发工作放在 Media Driver 里，它可以嵌进应用进程，也可以作为独立进程常驻，提供 Java 和 C 两种实现。

需要保存消息历史时，还可以配合 Aeron Archive（归档服务）。它是 Aeron 项目提供的录制与回放组件：把指定消息流写入文件，之后按指定位置重新发送历史消息。Archive 的 Java 实现既可以独立运行，也可以嵌入 Java 应用。例如，C++ 业务进程和运行在 JVM 中的 Archive 服务可以连接同一个 C Media Driver；Media Driver 负责消息传输，Archive 负责文件录制与回放。

Driver 内部按职责分成几块：Driver Conductor 处理应用发过来的注册命令（我要发布哪条流、我要订阅哪条流），做名称解析；Sender 线程负责把本地日志里已提交的数据发出去；Receiver 线程负责把收到的报文放进接收日志，需要时发 NAK 和状态消息。应用和 Driver 之间靠一个叫 cnc.dat 的内存映射文件交换命令和计数器，运行时各个位置（写到哪了、发到哪了、收到哪了、消费到哪了）都暴露成计数器，可以用 AeronStat 这类工具直接读。生产环境建议把 driver 目录放在 `/dev/shm`，也就是 tmpfs，避免共享日志落盘带来不必要的 IO。

[![应用进程只和共享内存里的 log buffer 打交道：同机订阅者直接按 position 读，跨机时由 Media Driver 的 Sender 发出、Receiver 收回，Conductor 处理注册命令](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-07/aeron-media-driver.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-07/aeron-media-driver.png)

图里同机读取和跨机收发走的是两套介质，但应用拿到的接口形态一致。发送方创建 Publication（发布对象），通过它向发送日志追加消息；接收方创建 Subscription（订阅对象），通过它读取收到的消息。它们都是应用代码持有的对象，实际的共享内存管理和网络收发由 Media Driver 协调。

### 1.3 log buffer：三个轮转的 term，和 position 这个字节坐标

假设行情接入进程要把消息发送给另一台机器上的策略进程。创建发布对象和订阅对象时，双方要约定通过什么地址通信、订阅哪组消息；如果同一地址上有多个行情发布者，还要区分各自的数据序列。Aeron 用 channel、stream ID 和 session ID 表达这三层信息：

| 标识 | 表达什么 | 在交易系统中的例子 |
| --- | --- | --- |
| channel（通道） | 传输方式、通信地址及相关参数 | `aeron:udp?endpoint=192.168.1.20:9000` 表示使用 UDP，目的端点是策略机器的 `192.168.1.20:9000`；`aeron:ipc` 表示使用本机共享内存通信 |
| stream ID（流编号） | 同一通道内的一组逻辑消息，由应用约定其含义 | 约定 `10` 传行情、`20` 传订单回报，订阅者按编号选择要接收的消息 |
| session ID（会话编号） | 同一通道、同一流编号下的一条具体发布会话，由 Aeron 默认分配，也可显式指定 | 两个独立行情发布会话可以分别使用 `501` 和 `502`，各自维护消息顺序和读写位置 |

例如，两个接入进程各自使用独立的发布会话，把行情发往同一个 UDP 端点；交易网关也使用这个端点发送订单回报。它们之间的关系可以表示为：

```text
channel = aeron:udp?endpoint=192.168.1.20:9000
│
├── stream ID = 10：行情
│   ├── session ID = 501：接入进程 A 的发布会话
│   └── session ID = 502：接入进程 B 的发布会话
│
└── stream ID = 20：订单回报
    └── session ID = 601：交易网关的发布会话
```

策略进程用 `aeron.addSubscription(channel, 10)` 订阅行情时，如果没有设置 session 过滤条件，就可以同时收到 `501` 和 `502` 的数据。Subscription 为每条会话流提供一个 Image（接收视图），每个 Image 分别维护自己的消费位置。Aeron 保证同一会话内部的消息按序交付；两个会话之间的消息先后次序，则要由业务自己定义。图中的行情和订单回报只是应用对编号的约定，Aeron 本身不解析这些业务含义。

session ID 标识的是发布会话，而不是进程。一个进程可以创建多个独立会话；连接同一个 Media Driver、通过 `addPublication` 创建的多个普通发布对象（`ConcurrentPublication`），在通道、流编号和相关配置兼容时，也可以共享底层发布会话。

发送端为每个独立发布会话维护一份 log buffer（日志缓冲区）。它是一个内存映射文件，由三个等长的 term（连续的日志分区）和末尾的元数据组成。每个 term 保存一段连续的消息帧；一帧包含 Aeron 帧头和消息数据，帧头携带会话、流编号以及帧所在位置等信息。写者向当前活动 term 追加帧，写满后切换到下一个 term。旧 term 暂时保留，供读者读取或发送端重传，之后再清理并复用这块空间。

term ID 是逻辑分区的编号，每切换到下一个 term 就递增；term offset 是分区内部的字节偏移。物理空间虽然只有三个分区、反复轮转，逻辑编号仍然继续增长。term 长度必须是 2 的幂，范围从 64KB 到 1GB；C Media Driver 的默认 UDP term 是 16MB、IPC term 是 64MB，都可以按流单独配。单条应用消息最长不超过 16MB 和 term 长度八分之一两者中的较小值，超过 `maxPayloadLength`（单帧能承载的最大消息数据长度）的消息会被 Aeron 自动分片。

position（流内字节位置）把已经跨过的 term 长度和当前 term offset 合在一起，表示同一发布会话内连续增长的位置。例如，一个从 position 0 开始的会话使用 64KiB 的 term：跨过第一个 term 后，第二个 term 内偏移 128 字节的位置，就是 `65536 + 128 = 65664`。切换 term 会让 term offset 从零重新开始，但 position 会继续向前推进。

因此，判断 position 属于哪里，要同时看 channel、stream ID 和 session ID。图中的会话 `501` 和 `502` 都可以有 `position = 4096`，但它们指向各自数据序列中的位置。position 的连续性只存在于同一发布会话中，不能拿两个独立会话的 position 大小判断消息发生的先后。

**position 数的是传输字节，不是业务消息条数。** 每帧有 32 字节的 Aeron 帧头，帧头加消息体的总长度还要向上对齐到 32 字节的整数倍。例如，一条未分片的 90 字节消息，帧头和消息体共 `32 + 90 = 122` 字节，对齐后占 128 字节；如果这次追加没有触发 term 末尾的额外填充，position 就增加 128。流的最大可达 position 由 term 长度决定。

帧的长度字段还承担着发布标记的作用。写者先写负长度占位，写完帧头和消息体后，再以 release 语义原子写入正长度。读者以 acquire 语义读取长度，看到正长度才访问帧的其他内容；这样，完成标记之前写好的数据也对读者可见。

用伪代码表示，`N` 是帧头加消息体的总长度，尚未包含对齐填充：

```text
// 写者：预留空间，负长度标记先于后续内容写入可见
frame = reserve_and_mark_incomplete(N)    // frame.length = -N
frame.type = DATA
write(frame.payload, message)
store_release(frame.length, N)            // 最后发布正长度

// 读者：查看当前 position 处的帧
frame = frame_at(position)
n = load_acquire(frame.length)
if n <= 0: return                         // 尚未完成，停在原地
if frame.type != PAD:
    handle(frame.payload)                // 只有数据帧交给业务
position += align_up(n, 32)               // DATA、PAD 都推进位置

// Driver：写者已退出，且满足解堵条件时
n = load_acquire(frame.length)
if n < 0:
    rebuild_as_padding(frame)            // 重建帧头，设置 type = PAD
    store_release(frame.length, -n)       // 再发布正长度
```

PAD（padding，填充帧）让读者跳过未完成消息，继续读取后面的帧。这里必须先把类型改成 PAD，再发布正长度，否则读者会把半条消息当成完整数据。Driver 的 publication 解堵超时默认是 15 秒，具体触发还取决于发布对象的类型和生命周期状态。

### 1.4 IPC：同机共享一份日志

IPC 模式下，发布者和订阅者映射的是同一份 log buffer 文件，Driver 不逐条转发消息体，只负责更新发布者允许写到哪里。应用侧两个发送方法：`offer()` 把已经编码好的缓冲区复制进日志，`tryClaim()` 直接预留一段日志空间，让你原地编码再 `commit()` 发布，省掉那次复制。`tryClaim` 适合热路径上长度已知的短消息，代价是必须在同一个短调用里 commit 或 abort，把预留空间挂给异步线程是事故来源。默认的 ConcurrentPublication 允许多个发送者线程争用尾指针，靠原子操作协调；如果一条发布句柄从头到尾只由一个线程写，就可以用 ExclusivePublication，这就是单写者原则：单一写者推进位置，省掉多写者之间的原子争用，前提是你真的能保证只有一个线程碰这个句柄。

`offer()` 的返回值不是布尔，业务代码必须按分支处理：



```
> 0      已写入本地日志，返回新的 position
-1       还没有订阅者连接，属正常状态，等待即可
-2       背压：写超了允许窗口，稍后重试或放缓自己的节拍
-3       管理动作（正在轮换 term），下一轮重试同一条
-4       publication 已关闭，走生命周期恢复
-5       流到达最大 position，需要新建会话衔接
```

背压（back pressure）在这里的意思是：下游消费不动了，允许写的窗口收窄，`offer` 返回 -2，压力沿着链路往上游传。Aeron 不替你决定丢还是等，这是业务语义的事。买一价这种 "只保留最新状态" 的字段可以合并覆盖；逐笔成交、订单意图这种每条都有独立含义的消息，合并就会改变结果，只能有界重试或者让上游降速。

IPC 不用状态消息，Driver 周期性地取订阅者消费位置的最小值，加上一个窗口更新发布上限。跟着流走的订阅者叫 tether；一个订阅者长期落在窗口后四分之一，超时后会被暂时移出约束集合休息一阵再重新加入，它的落后不再卡住整个窗口。这意味着一个挂死的旁路观察者不会永久拖住主链，但反过来，如果你要求某个记录进程必须每条都跟上，就得让它留在约束集合里，不能指望 "它总会自己追上来"。

### 1.5 UDP：乱序落位、NAK 补缺口、状态消息做流控

跨机时两端各有自己的 log。发布者先写本地日志，Sender 读已提交的帧发 UDP；接收端 Receiver 把报文按帧头里的 term ID 和 term offset 放到接收日志的对应位置，最后由应用从接收日志顺序消费。UDP 报文可能乱序到达，也可能丢，Receiver 维护两个水位：rcv-hwm 是见过的最高位置，rcv-pos 是连续可达的最高位置。中间有缺口时，rcv-pos 停在缺口前，后面先到的数据照样摆在自己的位置上，不挡路。

[![发送端日志经 Sender 发出 DATA 帧，Receiver 按 term offset 落位；发现缺口发 NAK 要求重传，消费位置和接收窗口通过 Status Message 回报给 Sender](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-07/aeron-udp-reliability.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-07/aeron-udp-reliability.png)

NAK（Negative Acknowledgement，负确认）就是接收方主动向发送方要缺失的那一段：发现缺口立刻发一个 NAK 帧，里面写明 term ID、起始 offset 和长度，Sender 收到后把对应数据帧重发一次。这和 TCP 的主动重传方向相反，是接收驱动的补救。多播场景下 NAK 会带一点延迟发送，避免一群接收者同时要同一段造成风暴；发送方重传后会在短时间内忽略针对同一帧的重复 NAK。

流控靠 Status Message（SM）反向回报：接收方把自己消费到的 term ID、offset 和愿意再收多少字节的窗口周期发回 Sender，Sender 只在窗口内推进。默认接收窗口 128KB。多个接收者时怎么取这个边界，由流控策略决定，多播默认是 `max`：取所有接收者里最靠前的消费位置，跟得上的人决定节奏，落后太久的接收者会被甩开、之后从最新点重新加入。`min` 策略反过来，最慢的人定速；`tagged` 只让打了标签的关键接收者参与限速。把 "可靠 UDP" 理解成 "所有订阅者永远不丢" 是最常见的配置错误：可靠只在有限的重传窗口内成立，长时间离线的历史数据要靠记录组件补，而不是指望 Sender 一直替你存着。

### 1.6 Archive：把流归档到磁盘上，按 position 重放

Media Driver 的 log buffer 会轮转复用，早先的消息最终会被覆盖。Archive 为需要保留的消息流建立 Subscription，持续把收到的数据帧追加到文件中。回放时，它从文件中读出历史帧，通过自己的 Publication 重新发送，恢复程序仍然用 Subscription 接收：

```text
录制：业务 Publication → Archive 的 Subscription → 磁盘文件
回放：磁盘文件 → Archive 的 Publication → 恢复程序的 Subscription
```

这两条数据路径中的消息传输仍由 Media Driver 支持。应用通过 Archive 客户端发送“开始录制”“查询录制记录”“从某个位置回放”等控制请求；录制和回放的文件读写由 Archive 服务执行。

一条发布会话录下来的数据称为一份 recording（录制记录），由 recording ID（录制编号）标识，可以分布在多个段文件中。catalog（录制目录）保存这些记录的元数据，包括通道、stream ID、session ID、起止 position 和文件分段信息。请求回放时，recording ID 指定“读哪份记录”，position 指定“从这份记录的哪个位置开始读”。Archive 保留了原始帧与 position 的对应关系，所以能够从已录制范围内的有效帧边界开始重放。

例如，策略进程短暂退出，而 Archive 仍在录制，策略重启后就可以请求补回这段消息。需要追上实时流时，可以配合回放合并功能，先读取历史，再切换到实时接收。

### 1.7 Cluster：一句话带过

Aeron Cluster 在 Archive 之上实现了 Raft 共识：多个节点把客户端输入序列化成一份复制日志，按相同顺序应用到各自状态机，快照走 Archive 的读写接口，2 个以上节点即可容错。它解决的是 "多个副本保持一致状态"，和传输、留档是三个层次，需要高可用服务（比如内存订单簿）时再单独评估，本篇不展开。

### 1.8 Aeron 在 HFT 里放在哪

同机的进程间分发是 Aeron IPC 最适合接入的位置：接入进程把行情扇出给策略、风控、记录几个订阅者，省掉 socket 来回，延迟就是一次共享内存读写。跨机机房内的行情多播、网关与撮合服务器之间的可靠链路，是 UDP 传输的典型位置，配合 `min` 或 `tagged` 保证关键消费方不掉队。

不该用的场景同样明确。单进程内部的线程间传递，第 03 篇讲过的 SPSC 环形队列更短，不需要 Driver 这一层。对外的公网请求和简单 RPC，UDP 重传模型帮不上忙。只想要一条能长期保存的记录而没有人实时消费，直接上 Archive，不要靠调大 term buffer 硬撑。另外 Aeron 不提供消息路由、持久化查询和跨机复制，这些超出了它的设计边界。

## 2. Chronicle Queue：把事件追加进内存映射文件，读完不删

### 2.1 它要解决什么问题

换一个问题：策略重启之后，怎么把今天上午的行情和自己的决策重新走一遍，把内存里的订单簿和状态恢复出来？普通队列读完即删，做不到；数据库能存但写入路径太重，每条事件都要经过索引和事务，扛不住行情的突发速率。Chronicle Queue 的思路很直接：磁盘比内存便宜，那就把事件一条一条追加到文件里，靠操作系统的页缓存承接读写速度，读者读走不删除，谁想从哪读就从哪读。

它是一个没有 broker 的 Java 库，进程直接操作本地文件，不需要中间服务器。开源版本只提供 Java 实现，跨语言（含 C++）和跨机复制属于商业产品范围，选型时这是硬边界。

### 2.2 mmap 和页缓存：写文件为什么能接近写内存

mmap把一个磁盘文件的一段映射进进程地址空间，程序写这段地址，写的是文件对应的物理页，内核在背后把脏页择机写回磁盘，应用不需要自己调用 write 系统调用搬数据。页缓存（page cache）是内核为文件在内存里留的缓存页，刚写进去的数据还在页上，同机另一个进程映射同一文件就能直接读到，不必等落盘。Chronicle Queue 全部数据 off-heap（堆外）存放，JVM 堆上几乎不占对象，也就避开了 GC 对热路径的打扰。

不过，建立映射时，很多页面还没有准备好。写线程首次访问这些页，会触发缺页异常（page fault），由内核准备物理页、补齐页表中的地址映射，再回来继续写。无需磁盘 I/O 就能处理的叫次缺页（minor fault）；需要从磁盘读入数据的叫主缺页（major fault）。即使追加的是新文件、不用读取旧数据，首次写新页仍有开销。行情突发时连续跨过大量新页，这些停顿就会进入写入延迟。[Linux 缺页统计定义](https://man7.org/linux/man-pages/man2/getrusage.2.html)

Chronicle Queue 对应的优化叫 **Pretoucher（预触页器）**：让后台线程跟着当前写入位置，提前访问前方一段即将写到的映射页。主动把首次访问引起的缺页提前处理，称为 **prefault**。在同一进程共享同一段映射的情况下，后台线程可以提前准备页缓存和可写的页表映射，减少业务写线程临时承担的工作。核心动作可以简化成：

```text
后台定期执行：
    终点 = 当前写入位置 + 提前触页的窗口大小
    对尚未触碰、位于终点之前的每一页：
        原子比较并交换(页首的 8 字节, 期望值=0, 新值=0)
```

这里的“0 换成 0”会保留已有数据：原值为零就仍写零，非零就不修改。对前方尚未使用的零值区域，成功的比较并交换会形成写访问，提前承担首次写入的缺页处理。只读一下页面，仍可能把首次写入的缺页留给业务线程。窗口会随写入位置向前移动，旧版实现还会根据最近的写入推进量调整提前量，因而不必一次触碰整个队列文件。[PretoucherState 源码](https://github.com/OpenHFT/Chronicle-Queue/blob/7b6d30dea84675d0965e9af47e50475cac239be7/src/main/java/net/openhft/chronicle/queue/impl/single/PretoucherState.java)、[Linux 读写预触页语义](https://man7.org/linux/man-pages/man2/madvise.2.html)

预触页把工作提前交给后台线程，主要收益是减少写线程的延迟尖峰。写入突然超过预触窗口，或者已触碰的页被内核回收，写线程仍会遇到缺页。

### 2.3 append-only、记录头与提交约定

队列只允许往末尾追加，没有插入和删除。每条记录前面有一个 32 位头：低 30 位存消息体长度，第 31 位是 NOT\_COMPLETE（未完成）标志，第 30 位是 META\_DATA 标志（元记录和文件结束标记用）。写入顺序和 Aeron 帧如出一辙：先把头写成 0（未发布），写完完整消息体，再用 CAS（Compare-And-Swap，比较并交换，一种无锁原子操作）把头替换成最终长度。读者只认 "头非零且不带未完成位" 的记录，看到半个写坏的文档就跳过去。进程在提交前被 kill，留下的未发布头不会被读者当成有效记录，恢复后的写者重新定位到末尾清理即可。

[![一个持锁的 Appender 只追加写今日 .cq4 文件，多个 Tailer 各自读今日或往日文件，写锁放在 metadata.cq4t，映射页默认 ASYNC 由内核择机写回磁盘](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-07/chronicle-queue-layout.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-07/chronicle-queue-layout.png)

写锁放在 metadata.cq4t 这个表存储文件里，多个进程通过 CAS 竞争同一把锁，一个进程内也不能让多个线程同时用同一个 appender。持锁的文档作用域越短越好，耗时计算、外部请求都放到取得写作用域之前完成。

### 2.4 滚动文件、稀疏索引和各自独立的 Tailer

文件按 roll cycle 滚动，开源版默认 FAST\_DAILY，按 UTC 日界切一个新文件，旧文件末尾由写者原子写入 EOF 标记，告诉所有人别再写它、去新文件。除了按天，还可以按小时、分钟等粒度滚动，创建时定下来就不能改。

文件内部是一串记录，但随机跳到第 N 条不能靠从头数，所以有一套稀疏索引：每隔 256 条记录记一个位置（两级索引先定位到附近块，再顺序扫到目标）。逻辑 index 由 cycle 号和 cycle 内序号组成，DAILY 下是 32 位序号拼在 cycle 号高位，它不是文件字节偏移。保存恢复游标时存完整 index，`moveToIndex()` 靠索引选文件并定位。

Tailer 是读者句柄，和 Aeron 的订阅者有个本质区别：读走不删除。每个 Tailer 自己维护位置，`toStart()` 从头、`toEnd()` 只跟新消息、`moveToIndex()` 随机定位，几个 Tailer 各读各的，一个读者消费不会让记录消失。具名 Tailer 把自己的进度存在队列里，重启后从上次位置继续，这对恢复场景很实用。

[![写者先把记录头写 0，写完消息体再用 CAS 写入最终长度；读者只读头已完整的记录，提交前崩溃的记录会被跳过](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-07/chronicle-queue-commit.png)](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/main/hft/assets/hft-07/chronicle-queue-commit.png)

### 2.5 sync () 到底同步了什么，持久化边界在哪

`appender.sync()` 听上去像 "确保落盘"，实际取决于 SyncMode。默认 ASYNC，底层调用 msync 的 MS\_ASYNC 标志，只提醒内核这些页可以写回，不等待完成；显式设成 SYNC 才用 MS\_SYNC 阻塞到所选范围写盘，NONE 则跳过。也就是说，只看到代码里调了一次 `sync()`，不能推出 "数据已落盘" 这个结论。

还要看清同步范围：`sync()` 作用于当前映射段，不等于覆盖目录项、所有历史 cycle 文件和业务快照的全局提交；设备自身写缓存的掉电保障也是另一层。进程被 kill -9 时操作系统还活着，页缓存通常还在，已提交记录不丢；整机断电则把这个前提拿掉。文件保留默认是永久的，归档和删除策略要自己通过 StoreFileListener 挂上去。另外开源版不支持 NFS、SAN 这类网络文件系统，因为它们提供不了 mmap 需要的原子原语。

### 2.6 Chronicle Queue 在 HFT 里放在哪，什么时候别用

它适合做本机的事件留档：一个 Java 记录服务订阅 Aeron 流，把行情、决策、发送结果逐条写进队列，实时 Tailer 做监控，另一个 Tailer 供事后回放重建状态，研究进程随时挂上来读全量历史。同机多读者共享一份档案，不用给每个人复制一份。

不该用的地方也要说清。它不做跨机复制，容灾要另想办法；开源版没有 C++ 原生客户端，纯 C++ 栈要么经过一个 Java 记录服务中转，要么评估商业版；它是顺序日志不是数据库，按条件海量点查、聚合分析交给下游系统；把默认 ASYNC 当成掉电安全，是最容易犯的错，真要承诺落盘就得显式 SYNC 并接受它的阻塞成本。

## 3. 放在一起看：Aeron 管高效通信，Chronicle Queue 负责归档

两个组件解决的问题不在一层。Aeron 是传输层：数据在内存日志里轮转，快进快出，解决 "此刻怎么低延迟地把消息送到"，加 Archive 才获得持久录制和按 position 重放的能力。Chronicle Queue 是存储层：数据躺在本地追加文件里，读完不删，解决 "事后怎么查、怎么重放"，它自己不负责实时分发。



| 维度     | Aeron（含 Archive）                    | Chronicle Queue         |
| ------ | ----------------------------------- | ----------------------- |
| 核心问题   | 进程间 / 跨机的有序可靠分发                     | 本机事件的持久留档与回放            |
| 介质     | 共享内存（IPC）、UDP；Archive 落磁盘段文件        | mmap 本地 .cq4 文件         |
| 数据生命周期 | log buffer 轮转复用；Archive 按保留期管理段文件   | append-only，读完不删，保留策略自理 |
| 消费模型   | 订阅者推进位置，慢者受流控约束                     | Tailer 各自独立位置，读走不删      |
| 可靠性补救  | NAK 重传 + 窗口流控                       | 记录头 CAS 提交 + 崩溃恢复       |
| 运行时    | Java 客户端 + C/Java Driver，可无 JVM 热路径 | 开源版为 Java，堆外存储          |
| 跨机     | 原生 UDP 单播 / 多播 / MDC                | 开源版不支持，复制属商业版           |

一个常见组合是：C++ 接入和策略进程走 Aeron IPC/UDP 完成实时链路，Archive 录制原始传输流用于故障恢复，旁路 Java 录包服务把面向运营的业务事件写进 Chronicle Queue 供审计和研究。两者也可以只取其一：已经用 Aeron 分发消息，只需要保存原始流以便重放，可以直接配合 Archive；不需要实时分发、只想要一份本地档案，Chronicle Queue 单独用也行。选型时先回答两个问题：这条消息现在就要低延迟送到谁手上，还是要留到以后再读？答案决定它先进 Aeron，还是先进队列文件。

## 参考资料



* Aeron 项目仓库与官方文档：[github.com/aeron-io/aeron](https://github.com/aeron-io/aeron)、[aeron.io 文档站](https://aeron.io/docs/)。

* log buffer、term 与位置语义：[Log Buffers & Images](https://aeron.io/docs/aeron/log-buffers-images/)、[Understanding Aeron Position](https://aeron.io/docs/aeron/aeron-understanding-position/)、[Publications & Subscriptions（offer/tryClaim 返回码与背压）](https://aeron.io/docs/aeron/publications-subscriptions/)。

* 通道、流编号、发布会话与订阅关系：[Channels, Streams and Sessions](https://aeron.io/docs/aeron/aeron-channel-stream-session/)、[Channel Configuration（session-id 参数）](https://github.com/aeron-io/aeron/wiki/Channel-Configuration)、[Client Concurrency Model（发布对象共享与独立会话）](https://github.com/aeron-io/aeron/wiki/Client-Concurrency-Model)。

* 帧的发布与读取（固定源码版本）：[HeaderWriter](https://github.com/aeron-io/aeron/blob/37db10bebd5e0f0acf3e9afa6902072eeb1ed23c/aeron-client/src/main/java/io/aeron/logbuffer/HeaderWriter.java)、[FrameDescriptor](https://github.com/aeron-io/aeron/blob/37db10bebd5e0f0acf3e9afa6902072eeb1ed23c/aeron-client/src/main/java/io/aeron/logbuffer/FrameDescriptor.java)、[TermReader](https://github.com/aeron-io/aeron/blob/37db10bebd5e0f0acf3e9afa6902072eeb1ed23c/aeron-client/src/main/java/io/aeron/logbuffer/TermReader.java)。

* 未完成帧的解堵：[TermUnblocker](https://github.com/aeron-io/aeron/blob/37db10bebd5e0f0acf3e9afa6902072eeb1ed23c/aeron-client/src/main/java/io/aeron/logbuffer/TermUnblocker.java)、[IpcPublication](https://github.com/aeron-io/aeron/blob/37db10bebd5e0f0acf3e9afa6902072eeb1ed23c/aeron-driver/src/main/java/io/aeron/driver/IpcPublication.java)、[默认超时配置](https://github.com/aeron-io/aeron/blob/37db10bebd5e0f0acf3e9afa6902072eeb1ed23c/aeron-driver/src/main/java/io/aeron/driver/Configuration.java)。

* Media Driver 结构与部署：[Media Driver](https://aeron.io/docs/aeron/media-driver/)。

* 流控、NAK 与协议帧：[Flow and Congestion Control（wiki）](https://github.com/aeron-io/aeron/wiki/Flow-and-Congestion-Control)、[Transport Protocol Specification（wiki）](https://github.com/aeron-io/aeron/wiki/Transport-Protocol-Specification)。

* Archive 的组成、录制与回放：[Aeron Archive Overview](https://aeron.io/docs/aeron-archive/overview/)、[Archive Basic Sample](https://aeron.io/docs/aeron-archive/basic-sample/)、[Working with Recordings](https://aeron.io/docs/aeron-archive/working-with-recordings/)。

* Cluster：[Aeron Cluster Overview](https://aeron.io/docs/aeron-cluster/overview/)。

* Chronicle Queue 项目仓库与 README：[github.com/OpenHFT/Chronicle-Queue](https://github.com/OpenHFT/Chronicle-Queue)。

* 滚动周期与索引：[RollCycles 源码](https://github.com/OpenHFT/Chronicle-Queue/blob/develop/src/main/java/net/openhft/chronicle/queue/RollCycles.java)；同步模式默认值见 [MappedFile 源码](https://github.com/OpenHFT/Chronicle-Bytes/blob/develop/src/main/java/net/openhft/chronicle/bytes/MappedFile.java)。

* msync 语义：[msync (2) 手册](https://man7.org/linux/man-pages/man2/msync.2.html)。

* Chronicle 商业版与开源版边界：[Chronicle Queue 产品页](https://chronicle.software/products/chronicle-queue/)。
