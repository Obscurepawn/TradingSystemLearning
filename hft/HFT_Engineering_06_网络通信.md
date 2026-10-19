# HFT Engineering 06：网络通信——从典型内核网络栈到ByPass Kernel

行情从网卡到达策略，订单从程序发往交易所，中间会经过协议处理、缓冲区复制和队列等待。降低网络延迟，就是减少这条链路中不必要的工作与等待，让关键消息尽早进入业务处理。

可以把整条链路想象成一条**带多级暂存仓的流水线**：消息每经过一个环节，要么被加工（协议解析、校验、加头），要么被搬运（复制、DMA），要么在等待（中断、排队、调度、凑批）。网络优化的本质就是三件事：

- **少等**：不要为了凑批、攒包、睡眠唤醒而白等；
- **少搬**：减少数据复制和跨核转移；
- **少打扰**：减少中断、系统调用和上下文切换。

这个章节我们从传统网络栈的流程、优化点探讨到bypass kernel网络栈。全章分四部分：

1. **传统网络栈的收发流程**：一条消息从网卡到策略（或反向）到底经过了什么，时间花在哪；
2. **保留内核网络栈时的 16 个优化点**：普通 socket 的调优，以及基于 UDP 的可靠传输优化；
3. **用户态收发与硬件旁路**：ExaNIC、DPDK、RDMA、Solarflare 四条绕开内核的路线；
4. **Crypto 行情中的 WebSocket 与 JSON**：当入口是加密长连接 + 文本协议时，额外的延迟来自哪里、怎么优化。

## 1. 传统网络栈的收发流程

普通 Linux 程序通过 socket 调用 `send()`、`recv()`。TCP 提供可靠、有序的字节流，UDP 提供数据报传输，两者都可以通过 socket 使用。下面这张图左侧是发送，从应用向下读到网卡；右侧是接收，从网卡向上读到应用。

![Linux 网络栈：TCP/UDP 收发流程概览（左发送右接收，含队列与阻塞/丢包点）](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/e1a13d0410c2b03463ea6adf436e5335d53ea679/hft/assets/hft-06/network_stack_overview.png)

对照这张图，一次典型的发送是：应用调用 `send()` → 数据复制进内核缓冲区 → TCP/UDP 加传输层头 → IP 层加 IP 头、查路由、过防火墙 → 填 MAC 头 → 进入网卡发送队列 → 网卡通过 DMA 取走数据并发到线上。接收方向相反：网卡把帧 DMA 进内存 → 中断通知 CPU → 内核软中断收包、逐层剥头 → 数据挂到 socket 的接收队列 → 应用调用 `recv()` 复制走。

对低延迟交易程序，主要成本集中在三处：

- **进入内核与等待调度。** `send()`、`recv()` 等系统调用要切换执行特权级。线程若阻塞，还要经历睡眠、唤醒和重新调度。通俗说，就是线程在"睡着等门铃 → 被叫醒 → 重新排队上 CPU"，这一睡一醒本身就是微秒级的开销，而且唤醒时机不稳定。
- **处理器参与协议与队列处理。** 驱动、TCP/UDP 协议处理、路由、过滤和各级队列管理都消耗处理器时间。中断处理、协议栈处理与业务线程之间的核间通信和等待，还会带来延迟波动。数据即使本身到得很快，被另一个核心"转手"一次，也要付出缓存失效和跨核通知的代价。
- **额外的payload复制。** 普通复制式 socket 发送会把应用数据复制到内核缓冲区，接收会把内核中的数据复制到应用缓冲区。应用再拼接消息、转交线程时，还可能增加复制。每多一次复制，就多一次内存读写和缓存占用。

网卡与主机内存之间的数据搬运，已经可以由直接内存访问（Direct Memory Access，DMA）完成，无须处理器逐字节搬运；主要差别在于搬到哪里，以及之后还要经过多少软件处理、等待和复制。协议栈各层可以复用同一份payload，并非图中每经过一层就复制一遍。[Linux DMA 指南](https://docs.kernel.org/core-api/dma-api-howto.html)、[AMD 传统网络栈与旁路对比](https://docs.amd.com/r/en-US/ug1586-onload-user/Contrasting-with-Conventional-Networking)

> **本节要点**：传统栈不是某一帧特别慢，而是链路上有大量"为通用性和吞吐而设计"的等待与搬运。后面的优化分别对应三条主线——让数据**少等**（中断、轮询、Nagle）、让 CPU **少搬**（零拷贝、批量系统调用）、让处理**少绕路**（RSS 绑核、卸载给网卡）。

## 2. 保留内核网络栈时，怎样降低延迟

内核旁路之前，普通 socket 还有不少优化空间。低延迟的重点是让关键消息及时进入处理器、减少核间通信，并避免为了吞吐而长时间等待。

下面 16 个手段可以先按"解决什么问题"分成八组：

| 分组 | 小节 | 解决的核心问题 |
| --- | --- | --- |
| 让数据更早被 CPU 看到 | 2.1 中断合并、2.2 忙轮询、2.3 RSS 与中断亲和性 | 缩短"包到了网卡"到"CPU 开始处理"之间的等待和绕路 |
| 让小消息按预期时机发出 | 2.4 TCP_NODELAY、2.5 MSG_MORE / TCP_CORK | 控制内核要不要替你"攒小包" |
| 减少系统调用与内存操作 | 2.6 sendmsg、2.7 recvmmsg、2.8 复用接收缓冲区 | 同样的活少进几次内核、少申请几次内存 |
| 把系统调用批量化 | 2.15 io_uring | 共享环形队列批量提交收发，热路径甚至不进内核 |
| 缓冲与队列的取舍 | 2.9 SO_RCVBUF、2.10 有界待发队列 | 抗短时突发，但不让过时消息无限堆积 |
| 把活交给网卡 | 2.11 校验和卸载、2.12 TSO、2.13 GRO | CPU 不做的，就不消耗 CPU 时间 |
| 彻底去掉一次复制 | 2.14 MSG_ZEROCOPY | 发送时网卡直接读用户内存 |
| 调整可靠传输策略 | 2.16 KCP | 在两端可控的 UDP 链路上缩短丢包恢复等待 |

下面的 C 片段面向 Linux，使用已有 socket `fd`，省略头文件与建立连接过程。`check()` 检查系统调用结果，`require()` 检查必要条件，`send_all()` 在阻塞 TCP 上处理短写与信号中断。

```c
static long check(long rc) {
    if (rc < 0) { perror("socket demo"); exit(EXIT_FAILURE); }
    return rc;
}

static void require(bool condition, const char *message) {
    if (!condition) { fprintf(stderr, "%s\n", message); exit(EXIT_FAILURE); }
}

static void send_all(int fd, const char *data, size_t size, int flags) {
    while (size) {
        ssize_t n = send(fd, data, size, flags | MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR) continue;
        check(n);
        require(n > 0, "send made no progress");
        data += n;
        size -= (size_t)n;
    }
}
```

网卡命令中的 `eth0`、CPU 编号和中断号按目标机器替换。

### 第一组：让数据更早被 CPU 看到

### 2.1 中断合并

**问题是什么**：在中断驱动的收包路径中，网卡先把数据 DMA 到接收缓冲区，再通过中断通知处理器。如果每来一个包就中断一次，包速率高时 CPU 会被中断淹没。于是网卡默认会"攒一攒"：等包数达到门槛或定时器到期，再集中通知。这就像快递员选择"攒一筐再送上门"而不是"每件都敲门"——**门铃次数少了，但最先到的那件在门口多等了一段时间**，尾延迟也可能随之增大。

**怎么做**：缩短定时等待、降低包数门槛，能让处理器更早开始收包，代价是更频繁的中断。[ethtool 合并配置](https://docs.kernel.org/networking/ethtool-netlink.html#coalesce-get)

先记录当前配置，再尝试关闭接收方向的自适应合并和定时等待：

```bash
ethtool -c eth0
sudo ethtool -C eth0 adaptive-rx off rx-usecs 0 rx-frames 1
ethtool -c eth0
```

`rx-usecs 0` 表示不设置这项定时等待，`rx-frames 1` 把帧数门槛设为 1（等价于"来一个就通知"）。驱动可能只支持其中部分参数；最后一行查看实际结果，比较结束后恢复先前记录的值。

> 注意这是用**中断频率换延迟**：低延迟交易机能接受更高的中断开销；吞吐型机器则通常保留适度合并。

### 2.2 SO_BUSY_POLL：忙轮询

**问题是什么**：普通阻塞接收在没有数据时会让线程睡眠，之后经历网卡中断、内核收包和线程唤醒。Linux 的 NAPI 是驱动从网卡队列取包并交给协议栈的处理机制：驱动从网卡队列批量取包、再交给协议栈处理，平时由中断"叫醒"它。`SO_BUSY_POLL` 的思路很直接——**与其睡下等门铃，不如让线程站在门口主动等一会儿**：接收线程进入 `recv()` 时，可以先在内核中主动轮询对应的 NAPI 实例。如果包在这段时间内到达，就有机会直接处理，减少等待中断和重新调度的时间。

**代价**：这项优化用持续占用处理器换取较低的等待延迟，TCP/UDP 协议处理和接收复制仍在内核中完成。另外要注意，只在用户态反复调用非阻塞 `recv()`，不会自动启用 NAPI 忙轮询，必须通过这个 socket 选项开启。[Linux NAPI 忙轮询](https://docs.kernel.org/networking/napi.html#busy-polling)

```c
int us = 50;
check(setsockopt(fd, SOL_SOCKET, SO_BUSY_POLL, &us, sizeof(us)));
char buf[2048];
ssize_t n = check(recv(fd, buf, sizeof(buf), 0));
```

`50` 的单位是微秒，限制的是忙轮询时间，超过后仍按 socket 的阻塞规则等待。socket 从支持该功能的网卡收到数据、关联到对应 NAPI 实例后，才有实际的忙轮询对象。[SO_BUSY_POLL 接口](https://man7.org/linux/man-pages/man7/socket.7.html)

### 2.3 RSS 与中断亲和性

**问题是什么**：一块现代网卡有多个接收队列，可以把流量分散到多个 CPU 核心并行处理，避免单核成为瓶颈。接收侧扩展（Receive Side Scaling，RSS）让网卡对报文的源、目的地址及端口等字段做哈希，再查映射表选择接收队列；配置稳定时，同一条流会落到同一个队列（类似超市的"熟客固定通道"，同一会话每次都排同一队，缓存热度得以保留）。中断亲和性再决定由哪个核心处理该队列的中断。

**原则**：让收包、协议处理和业务线程尽量靠近，可以减少跨核通知、缓存数据转移和额外排队。如果硬件队列已经合理分配，再用软件把每个包转交给另一个核心，就会增加一次核间通信。单核能承担整条处理链时，可以尽量在同一核心完成；负载超过单核能力时，再按流拆分到多个核心。[Linux 网络栈扩展与队列分配](https://docs.kernel.org/networking/scaling.html)

单接收核实验可以先把 RSS 分配集中到队列 0，再绑定这个队列的中断。下面假设从 `/proc/interrupts` 找到的队列 0 中断号是 `42`，目标核心是 CPU 2：

```bash
ethtool -x eth0                         # 记录原 RSS 分配
sudo ethtool -X eth0 equal 1             # RSS 哈希桶集中到队列 0
rg 'eth0' /proc/interrupts               # 找到队列对应的中断号
echo 2 | sudo tee /proc/irq/42/smp_affinity_list
cat /proc/irq/42/effective_affinity_list # 确认实际生效的 CPU
```

网卡队列在 `/proc/interrupts` 中的名称由驱动决定。若 `irqbalance` 正在管理该中断，还要避免它把亲和性改回。RSS 和中断亲和性的接口分别见 [ethtool](https://man7.org/linux/man-pages/man8/ethtool.8.html) 与 [Linux IRQ affinity](https://docs.kernel.org/core-api/irq/irq-affinity.html)。

### 第二组：让小消息按预期时机发出

### 2.4 TCP_NODELAY

**背景：内核默认会替你"攒车"**。Nagle 算法为了减少小包，会在连接上已有未确认数据时，暂存后续不足一个完整 TCP 段的数据，等确认到达或数据积累到足够再发——像一辆没坐满不发车的班车。对几十字节的订单，这种合并会把本来已经准备好的消息留在发送缓冲区。

**做法**：`TCP_NODELAY` 关闭这项等待，让小消息尽早提交发送；代价是报文数量增加，拥塞窗口、接收窗口和发送队列仍会限制实际发送时机（也就是它只能让消息"别在本地攒着"，不能突破网络本身的发送限制）。[Nagle 算法](https://www.rfc-editor.org/rfc/rfc9293.html#section-3.7.4)、[TCP_NODELAY 接口](https://man7.org/linux/man-pages/man7/tcp.7.html)

```c
int one = 1;
check(setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one)));
send_all(fd, "BUY\n", 4, 0);
```

### 2.5 MSG_MORE 与 TCP_CORK

Nagle 是**内核猜**你后面还有数据而替你攒包；这两个接口则是**应用主动明确地告诉内核**"这条消息后面还有数据"。在 TCP 发送中，内核可以先保留不足一个完整段的尾部，把紧接着提交的头部、消息体合并发送，减少小包数量。两者区别在于作用范围：`MSG_MORE` 随每次发送传入，`TCP_CORK` 则是跨调用保留的 socket 状态。

低延迟的用法是**只合并且一次写调用中已经准备好的内容**，最后一段及时清除标记或解除 cork，避免等待下一笔尚未产生的订单——否则"为合并而等待"反而制造了延迟。[MSG_MORE 接口](https://man7.org/linux/man-pages/man2/send.2.html)、[TCP_CORK 接口](https://man7.org/linux/man-pages/man7/tcp.7.html)

下面两段数据合起来是一条完整消息，第二次发送不再传 `MSG_MORE`：

```c
send_all(fd, "PRICE=", 6, MSG_MORE);
send_all(fd, "123\n", 4, 0);
```

如果之前通过 `TCP_CORK` 暂缓发送，完整消息提交后解除：

```c
int zero = 0;
check(setsockopt(fd, IPPROTO_TCP, TCP_CORK, &zero, sizeof(zero)));
```

### 第三组：减少系统调用与内存操作

### 2.6 sendmsg：分散缓冲区一次提交

**问题是什么**：消息头和 payload 经常放在不同的应用缓冲区。先拼成连续数组，会增加一次应用内复制；分别调用 `send()`，又会增加系统调用（每进一次内核都有固定开销）。`sendmsg()` 接收一组 `iovec`，每项只记录地址和长度，内核在一次调用中按顺序读取这些区域，省掉应用拼接和多次进入内核的开销。这是一次"分散-聚集（gather）"式提交：数据不用物理上挨在一起，只要告诉内核去哪几块内存读。普通发送仍会把 payload 复制到内核缓冲区（省的是系统调用和应用内拼接，不是内核复制）。[sendmsg 接口](https://man7.org/linux/man-pages/man2/sendmsg.2.html)

在已连接的 UDP socket 上，下面发送一条内容为 `PX=123` 的数据报：

```c
char header[] = "PX=", body[] = "123";
struct iovec parts[] = {{header, 3}, {body, 3}};
struct msghdr msg = {.msg_iov = parts, .msg_iovlen = 2};
check(sendmsg(fd, &msg, 0));
```

### 2.7 recvmmsg：批量接收

**问题是什么**：逐条调用 `recv()`，每条消息都要支付一次系统调用的固定开销。`recvmmsg()` 在一次调用中取出多条消息，分别写入各自的缓冲区，保留数据报边界——相当于"一次把信箱里的信全取走"。配合 `MSG_DONTWAIT`，它只处理当前已经到达的消息，不等未来消息凑满一批；减少的是进入、退出内核的次数，payload 复制仍然存在。[recvmmsg 接口](https://man7.org/linux/man-pages/man2/recvmmsg.2.html)

示例先向接收队列放入两条短 UDP 消息，再用四个位置接收：

```c
char buf[4][128];
struct iovec iov[4];
struct mmsghdr msgs[4] = {0};
for (int i = 0; i < 4; ++i) {
    iov[i] = (struct iovec){buf[i], sizeof(buf[i])};
    msgs[i].msg_hdr = (struct msghdr){.msg_iov = &iov[i], .msg_iovlen = 1};
}
int n = (int)check(recvmmsg(fd, msgs, 4, MSG_DONTWAIT, NULL));
```

此时 `n` 为 2，无须等到四个位置填满。第 `i` 条消息的长度在 `msgs[i].msg_len`；如果 `msg_flags` 带有 `MSG_TRUNC`，说明该条消息超过缓冲区，应丢弃并处理缺口。队列为空时返回 `-1/EAGAIN`，事件循环可以稍后再读。重复接收时可以复用这些数组，不必每轮重新分配。

### 2.8 复用应用接收缓冲区

**问题是什么**：每条消息都申请、释放内存，会反复执行分配器逻辑；首次触碰新页面还可能触发缺页处理（临时分配的内存第一次写入时，内核才真正分配物理页，这是一次可能发生在关键路径上的停顿）。预先分配并复用缓冲区，可以减少这些工作，也有利于复用缓存中的数据。`recv()` 仍把内核数据复制进这块内存，因此上一条消息要消费完，再覆盖同一缓冲区。

接收两条小于 2 KiB 的 UDP 消息时，只准备一份应用缓冲区：

```c
char buf[2048];
for (int i = 0; i < 2; ++i) {
    ssize_t n = check(recv(fd, buf, sizeof(buf), 0));
    printf("%.*s\n", (int)n, buf);  // 同步消费；下一次 recv 会覆盖 buf
}
```

这里用打印展示收到的内容，交易程序中替换为字段解析。

### 第四组：缓冲区与队列：抗突发，但不制造速度

### 2.9 SO_RCVBUF：内核接收缓冲区

**它是什么**：数据经过协议处理后，先留在 socket 的内核接收队列，等应用调用 `recv()` 取走。`SO_RCVBUF` 控制这部分缓冲的内存额度。把它想成柜台前的等候区：**等候区变大不会让柜台处理得更快，只能在人突然变多时少拒几位客人**。适当增大可以容纳应用短暂来不及处理的突发行情，减少 UDP 因队列溢出而丢包；但持续过载时，更大的队列只是让旧行情等待更久——对交易而言，"迟到的行情"往往等于"错误的行情"。它不会提高应用处理速度。

设置后读回实际值：

```c
int bytes = 256 * 1024;
check(setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &bytes, sizeof(bytes)));
socklen_t size = sizeof(bytes);
check(getsockopt(fd, SOL_SOCKET, SO_RCVBUF, &bytes, &size));
printf("SO_RCVBUF=%d\n", bytes);
```

Linux 会限制请求值，并在返回值中包含额外的内核记账空间，因此读回值不一定等于传入值。[SO_RCVBUF 接口](https://man7.org/linux/man-pages/man7/socket.7.html)

### 2.10 有界待发队列

**问题是什么**：策略产生订单的速度超过发送端处理速度时，无界队列会持续积压，轮到发送的订单可能已经过时（行情早变了，这单发出去就是亏损）。固定容量的环形队列预先分配槽位，并在满时拒绝继续入队，让上游及时重试、停止产生或放弃已失效的订单。它既省掉逐条分配，也把过载暴露给调用方，避免把压力全部转化成等待——这是一种显式的**背压（backpressure）**机制。

下面是单线程队列的入队操作，满时返回 `false`，由调用方决定稍后重试或放弃该笔尚未提交的订单：

```c
enum { CAP = 8 };
struct Order { int price; };
struct Queue { struct Order items[CAP]; unsigned head, tail, count; };
bool try_push(struct Queue *q, struct Order order) {
    if (q->count == CAP) return false;
    q->items[q->tail] = order;
    q->tail = (q->tail + 1) % CAP;
    ++q->count;
    return true;
}
```

队首订单处理完后，推进 `head` 并减少 `count`，即可复用空位。这个容量限制作用于应用尚未提交的订单；已经提交给 TCP 的字节仍按连接顺序发送。

### 第五组：把活交给网卡

### 2.11 校验和计算卸载到网卡

**先理解校验和是什么**：校验和（checksum）是报文携带的一个校验值，用来检测传输中的数据差错。以 TCP 为例，发送端根据 TCP 头、payload，以及包含源地址、目的地址等信息的“伪首部”计算一个 16 位校验和，写入 TCP 头；接收端按同样的规则验证收到的数据。[TCP 校验和定义](https://www.rfc-editor.org/rfc/rfc9293.html#section-3.1)

**为什么它是一项 CPU 负担**：假设一条 TCP 报文带有 1000 字节 payload，计算校验和就要把这 1000 字节都纳入计算。网卡通过 DMA 读取报文，只完成数据搬运；如果校验仍由软件执行，CPU 还要承担相应的加法、进位处理和取反运算。[Internet 校验和算法](https://www.rfc-editor.org/rfc/rfc1071.html)

**offload 改变的是"这部分计算由谁执行"：**

| 分工 | 关闭发送校验和 offload | 开启发送校验和 offload |
| --- | --- | --- |
| CPU | 构造报文，并根据报文内容算好完整校验和 | 构造报文，准备计算范围、结果位置及必要的初始值 |
| 网卡 | DMA 读取已经算好校验和的报文，再发送 | DMA 读取报文，完成校验和计算并填入报文，再发送 |

驱动会把“从哪里开始计算、结果写入头部哪个位置”等信息交给网卡，网卡的数据处理硬件完成剩余计算。线上发出的仍是带有正确校验和的标准 TCP 报文，对端无须改变协议。接收方向也可以卸载：网卡验证校验和并报告结果，内核对已验证的报文省去重复计算。[Linux 校验和卸载](https://docs.kernel.org/networking/checksum-offloads.html)

因此，它减少的是 CPU 的校验计算。软件实现若单独扫描报文，还能减少这次遍历；若把复制与校验合在一次遍历中完成，则主要省下校验运算。系统调用和用户缓冲区到内核缓冲区的复制仍然存在。报文较大或包速率较高时，这些计算累积起来会占用更多 CPU；卸载后能把处理器时间留给协议处理和交易逻辑。

先记录当前状态，再启用发送校验和卸载：

```bash
ethtool -k eth0
sudo ethtool -K eth0 tx on     # 发送校验和卸载
```

### 2.12 TSO：发送分段卸载

**背景**：线上的 TCP 段受 MTU 限制（通常每段约 1500 字节），但协议栈手里可能是几十 KB 的大块数据。传统做法是 CPU 在软件里把它切成一段段、每段单独加头算校验和。TCP 分段卸载（TCP Segmentation Offload，TSO）让内核把较大的数据块、分段大小和报文头信息一起交给网卡，由网卡切成线上允许的多个 TCP 段，并生成各段的头部和校验和。处理器因此少做逐段处理，较大的发送更容易受益；单条很小的订单本来就只有一个段，节省的工作有限。[Linux TSO](https://docs.kernel.org/networking/segmentation-offloads.html#tcp-segmentation-offload)

下面关闭 TSO，用于比较开关前后的单条消息延迟与处理器占用：

```bash
sudo ethtool -K eth0 tso off   # 观察关闭 TSO 后的变化
```

### 2.13 GRO：接收合并

**与 TSO 对称的接收侧优化**：通用接收合并（Generic Receive Offload，GRO）由内核软件在收包过程中，把同一条流中符合条件的连续报文合成较大的报文表示，再交给上层协议处理。这样可以减少逐包调用和重复处理报文头的开销，线上报文并没有改变（合并只发生在主机内部）。它合并的是收包过程中已经取得的数据；额外等待多久，还取决于中断合并和 NAPI 定时配置——所以在小消息延迟敏感的场景，GRO 的"再等等看有没有后续包"也可能成为延迟来源，需要实测开关对比。[Linux GRO](https://docs.kernel.org/networking/segmentation-offloads.html#generic-receive-offload)、[NAPI 合并与定时配置](https://docs.kernel.org/networking/napi.html#software-irq-coalescing)

观察小消息延迟时，可以单独关闭 GRO 做对比：

```bash
sudo ethtool -K eth0 gro off   # 观察关闭 GRO 后的变化
```

### 第六组：彻底去掉一次复制

### 2.14 MSG_ZEROCOPY：发送 payload 零拷贝

普通 `send()` 先由处理器把用户缓冲区复制到内核缓冲区，再让网卡通过 DMA 读取。`MSG_ZEROCOPY` 尝试省掉前一次复制，让网卡直接读取用户缓冲区对应的内存页；系统调用、协议处理和报文头生成仍由处理器完成。

为什么这件事不容易？因为 `send()` 正常返回后应用随时可以复用缓冲区，而网卡可能还没把数据发完，TCP 甚至可能在丢包后重传。零拷贝的解法是**把"什么时候可以复用缓冲区"的所有权交接显式化**。在能够保持零拷贝的发送路径中，基本过程是：

1. **内核固定用户页。** 应用只传入虚拟地址和长度，内核找到对应页面并 pin 住，避免这些页在使用期间被回收或换出；内核报文描述结构 `sk_buff`（简称 `skb`）通过页引用、偏移和长度描述 payload，省去另存一份 payload。[Linux skb 结构](https://docs.kernel.org/networking/skbuff.html)
2. **驱动建立 DMA 映射。** 驱动通过 Linux DMA API 得到网卡可用的 DMA 地址。启用输入输出内存管理单元（Input-Output Memory Management Unit，IOMMU）时，这通常是 I/O 虚拟地址（I/O Virtual Address，IOVA），由 IOMMU 翻译到物理地址；应用无须查找或传递物理地址。[Linux DMA 地址映射](https://docs.kernel.org/core-api/dma-api-howto.html)
3. **网卡读取 payload。** 驱动把 DMA 地址和长度写入发送描述符，网卡自身的 DMA 引擎按描述符读取数据。支持分散聚集（scatter-gather）的网卡可以从多段内存取数，用户缓冲区不必在物理内存中连续。
4. **内核通知页面可以复用。** 等网络栈释放对这些页面的引用后，内核向 socket 的错误队列放入完成通知。应用通过 `recvmsg(..., MSG_ERRQUEUE)` 读取，再复用对应缓冲区。TCP 为重传保留的数据引用也会影响这个时机。[Linux MSG_ZEROCOPY](https://docs.kernel.org/networking/msg_zerocopy.html)

**两个必须记住的坑：**

- **pin 住页面不会阻止应用写入；在完成通知到来前覆盖缓冲区，可能改变尚未发出的内容。** 也就是说，pin 只保证页不被内核回收，不限制你自己写它——收到通知前必须把这块内存当作"还在内核手里"。
- 完成通知只表示原缓冲区已可复用；发生退回复制时，它甚至可以早于实际发包。页面管理、DMA 映射和通知都有成本，所以这项优化更适合较大的发送；几十到几百字节的小订单，直接复制往往更划算。

下面只在一个新建、已连接的 IPv4 UDP socket 上提交一次数据，对应的零拷贝完成编号为 `0`。`wait_zerocopy_0()` 先等待错误队列可读，再检查通知来源和完成编号；通知内容来自 `<linux/errqueue.h>` 中的 `sock_extended_err`：

```c
static void wait_zerocopy_0(int fd) {
    struct pollfd p = {.fd = fd, .events = POLLERR};
    require(check(poll(&p, 1, 1000)) == 1, "zerocopy notification timed out");
    union { struct cmsghdr align; char bytes[128]; } control;
    struct msghdr msg = {.msg_control = control.bytes,
                        .msg_controllen = sizeof(control.bytes)};
    check(recvmsg(fd, &msg, MSG_ERRQUEUE));
    require(!(msg.msg_flags & MSG_CTRUNC), "completion control data truncated");
    for (struct cmsghdr *c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg, c)) {
        if (c->cmsg_level != SOL_IP || c->cmsg_type != IP_RECVERR ||
            c->cmsg_len < CMSG_LEN(sizeof(struct sock_extended_err))) continue;
        struct sock_extended_err e;
        memcpy(&e, CMSG_DATA(c), sizeof(e));
        if (e.ee_origin != SO_EE_ORIGIN_ZEROCOPY || e.ee_errno != 0) continue;
        require(e.ee_info == 0 && e.ee_data == 0, "unexpected completion IDs");
        printf("MSG_ZEROCOPY: completion 0, copied=%d\n",
               e.ee_code == SO_EE_CODE_ZEROCOPY_COPIED);
        return;
    }
    require(false, "no matching zerocopy completion");
}
```

发送后等到通知，再覆盖原缓冲区：

```c
int one = 1;
check(setsockopt(fd, SOL_SOCKET, SO_ZEROCOPY, &one, sizeof(one)));
char buf[32768] = {0};
check(send(fd, buf, sizeof(buf), MSG_ZEROCOPY));
wait_zerocopy_0(fd);
memset(buf, 'X', sizeof(buf));  // 收到完成通知后才允许复用
```

### 第七组：把系统调用批量化

### 2.15 io_uring：共享环形队列，批量提交收发

**问题是什么**：前面的 `sendmsg`、`recvmmsg` 已经在减少系统调用，但它们仍是"每次调用进一次内核"；而传统的 `epoll_wait()` 还有一个额外的往返——先被唤醒、拿到"哪些 fd 就绪"的列表，再分别调用 `recv()` 取数据。一轮事件循环里通常是 `epoll_wait` + N 次 `recv` + N 次 `send`，高频小消息下，陷入内核和睡眠/唤醒的固定开销占比会很明显。

**io_uring 怎么做**：它在应用和内核之间建立两块**共享内存环形队列**——应用往**提交环（SQ）**填请求（SQE），内核执行后把结果写入**完成环（CQ）**。一批请求可以一次性提交；完成结果直接写在共享内存里，应用轮询即可看到，不必为了"取结果"再进内核。网络场景下对低延迟最有用的几个能力是：

- **一次提交多个操作**：accept、recv、send、超时、取消等不同操作混在一批里，还可以链接成流水线（先收、再发、带超时）。
- **multishot 接收**：提交一次"持续接收"请求（6.0 及以上内核），之后每条到达的消息都会自动产生一个完成项，不用反复提交 recv。
- **固定文件与注册缓冲**：fd 和缓冲区预先登记，内核执行时免去每次查找和准备。
- **SQPOLL 模式**：一个内核线程主动轮询提交环，应用连提交操作都不用陷入内核；本质也是拿一个核的空转换延迟。
- **零拷贝发送**：`SEND_ZC` 操作的完成通知统一从完成环返回，和 MSG_ZEROCOPY 解决同一个复制问题，但通知机制更统一。

![io_uring 与传统 epoll+socket 对比：共享 SQ/CQ 环批量提交、直接读完成，但下半段内核协议栈相同](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/e1a13d0410c2b03463ea6adf436e5335d53ea679/hft/assets/hft-06/io-uring-vs-socket.png)

**必须先澄清的边界：io_uring 不是内核旁路。** 从 NAPI 软中断、IP/防火墙到 TCP/UDP 协议处理，下半段与传统 socket **完全一样**，接收方向的 payload 拷贝也依然存在。它省掉的是系统调用边界、fd/缓冲的重复准备和睡眠唤醒；因此它与中断合并、忙轮询、RSS 绑核是**叠加**关系，与 DPDK、Onload 这类用户态旁路是不同层次的技术。[io_uring 接口](https://man7.org/linux/man-pages/man7/io_uring.7.html)

下面是用 liburing 做 multishot 接收的极简骨架（示意，具体 API 随 liburing 版本略有差异）：

```c
// 初始化 ring；注册 fd 与接收缓冲区数组（固定文件 + 提供缓冲）后：
struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);
io_uring_prep_recv_multishot(sqe, fd, NULL, 0, 0);  // 一次提交，持续接收
io_uring_sqe_set_flags(sqe, IOSQE_BUFFER_SELECT);   // 命中注册缓冲区
io_uring_submit(&ring);                              // 一批 SQE 只需一次 submit

while (running) {
    struct io_uring_cqe *cqe;
    io_uring_wait_cqe(&ring, &cqe);                  // 也可直接轮询 CQ，不睡眠
    // cqe->res = 收到的字节数；cqe->flags 给出命中的缓冲区编号
    io_uring_cqe_seen(&ring, cqe);
    // multishot 结束时（如连接关闭）需要重新提交
}
```

**代价与适用场景**：SQE/CQE 的生命周期、完成环溢出、multishot 终止后重新提交等正确性问题需要自己管；SQPOLL 要常驻一个核空转且通常需要系统权限；不少网络操作要求较新内核（multishot、`SEND_ZC` 需 6.0+）。当消息又小又快、一个线程混合处理多种操作、且机器内核够新时，它能显著压低系统调用开销；消息速率很低时，普通阻塞 socket 反而更简单。

### 第八组：调整可靠传输策略

### 2.16 KCP：在 UDP 上实现低延迟的可靠传输

**它优化什么**：当通信两端都能改造时，可以在普通 UDP socket 上接入 KCP。它在用户态实现自动重传请求（Automatic Repeat Request，ARQ），用确认与重传补齐 UDP 的可靠性，并提供有序交付。数据仍经过内核 UDP/IP 栈；降低延迟的重点是更及时地反馈和恢复丢包。[KCP 官方说明](https://github.com/skywind3000/kcp/blob/master/README.en.md)

**UDP 怎样变得可靠**：

1. **分段、编号并保留副本。** 发送端把消息拆成带序号和分片信息的数据段，经 UDP 发出；未确认的数据保留在发送缓冲区，供后续重传。
2. **确认、去重并按序重组。** 接收端用确认报文（Acknowledgment，ACK）确认具体数据段，同时通过 `una` 字段告知下一个期待的序号，累计确认此前的数据。乱序段先缓存，重复段丢弃；接收窗口限制发送端继续发来的数据量。
3. **发现缺口后补发。** 后续 ACK 越过某个未确认段时，发送端累积快速重传计数；达到阈值就安排重传。没有足够后续 ACK 时，则依据测得的往返时间计算重传超时（Retransmission Timeout，RTO），到期后补发。

**把这些动作连成状态图**：发送端跟踪每个数据段，接收端最终交付完整消息。图中的 `snd_buf` 是发送端保留未确认数据的缓冲区；发送窗口允许多个段同时处于“已发，等待确认”状态。

![KCP 简化状态机：发送端保留副本、接收端确认与有序交付，以及丢包后的重传回路](assets/hft-06/kcp-state-machine.png)

从上排开始读：消息分段后排队，窗口允许时经 UDP 发出；收到对应 ACK 或覆盖该段的累计确认后，发送端才能释放副本。若迟迟没有确认，或后续确认足以判断出现缺口，就沿红色回路补发，随后继续等待确认。应用调用 `ikcp_update()` 推进刷新，发送和重传才会实际执行。

下排区分了**收到数据**与**交付消息**：接收端收到乱序段也可以先确认、缓存；只有前序缺口和消息分片都补齐后，应用才能按序读到它。因此，确认可以早于应用读取，同一 KCP 会话仍有队头阻塞。[KCP 确认、接收与重传实现](https://github.com/skywind3000/kcp/blob/master/ikcp.c)

**为什么可能比原生 TCP 延迟更低**：KCP 的快速模式允许更积极地发送和重传，用更多协议处理及额外报文换取较短的丢包恢复等待，收益主要体现在有丢包、抖动时的尾延迟。

| 机制 | 怎样减少等待 |
| --- | --- |
| 可调的快速重传阈值 | 例如设为 `2`，未确认段的快速重传计数达到 2 后即可补发，无须等 RTO 到期；阈值较低也更容易把乱序当成丢包 |
| 较短的超时与较缓的退避 | 快速模式降低 RTO 下限；标准 C 实现中，`nodelay=1` 时连续超时约按 1.5 倍延长等待，比 TCP 超时后翻倍退避更积极 |
| 更频繁的协议刷新 | 缩短刷新间隔，让待发数据、ACK 和重传更早提交；ACK 在刷新时发出，应用的事件循环必须及时推进协议 |

这些参数控制的是何时确认、何时补发；重传越积极，带宽和处理器开销也越大。[KCP 参数与实现](https://github.com/skywind3000/kcp/blob/master/ikcp.c)、[TCP 超时退避规则](https://www.rfc-editor.org/rfc/rfc6298.html#section-5)

已有 KCP 对象 `kcp` 时，可以先使用保留拥塞控制的配置：

```c
// 快速模式；10 ms 刷新间隔；快速重传阈值 2；保留拥塞控制
ikcp_nodelay(kcp, 1, 10, 2, 0);
```

应用通过 `ikcp_send()` 提交消息，在输出回调里用 UDP socket 发包；收到 UDP 数据后交给 `ikcp_input()`，再用 `ikcp_recv()` 取出完整消息，并定期调用 `ikcp_update()` 驱动刷新。常见激进配置会把最后一个参数改为 `1`，跳过默认拥塞窗口约束，但仍受发送窗口和对端接收窗口限制。这适用于容量与流量可控的链路；在拥塞网络中继续抢发，会增加排队和丢包。[KCP 接入与配置](https://github.com/skywind3000/kcp/blob/master/README.en.md#basic-usage)

#### 用具体故障对照 KCP 与 TCP

下面用 `1、2、3……` 表示数据段编号，每段先按承载一条完整消息理解；TCP 实际按字节编号。先看两个能直接解释延迟差异的例子，再看其他异常如何处理。

**例一：中间的 `2` 丢失，KCP 可以更早达到快速重传条件。** 设 `1` 已确认，发送窗口足以继续发送 `3、4、5`，它们的确认分别到达发送端。KCP 使用上例的快速重传阈值 `2`；TCP 先按经典的“三个重复 ACK”规则比较。

| 后续反馈到达发送端 | KCP 的处理 | 经典 TCP 的处理 |
| --- | --- | --- |
| 收到 `3` 对应的反馈 | `ACK(3)` 确认 `3`，`una=2` 表示前面仍缺 `2`；`2` 的快速重传计数变为 1 | 收到第一个重复 ACK，累计确认仍停在 `2` 的起点 |
| 收到 `4` 对应的反馈 | `ACK(4)` 让计数达到 2，下一次刷新即可补发 `2` | 收到第二个重复 ACK，尚未达到经典快速重传阈值 |
| 收到 `5` 对应的反馈 | 已经具备补发 `2` 的条件，无须靠这次反馈才触发 | 第三个重复 ACK 到达，触发补发 `2` |
| 补发的 `2` 到达接收端 | 补齐缺口，随后可按序交付 `2、3、4……` | 补齐字节流缺口，向应用提供连续字节 |

这里省下的是**从第二次反馈等到第三次反馈的时间**，最终发送还受刷新时机影响。TCP 收到乱序段通常会立即发送重复 ACK；这个例子的差异来自重传阈值。[TCP 快速重传与确认规则](https://www.rfc-editor.org/rfc/rfc5681.html#section-3.2)

对应到 KCP 的实现，表中假设 `ACK(3)`、`ACK(4)` 分别进入两次 `ikcp_input()`。若两条 ACK 合并在同一个 UDP 数据报里，标准 C 实现只会在本次输入结束时，使用本次选出的确认序号更新一轮快速重传计数，不能直接把“两条 ACK”算成“计数加 2”。[KCP 快速重传计数](https://github.com/skywind3000/kcp/blob/master/ikcp.c)

现代 TCP 还可以使用选择确认（Selective Acknowledgment，SACK）报告已经收到的不连续字节范围，以及近期确认（Recent Acknowledgment，RACK）算法，根据发送时间和后续确认判断早先的数据是否丢失。启用这些机制后，TCP 也能选择性补发，并且不必始终等满三个重复 ACK。[TCP SACK](https://www.rfc-editor.org/rfc/rfc2018.html)、[TCP RACK](https://www.rfc-editor.org/rfc/rfc8985.html#section-3.1)

**例二：补发的 `2` 又丢了，而且没有足够的新 ACK。** 此时双方都可能退回超时重传。为了单独比较退避倍率，设两者第一轮超时等待都为 `200 ms`，后续没有新 ACK 更新估计，KCP 使用 `nodelay=1`，定时任务及时执行：

| 第几轮等待 | KCP 本轮等待 | TCP 本轮等待 |
| --- | --- | --- |
| 第一轮 | 200 ms | 200 ms |
| 第二轮 | 300 ms | 400 ms |
| 第三轮 | 450 ms | 800 ms |

连续经过这三轮等待后，KCP 共等待 `950 ms`，TCP 共等待 `1400 ms`。这个算例说明，较缓的退避可以减少连续丢包时的长时间停顿，同时也会更频繁地向尚未恢复的网络重传。它比较的是**连续超时这条分支**；有新确认或提前探测时，恢复过程会改走其他分支。[KCP 超时实现](https://github.com/skywind3000/kcp/blob/master/ikcp.c)、[TCP 超时翻倍规则](https://www.rfc-editor.org/rfc/rfc6298.html#section-5)

**其他异常可以沿同一张状态图理解：**

| 场景与具体例子 | KCP 怎样处理 | TCP 会怎样处理，延迟差异在哪 |
| --- | --- | --- |
| **尾段丢失**：只发 `1、2、3`，最后的 `3` 丢了 | 没有 `4、5` 的 ACK 来暴露缺口，只能等待该段超时；较短的当前 RTO 可以让补发更早发生 | 经典恢复也依赖超时。现代 TCP 的尾包探测（Tail Loss Probe，TLP）会尝试在 RTO 前发送探测段获取确认，因此这一场景也可能由 TCP 更早恢复 |
| **ACK 丢失**：`2` 已收到，但 `ACK(2)` 丢了 | 后续报文若携带 `una=3` 或更大的累计确认，就能释放 `2`；若没有后续确认，则超时补发，接收端去重并再次确认 | TCP 的后续累计 ACK 也能覆盖早先丢失的 ACK；没有后续反馈时也会重传。两者都不需要“每一条 ACK 必须到达” |
| **乱序到达**：顺序变成 `1、3、4、2` | 先缓存并确认 `3、4`，等待 `2`；若快速重传先触发，可能多发一份其实还在途中的 `2` | TCP 也缓存乱序数据，并在缺口补齐后提供连续字节。较低的重传阈值能更快处理真丢包，也更容易因乱序产生多余重传；RACK 会考虑乱序等待窗口 |
| **重复数据**：网络或重传让 `2` 到达两次 | 同一会话按序号去重，重复段不会再次交付；接收端仍可再次发确认 | TCP 同样按字节序号去重。去重保证交付正确性，本身没有 KCP 独有的延迟收益 |
| **多个缺口**：发送 `1～6`，`2、4` 丢失 | 分别记录已确认段，按各段的快速重传计数或计时器补发 `2、4`；已确认的 `3、5、6` 无须一起重发 | 支持 SACK 的 TCP 同样能定位多个缺口并选择性补发。KCP 的收益主要取决于触发和退避策略；仅靠累计确认的旧式恢复可能需要更多轮次 |
| **消息分片未齐**：一条消息拆成三段，只到两段 | 已到的段可以确认，但默认消息模式下，`ikcp_recv()` 要等全部分片齐全后才能返回完整消息 | TCP 可先把缺口之前的连续字节交给应用，应用再负责拼消息。若业务必须拿到完整消息，双方都要等待缺失部分；KCP 的消息边界不会消除这个等待 |
| **接收窗口耗尽**：应用停止读取，接收队列装满 | 通告零窗口，发送端暂停发送新的数据段；应用读走数据后通告窗口恢复。窗口通知丢失时，还可通过窗口探测恢复 | TCP 也会停止发送新数据并做零窗口探测。瓶颈是接收端消费速度；`nc=1` 仍不能越过 KCP 的接收窗口限制 |
| **持续断网**：数据和确认一直都到不了 | 反复重传达到 `dead_link` 阈值后设置死链标记，由外层停止会话、重连并核对未决消息；空闲时没有在途数据，还需应用自行检测存活 | 内核 TCP 会按重传与超时策略报告连接失败；Linux 可通过 `TCP_USER_TIMEOUT` 限制未确认数据等待时间。两者重连后都仍需业务层确认操作是否已经执行 |
| **应用停顿**：事件循环卡住 100 ms | 收到的 UDP 包无法及时交给 `ikcp_input()`，刷新与重传也随 `ikcp_update()` 推迟；低延迟参数在停顿期间无法生效 | 内核 TCP 可以继续处理确认和重传，应用自身的读取仍会推迟。KCP 把协议调度权交给应用，也让应用停顿直接影响恢复速度 |
| **输入格式错误**：UDP 报文被截断，或会话编号 `conv` 不匹配 | `ikcp_input()` 对相应输入返回错误，接入层需要丢弃并记录；持续错误的会话配置要由应用修正 | TCP 在内核中检查报文并按连接分流。两者都需要正确的协议与会话配置，缩短重传间隔无法修复配置错误 |

上表中的 KCP 分支可在 `ikcp_input()`、`ikcp_recv()` 和 `ikcp_flush()` 中对应找到。[KCP 核心实现](https://github.com/skywind3000/kcp/blob/master/ikcp.c) TCP 的提前探测、选择性补发、窗口探测和失败超时分别见 [RACK-TLP](https://www.rfc-editor.org/rfc/rfc8985.html)、[SACK](https://www.rfc-editor.org/rfc/rfc2018.html)、[零窗口探测](https://www.rfc-editor.org/rfc/rfc9293.html#section-3.8.6.1)与 [TCP_USER_TIMEOUT](https://man7.org/linux/man-pages/man7/tcp.7.html)。

窗口恢复也有独立的计时：标准 C 实现的初次零窗口探测等待为 `5000 ms`，与这里配置的 `10 ms` 刷新间隔是两回事。接收队列恢复后主动发出的窗口通知通常能更早恢复发送；若通知丢失，等待探测就可能明显拉长停顿。[KCP 窗口探测实现](https://github.com/skywind3000/kcp/blob/master/ikcp.c)

**放到 HFT 场景中看**：TCP 也支持选择确认和按时间检测丢包，比较时应以开启 `TCP_NODELAY`、使用现代丢包恢复机制的 TCP 为基线。[TCP 丢包检测](https://www.rfc-editor.org/rfc/rfc8985.html) KCP 更适合双方可控、需要改善丢包恢复的链路；在微秒级机房通信中，若只按示例每 10 ms 刷新，新增等待就可能远超原有网络延迟。交易所只开放 TCP/WebSocket 时，也需要沿用对端协议。

> **第二部分小结**：这些手段的共同点是"把吞吐导向的默认值改成延迟导向"。它们不是越多越好——中断合并、GRO、TSO、缓冲区大小都要在自己的工作负载下实测开关前后的延迟分布（尤其关注尾延迟），再决定取舍。

## 3. 用户态收发与硬件旁路

前面的优化都保留了内核网络栈的收发路径。内核旁路（kernel bypass）则更进一步：把关键数据收发移出普通内核网络栈。需要注意的是，"旁路"不等于"完全不要内核"——设备初始化、内存映射和权限管理仍由驱动配合；只是在**稳定运行时**，应用直接访问收发队列或映射的缓冲区，热路径上不再有系统调用、内核协议处理和内核缓冲复制。

打个比方：传统栈像走公共邮政系统，每个包裹都要经过分拣中心的统一流程；内核旁路则是在你的机房和交易所机房之间修一条专用通道，日常运输直接走专线，但修路、划地、安检仍要一次性完成。

下面四类技术采用了不同的分工：有的提供原始包接口，有的实现用户态 TCP/UDP，有的把传输工作交给网卡。可以按"应用需要自己承担多少工作"来排序：越往下越接近硬件、越快，但应用要管的事情也越多。

### 3.1 ExaNIC

ExaNIC 是 Cisco Nexus SmartNIC 的原名。它的设计哲学是**卡上尽量不缓存**，把到达的数据分块 DMA 到主机接收环；应用通过 `libexanic` 轮询并直接读取这些内存。支持的接口还允许在整帧到齐前先处理已经到达的部分，重叠线路接收与解析工作（网卡还在收后半帧，CPU 已经开始解析前半帧）。发送则可以由应用写入映射的卡上发送缓冲区，再触发网卡发送。[Cisco ExaNIC Internals](https://www.cisco.com/c/en/us/td/docs/dcn/nexus3550/smartnic/sw/user-guide/cisco-nexus-smartnic-user-guide/internals.html)

![ExaNIC/libexanic：分块 DMA 进接收环，应用用户态轮询直读、边收边解析](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/e1a13d0410c2b03463ea6adf436e5335d53ea679/hft/assets/hft-06/bypass-exanic.png)

它的关键在于减少缓存等待和搬运核间通信。代价也要清楚：应用接管帧解析，也要跟上接收环的推进速度，否则旧数据会被新数据覆盖（环形缓冲没有人为你排队）；提前解析时，还要等完整帧的有效性结果（比如校验和），再决定是否采用这条消息，不能只解析了一半就下单。`libexanic` 的原始帧接口适合直接处理行情包，完整的 TCP 连接仍由额外的协议栈维护。

### 3.2 DPDK

数据平面开发套件（Data Plane Development Kit，DPDK）通过轮询模式驱动（Poll Mode Driver，PMD）在用户态收发包——没有中断，线程永远在轮询队列。网卡把数据 DMA 到预先准备的内存池，应用取得描述报文的 `mbuf`，完成处理后回收，或者转交发送队列。它把轮询、批量收发、内存池和核心分工组织在一起，减少逐包中断、临时分配和通用内核处理成本。[DPDK PMD](https://doc.dpdk.org/guides/prog_guide/ethdev/ethdev.html)

![DPDK/PMD：专用核心上死循环轮询收发环，mbuf 取自预分配内存池，热路径无中断无内核](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/e1a13d0410c2b03463ea6adf436e5335d53ea679/hft/assets/hft-06/bypass-dpdk.png)

DPDK 适合可分配专用核心、需要高包速率的行情接收和分发。要注意边界：它提供的是**包处理框架**（拿到的是原始以太网帧），TCP 的建连、重传和流控仍要由附加协议栈完成；它也要求独占 CPU 核心和网卡，不能和内核网络栈混用同一张卡。批量取包时，一次取走当前队列里的多个包可以减少开销；但主动等队列凑满才处理，会增加低流量时的等待——低延迟场景应当"有多少处理多少"，而不是"凑一批再动"。

### 3.3 RDMA

远程直接内存访问（Remote Direct Memory Access，RDMA）把通信区域预先注册给网卡，应用提交发送、读取或写入请求，由网卡执行传输，再通过完成队列报告结果。在硬件 RDMA 路径中，数据可以直接从一端的应用内存到另一端的应用内存，减少内核缓冲区复制，并由网卡承担相应传输协议工作。[NVIDIA RDMA 编程指南](https://networking-docs.nvidia.com/doca/archive/3-5-0/rdma-aware-networks-programming-guide)

![RDMA：内存注册后由 RNIC 搬运，单边读写数据直达远端应用内存，远端 CPU 不执行接收调用](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/e1a13d0410c2b03463ea6adf436e5335d53ea679/hft/assets/hft-06/bypass-rdma.png)

单边读写尤其直观：远端应用先注册内存并授予访问权限，之后一次远程读/写可以由网卡完成，无须远端业务线程逐次执行接收调用——数据像被"隔空放"进了对方内存。但要区分两个时间点：**本地传输完成 ≠ 远端业务处理完成**，业务线程仍负责消费数据和维护状态。RDMA 更适合双方可控、硬件与网络匹配的内部集群（例如机房内各服务之间）；交易所只提供 TCP/WebSocket 入口时，客户端无法单方面把连接改成 RDMA。

### 3.4 Solarflare：Onload、TCPDirect 与 ef_vi

Solarflare 的软件接口可以按应用愿意承担多少工作来理解：

| 接口 | 核心做法 | 应用怎样接入 |
| --- | --- | --- |
| Onload | 在用户态实现 TCP/UDP，尽量兼容现有 socket 编程方式 | 延续 `send()`、`recv()` 等调用方式，对受支持的连接进行加速 |
| TCPDirect | 用更精简的功能和专用接口实现用户态 TCP/UDP | 改造事件循环，由应用主动推进协议处理，并使用直接访问缓冲区等能力 |
| `ef_vi` | 暴露更接近网卡的帧与事件接口 | 应用取得原始帧，自行安排上层协议和业务处理 |

![Solarflare 三档接口对比：Onload / TCPDirect / ef_vi，及 TCPDirect reactor 热路径与 CTPIO](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/e1a13d0410c2b03463ea6adf436e5335d53ea679/hft/assets/hft-06/bypass-solarflare.png)

Solarflare TCPDirect 是其中面向低延迟应用的用户态 TCP/UDP 协议栈。关键收发操作绕过内核，线上仍然使用标准 TCP/UDP 报文，对端可以继续使用普通网络接口（旁路是本机的事，交换机和对端无感知）。应用在同一线程中推进网络、解析行情和计算策略，可以减少系统调用、线程唤醒及缓冲区复制。[TCPDirect 项目说明](https://github.com/Xilinx-CNS/tcpdirect)

应用通过 `zf_reactor_perform()` 推进收包、协议状态和定时任务——这是用户态协议栈的典型模型：**协议不会自己往前走，必须由应用的事件循环反复"驱动"它**。零拷贝接收接口让应用直接读取协议栈中的数据，处理完后归还缓冲区。协议处理仍由处理器执行，应用长时间不推进 reactor，网络处理也会停下来。[TCPDirect 用户指南](https://www.amd.com/content/dam/amd/en/support/downloads/solarflare/onload/openonload/packages/SF-116303-CD-9_TCPDirect_User_Guide.pdf)

这套技术的重点是让协议处理贴近应用，并利用配套网卡减少数据搬运。支持的网卡还可以使用直通程序控制输入输出（Cut-Through Programmed I/O，CTPIO）：处理器向网卡写入报文时，网卡在数据尚未全部写完前就开始发往线路，重叠"主机到网卡"和"网卡到线路"两段传输——连"写完再发"的串行等待都被压缩掉。[Onload 用户指南](https://docs.amd.com/r/en-US/ug1586-onload-user/all)、[`ef_vi` 接口说明](https://github.com/Xilinx-CNS/onload/blob/master/src/onload/distfiles/README.ef_vi)、[CTPIO 说明](https://docs.amd.com/r/en-US/ug1586-onload-user/CTPIO)

选择原始包接口，意味着自己承担更多协议和缓冲区管理；选择 TCPDirect、Onload，则可以沿用成熟 TCP/UDP 实现，把精力放在交易逻辑上。对于固定使用 TCP 的交易所入口，完整的用户态 TCP 栈往往比单纯取得原始帧更接近实际需求。

**四类路线横向对比：**

| 路线 | 应用拿到什么 | TCP/UDP 谁实现 | 典型场景 |
| --- | --- | --- | --- |
| ExaNIC（libexanic） | 映射内存中的原始帧 | 应用自行解析，或另配协议栈 | 超低延迟行情捕获、抢跑解析 |
| DPDK（PMD） | 原始帧（`mbuf`） | 需要附加用户态协议栈 | 高包速率行情接收、分发网关 |
| RDMA | 已到应用内存的消息/单边读写 | 网卡硬件实现传输 | 双方可控的内部集群 |
| Onload / TCPDirect | socket 或专用 API 的消息 | 成熟的用户态 TCP/UDP 栈 | 现有 TCP 策略程序的低延迟改造 |
| `ef_vi` | 原始帧与事件 | 应用自行实现 | 需要完全自控的定制协议栈 |

> **第三部分要点**：内核旁路不是银弹。它用"专用核心、专用网卡、应用承担更多责任"换来热路径上的低延迟；能否使用，还取决于交易所开放什么入口（RDMA/组播行情还是只有 TCP/WebSocket）。

## 4. Crypto 行情中的 WebSocket 与 JSON

前面讨论的优化针对"数据在本机怎么收发"。但消息到达应用之前的**表达方式**同样影响延迟：传统头部交易所已经有面向专业接入的二进制行情协议，例如 Nasdaq ITCH 直接规定消息类型、字段长度和二进制编码——程序拿到字节流后按固定偏移就能取出价格，几乎没有解析成本。Crypto 的低延迟接入能力则更不统一，面向普通开发者的公开行情入口仍广泛采用 WebSocket 与 JSON；Binance 的普通现货行情流就是一个具体例子。接入这种接口时，交易程序还要承担解密、消息重组和文本解析。[Nasdaq ITCH 规范](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQTVITCHSpecification.pdf)、[Binance WebSocket 行情](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md)

这种差异更适合按实际入口讨论：Binance 已经提供简单二进制编码（Simple Binary Encoding，SBE）行情流，并通过 WebSocket 二进制帧传输。因此，WebSocket 可以承载 JSON，也可以承载二进制消息；能否使用更直接的编码，取决于交易所开放了什么接口。[Binance SBE 行情](https://github.com/binance/binance-spot-api-docs/blob/master/sbe-market-data-streams.md)

### WebSocket：为什么需要它，数据怎么流过来

先解决一个常见疑问：既然底层就是 TCP，为什么还要 WebSocket？因为原生 TCP 只给字节流，没有"消息边界""订阅关系""浏览器可用的全双工通道"这些概念。WebSocket 在一条长期保持的 TCP 连接上提供双向消息通信，且可以复用 HTTP 的 80/443 端口和升级机制。

以经典的 HTTP/1.1 Upgrade 方式为例，建连和收行情的完整过程是：

1. 客户端与服务器建立 TCP 连接，再通过超文本传输协议（Hypertext Transfer Protocol，HTTP）发送一个带升级请求的 HTTP 报文；
2. 服务器返回 `101 Switching Protocols`，连接此后不再走 HTTP 规则，改为 WebSocket 帧；
3. 客户端发送订阅消息（订阅哪个品种、什么深度）；
4. 服务器持续主动推送更新，无须客户端为每条行情再发起一次 HTTP 请求。

`wss://` 还使用传输层安全协议（Transport Layer Security，TLS）加密连接。JSON 则负责表达行情里的品种、价格、数量等业务字段。下面把建连过程和一条行情的接收过程放在同一张图中。

![WebSocket 原理：建立连接、升级协议、订阅与持续推送，以及接收端的逐层解析](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/e1a13d0410c2b03463ea6adf436e5335d53ea679/hft/assets/hft-06/websocket-message-flow.png)

### 一次 recv() 的三种"不对齐"

应用收到字节后，不能直接当完整消息解析，因为各层都有自己的分片规则：

- **WebSocket 层**：帧带有操作码、长度和分片信息，用来识别文本、二进制数据，以及 Ping、Pong、Close 等控制消息；一条业务消息可以拆成多个帧。[WebSocket RFC 6455](https://www.rfc-editor.org/rfc/rfc6455)
- **TCP 层**：只提供连续字节流，没有边界。一次 `recv()` 既可能只读到半个帧，也可能读到多个帧。因此接收程序要自己做**粘包/半包处理**：先完成 TLS 解密和 WebSocket 消息重组，攒出一条完整业务消息，再把完整 JSON 交给解析器。
- **TCP 丢包**：后续字节要等缺失部分补齐后才能按序交付（队头阻塞），这段等待也会原样传递到行情解析——即使后面的字节早就到了，也不能越过缺口先解析。

客户端发送的 WebSocket 帧还要按协议使用掩码（服务器发来的帧不使用掩码，这是协议硬性规定）；加密由 TLS 负责。若连接协商了 `permessage-deflate` 扩展，接收链上还会增加解压步骤——用 CPU 换带宽，低延迟场景要权衡。[WebSocket 压缩扩展](https://www.rfc-editor.org/rfc/rfc7692)、[TCP 规范](https://www.rfc-editor.org/rfc/rfc9293.html)

对于这类入口，本地优化可以减少协议和解析开销；但交易所选择的推送频率、服务端排队以及网络距离，仍决定消息何时能够到达。客户端更换网卡或使用内核旁路，改变的只是本机这一段。

### JSON 解析慢在哪，SIMD 怎么帮忙

JSON 易于查看，但解析器面对的是文本。例如 `"price":"123.4500"`，程序要先找到字段名和字符串边界，再把价格转换为可计算的数值；消息里还有对象、数组、转义字符和不同类型的值。逐字节判断字符类型、维护解析状态（现在在字符串里吗？这个引号是结束还是被转义？），会产生大量重复的分支判断——分支预测失败还会打断 CPU 流水线。

SIMD（单指令多数据）允许一条指令同时处理多个数据元素。一个直观的比喻：标量指令像一个工人一次检查一个字符；256 位 SIMD 则像 **32 个工人站成一排，同时检查 32 个字节**。例如，一个 256 位向量可以装下 32 个字节，并对这些字节同时进行比较。simdjson 把这种能力用于 JSON 扫描：按块读取输入，用向量比较和查表同时识别引号、反斜杠、空白及 `{ } [ ] : ,` 等结构字符，再把结果压成位掩码——位掩码中的每一位对应输入里的一个字符位置，后续可以用少量位运算（而不是逐字节循环）处理整块结果，例如快速跳过字符串、定位逗号、建立结构索引，再按需解析字段。[simdjson 论文](https://arxiv.org/abs/1902.08318)

![simdjson 扫描示意：并行分类字符，过滤字符串内的逗号，生成索引，再按需解析字段](https://raw.githubusercontent.com/Obscurepawn/TradingSystemLearning/e1a13d0410c2b03463ea6adf436e5335d53ea679/hft/assets/hft-06/simdjson-parse.png)

如果是Crypto场景典型的WebSocket + JSON的场景，类似于simdjson的手段是一个非常有效的降低延迟的方法——因为这条链路上 TLS、文本解析这类"本机必做的工作"占比高，而它们恰好适合向量化和批量化处理。

## 延伸阅读

- 内核路径：[Linux NAPI](https://docs.kernel.org/networking/napi.html)、[网络栈扩展](https://docs.kernel.org/networking/scaling.html)、[socket 接口](https://man7.org/linux/man-pages/man7/socket.7.html)。
- 可靠 UDP：[KCP 官方说明](https://github.com/skywind3000/kcp/blob/master/README.en.md)、[KCP 核心实现](https://github.com/skywind3000/kcp/blob/master/ikcp.c)；对照 [TCP 快速重传](https://www.rfc-editor.org/rfc/rfc5681.html)、[TCP 选择确认](https://www.rfc-editor.org/rfc/rfc2018.html)、[TCP 超时重传](https://www.rfc-editor.org/rfc/rfc6298.html)、[TCP 丢包检测](https://www.rfc-editor.org/rfc/rfc8985.html)。
- 用户态与硬件旁路：[TCPDirect](https://github.com/Xilinx-CNS/tcpdirect)、[Cisco ExaNIC](https://www.cisco.com/c/en/us/td/docs/dcn/nexus3550/smartnic/sw/user-guide/cisco-nexus-smartnic-user-guide/internals.html)、[DPDK](https://doc.dpdk.org/guides/prog_guide/ethdev/ethdev.html)、[RDMA](https://networking-docs.nvidia.com/doca/archive/3-5-0/rdma-aware-networks-programming-guide)。
- 消息与解析：[WebSocket RFC 6455](https://www.rfc-editor.org/rfc/rfc6455)、[simdjson 论文](https://arxiv.org/abs/1902.08318)、[simdjson On-Demand](https://github.com/simdjson/simdjson/blob/master/doc/ondemand_design.md)。
