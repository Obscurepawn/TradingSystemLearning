# HFT Engineering从入门到入土（4）—— 深入理解TSC、PHC和时钟同步

判断交易系统快慢，先要知道用来测量耗时的读数从哪里来。

一条行情到达网卡后，程序读取行情、更新订单簿、执行策略，最后发出订单。网卡可以记录收发报文的时间，程序可以在各阶段读取 CPU 计数器，交易所回报里也可能带有自己的时间戳。这些数字虽然都表示时间，起点、单位和生成方式却未必相同。

解释一个时间戳，要知道**它记录了什么事件、由哪个时钟生成、采用什么时间基准，以及存在多大的误差**，才能判断哪些读数可以相减、差值覆盖了哪段耗时。CPU 计数器部分以 x86 TSC 为例。文中的计算数字和日志输出用于演示；引用资料中的实测结果另有来源说明。

<a id="sec-1"></a>
## 1. 高频交易系统为什么需要时钟

### 1.1 同一个交易系统，需要回答几种不同的时间问题

分析策略决策时，我们关心计算用了多久；排查跨机故障时，希望把几台机器的日志对应起来；观察一次下单时，则关心从行情到订单的 tick-to-order 延迟。这些任务对时钟的要求有所不同：

| 场景 | 具体问题 | 对时钟的主要要求 |
|---|---|---|
| 进程内性能分析 | 更新订单簿和策略计算各花了多久？ | 区间可比较、读取成本低、打点不扰乱热路径 |
| 本机行情到发单 | 行情进入网卡，到订单离开网卡用了多久？ | 收发边界清楚、硬件时间戳可对应 |
| 跨机日志与单向延迟 | A 发出后，B 何时收到？ | 两端时间基准可比较，同步误差足够小 |

### 1.2 从一条行情到一笔订单，先标清测量边界

沿用前文的记号：`H0` 是网卡接收行情的硬件打点，`C0` 是应用取得行情的时刻，`C4` 是订单完成风控与编码的时刻，`H1` 是网卡发送订单的硬件打点。

![行情到订单的数据路径与时钟域](assets/hft-04/clock-domains-path.png)

`C4 - C0` 覆盖应用内部处理；`H1 - H0` 覆盖本机从接收行情到发出订单的路径。固定测量口径后，优化前后的结果才有可比性。

网卡常使用 PHC（PTP Hardware Clock，精确时间协议的硬件时钟）生成时间戳。如果 `H0`、`H1` 由同一个 PHC 产生，且测量区间没有时钟跳变，通常可以直接计算这两个硬件打点之间的时间。

<a id="sec-2"></a>
## 2. 先分清我们需要哪一种时间

### 2.1 绝对时间、时间间隔与时钟域

绝对时间把事件放到一条公共时间线上，例如日志中的日期与时刻。时间间隔描述两个事件之间经过了多久，关注的是这段持续时间。

时钟域在这里指采用同一套计时基准和解释规则的时间戳集合，不同时钟域的起点、计数单位、频率和接受的时间校正都可以不同。一台机器可以同时有 CPU TSC、多个网卡 PHC，以及具有不同语义的系统时钟；两个进程也可以读取同一台机器上的相同系统时钟。因此，时钟域与机器、进程并非一一对应。

<a id="sec-3"></a>
## 3. 时间从哪里来

### 3.1 振荡器产生节拍，计数器累计节拍

本地计时依靠振荡器和计数器：振荡器产生周期信号，计数器记录经过了多少个周期。软件再利用频率和一个已知时间点，把计数换算成时间：

$$
t=t_0+\frac{c-c_0}{f}
$$

其中，`c0` 是基准时刻的计数，`t0` 是它对应的时间，`f` 是用于换算的计数频率。换算结果取决于三个因素：`t0` 准不准、`f` 稳不稳定，以及读取 `c` 的瞬间处于被测过程的哪个位置。

石英振荡器利用晶体的机械谐振产生频率参考。振荡器会通过不同设计减小环境影响，温度、振动和器件老化仍会影响其频率。[NIST：石英振荡器][nist-quartz]

服务器的 RTC（实时时钟）通常由备用电池供电，在主机关闭后仍继续记录日期和时刻。操作系统启动时，可以读取 RTC，为系统日历时间设定初值。[Linux RTC 手册](https://man7.org/linux/man-pages/man4/rtc.4.html)

系统运行后，Linux 利用 CPU 的 TSC 等硬件计数器计算时间。内核保存一个基准时间及其对应的计数值；程序读取时间时，读取函数取得当前计数，将计数增量换算成经过的时间，加到基准时间上，就得到当前时间。这条读取路径不需要访问 RTC。[Linux timekeeping][timekeeping]

### 3.2 CPU TSC 与 RDTSC

x86 的 TSC（Time Stamp Counter）是处理器提供的时间戳计数器，`RDTSC` 是读取它的指令。读取结果是计数值，通过相应的换算关系才能把计数差解释成纳秒。

在支持 invariant TSC 的处理器上，TSC 按固定速率推进，不随核心的动态频率状态一起变化。

固定的计数速率解决了“按什么速度走”的问题。用两个 TSC 读数相减来测量耗时，还要考虑下面三种情况。

**第一种：起点和终点在不同的 CPU 核上读取。** 两个核心即使计数速度相同，同一时刻的读数也未必相同。假设核心 B 始终比核心 A 多计数 100，两者的变化如下：

| 时刻 | 核心 A 的 TSC | 核心 B 的 TSC |
|---|---:|---:|
| 开始测量时 | 1000 | 1100 |
| 经过 200 个计数后 | 1200 | 1300 |

如果线程在 A 上读取起点，随后迁移到 B 上读取终点，直接相减得到 `1300 - 1000 = 300`。实际只经过了 200 个计数，多出来的 100 来自两个核心的计数偏移。

同一台机器的不同 socket 之间也存在这个问题。socket 指物理 CPU 插槽；双路服务器中的两颗 CPU 各自包含多个核心。线程换核，或由两个线程分别记录起点和终点时，能否直接相减取决于这些核心的 TSC 是否对齐，以及剩余偏差是否满足测量要求。始终在同一个逻辑 CPU 上完成两次读取，可以避开跨核计数偏移；跨核测量则要结合平台的同步保障与实际偏差来判断。[Linux TSC 同步说明](https://docs.kernel.org/virt/kvm/x86/timekeeping.html#tsc-synchronization)

**第二种：两次都在同一个核上读取，但读数没有夹住被测计算。** 源代码虽然写成“读取起点 → 执行计算 → 读取终点”，CPU 却可以在计算尚未完成时执行结束处的裸 `RDTSC`。这样得到的差值漏掉了后半段耗时。计数器仍然正常递增，错误出在读取时间戳的时机。约束 CPU 的执行顺序和编译器重排，才能把打点放到预期位置；5.4 节给出了具体的指令结构。[Intel 指令集参考][intel-sdm-vol2]

**第三种：起点和终点来自不同的服务器。** 例如，服务器 A 在发送消息时读取 TSC，服务器 B 在收到消息时读取 TSC。两台机器各有自己的计数起点与频率。即使都支持 invariant TSC，也只是各自的计数速率不随核心动态频率变化，两台机器未必已经对时。直接用 B 的接收计数减去 A 的发送计数，会把时钟之间的差异也算进网络延迟。跨机测量时，先将两边读数转换到共同的时间基准，评估同步和换算误差，再计算时间差。

### 3.3 网卡 PHC 与交换机时钟

PHC 是 PTP Hardware Clock 的缩写。Linux 驱动支持 PHC 接口时，可以把设备时钟注册为 `/dev/ptpN`，应用通过相关接口读取时间、查询设备能力，或调整时间和频率。具体支持哪些功能，由设备和驱动决定。[Linux PHC 接口][linux-phc]

网卡在报文经过指定硬件位置时捕获时钟读数，形成硬件时间戳。多端口网卡可以共享 PHC，也可以采用其他时钟布局。各端口使用哪个 PHC，要根据设备的实际时钟布局确认。

交换机同样可以在端口或内部转发路径中记录时间。

### 3.4 原子钟与卫星授时提供什么

原子钟利用稳定的原子跃迁频率作为参考，通过反馈控制本地振荡器，再由计数构造时间。它仍然涉及振荡、计数和校准，只是参考机制具有更好的长期稳定性。[NIST：原子振荡器][nist-atomic]

交易服务器通常通过授时链路取得参考。一个常见部署是：GNSS（全球卫星导航系统，例如 GPS）接收机提供时间与频率参考，主时钟设备结合本地振荡器生成授时输出，再通过网络把时间分发给服务器。GPS 驯服振荡器会持续调整本地振荡器，使它与接收的参考信号保持一致。[NIST：驯服振荡器][nist-disciplined]

<a id="sec-4"></a>
## 4. 时钟为什么逐渐不一致

### 4.1 先分清偏移、频率误差和漂移

本地时钟比参考时钟领先 5 微秒，这 5 微秒就是**时间偏移**，描述的是两个时钟现在差多少。

如果本地时钟每秒比参考时钟多走 1 微秒，说明它走得偏快，**相对频率误差**为 +1 ppm。ppm（parts per million）表示百万分之一。假如它一直保持这个速度，期间没有校正，那么 10 秒后又会多走 10 微秒，加上原先领先的 5 微秒，就一共领先了 15 微秒。

如果偏快的程度也随时间变化，例如温度升高后，从每秒多走 1 微秒逐渐变成每秒多走 2 微秒，这就是**频率漂移**。

### 4.2 一次时钟对齐为什么不够

把两个时钟在某个时刻设成相同读数，只消除了当时的偏移。只要后续推进速度不同，偏差还会重新增长。温度变化、预热、供电与器件老化，又会让频率误差随时间变化。[NIST：漂移与驯服][nist-disciplined]

同步程序持续观测偏差，并调整时间或频率，让两个时钟保持对齐。直接把时钟改到另一个值称为跳变校正（step）；逐渐改变推进速度、让偏差慢慢消失称为渐进校正（slew）。采用哪种策略，由同步软件和配置决定。

### 4.3 测量噪声也会出现在偏差曲线里

在用户态先读时钟 A，再读时钟 B，两次读取之间存在时间差。系统调用、设备访问和线程抢占会拉长这个间隔；网络同步还会受到排队、打点位置和路径不对称的影响。

偏差曲线上出现尖峰时，可以检查同期的采样窗口是否变宽、同步是否失锁、参考源是否切换。这些记录能帮助判断波动来自读取过程、同步链路，还是时钟本身。

<a id="sec-5"></a>
## 5. Linux 程序如何读取时间

### 5.1 硬件计数与系统时间之间，还有 timekeeping

Linux 把用于计时的硬件计数来源称为 clocksource。在合适的 x86 平台上，TSC 可以作为 clocksource。内核的 timekeeping 机制负责把这些计数换算成系统时间：先保存某一时刻的系统时间及当时的硬件计数值，之后读取时间时，再将新增的计数换算成纳秒，加到保存的基准时间上。

假设计数器每增加 1，代表经过 2.5 纳秒，那么新增 100 个计数就对应 250 纳秒。计算时，“乘以 2.5”可以改写成“先乘以 5，再除以 2”。对于这里的非负整数，右移 1 位就相当于除以 2 后舍去余数，因此也可以写成“先乘以 5，再右移 1 位”。

代码中的 `mult` 就是乘数，`shift` 就是右移的位数；在这个例子里，分别是 `5` 和 `1`。实际使用的参数由内核根据计数器频率预先计算。换算过程可以简化成下面两行，其中 `delta_cycles` 是新增的计数，`delta_ns` 是换算出的纳秒数，`base_ns` 是之前记下的基准时间，也以纳秒表示：

```text
delta_ns ≈ (delta_cycles * mult) >> shift
time_ns  ≈ base_ns + delta_ns
```

这样就能用整数运算把计数换算成纳秒。实际代码还要处理计数器的位数限制和溢出，遵守各类时钟的计时规则，并保证一次读取使用的是同一轮更新的数据。[Linux timekeeping][timekeeping]

通过 PTP 校准 PHC、再校准系统时间时，网卡调整 PHC，内核更新系统时间的基准和换算参数。**物理 TSC 仍然提供计数，并未被设成某个 UTC 数值。**

![时间参考、系统时钟和应用读取路径](assets/hft-04/clock-reading-sync.png)

同步程序持续校正时钟，应用沿图中的读取路径取得时间。部署时可以选择不同的 clocksource 或参考源。

### 5.2 clock_gettime 的重点在 clock ID

调用 `clock_gettime(clockid, &ts)` 时，`clockid` 决定读取哪个时钟，也决定返回值的含义。常用选择如下：[Linux clock_gettime][clock-gettime]

| 接口或时钟 | 适合的用途 | 主要边界 |
|---|---|---|
| `CLOCK_REALTIME` | 日历时间、带外部时间含义的记录 | 可能发生跳变，也接受频率校正 |
| `CLOCK_MONOTONIC` | 耗时测量、期限与超时判断 | 不随日历时间的直接跳变而跳变，但接受频率校正；不包含系统挂起时间 |
| `CLOCK_MONOTONIC_RAW` | 观察未受软件频率校正的计时行为 | 保留本地频率误差；RAW 不代表更接近外部参考 |
| `CLOCK_BOOTTIME` | 需要包含系统挂起时间的期限 | 在 MONOTONIC 的基础上，把系统挂起期间经过的时间也计入 |
| `CLOCK_TAI` | 采用国际原子时（TAI）的时间记录 | 依赖系统时间与 TAI 偏移配置 |
| `CLOCK_THREAD_CPUTIME_ID` | 当前线程消耗的 CPU 时间 | 不是包含排队和等待的真实经过时间 |

TAI 是连续的原子时标；UTC（协调世界时）以 TAI 为基础，通过闰秒保持与地球自转时间接近。[BIPM：UTC 与 TAI][bipm-utc]

两个进程读取同一台主机上的 `CLOCK_MONOTONIC`，通常可以直接关联时间点；两台机器的 `CLOCK_MONOTONIC` 则没有天然共同起点。`clock_getres()` 返回时钟的分辨率。要知道读数与参考时钟之间最多相差多少，还得通过校准或测量确定。

在 C++ 中，`std::chrono` 用 `time_point` 表示时间点，用 `duration` 表示两个时间点之间的时长。不同的时钟类型决定这些读数有什么含义；底层调用哪个接口，则由标准库和操作系统决定。下面以 Linux 上常见的 libstdc++ 实现为例。

**`steady_clock`：用来测经过了多久**

`steady_clock` 适合测量函数耗时、设置超时期限。C++ 要求它的读数随着时间推进不倒退，`is_steady` 为 `true`。这里的“不倒退”允许连续两次读到相同值。它的计时起点由实现决定，主要用于计算两个时间点之差，不用于表示某年某月某日。[C++ 时钟要求][cpp-clocks]

在上述 Linux 实现中，`steady_clock::now()` 调用 `clock_gettime(CLOCK_MONOTONIC, ...)`，再把返回的秒和纳秒包装成 C++ 时间点。C++ 标准库负责封装读取结果，保持单调的是 Linux 的计时机制。[libstdc++ 的时钟读取实现][libstdcpp-chrono-source]

Linux 根据硬件计数的增量推进时间，同时维护日历时间与单调时间之间的偏移。当管理员把日历时间拨回 30 秒时，内核会同步调整这个偏移，抵消拨钟对单调读数的影响，使单调时间保持连续。也就是说，“现在是几点”变了，“已经经过了多久”仍然连续累计。[Linux 时间校正实现][linux-timekeeping-source]

Linux 的 `CLOCK_MONOTONIC` 仍会接受 NTP 等同步程序对走时速度的微调，因此这里的 `steady_clock` 与 `CLOCK_MONOTONIC_RAW` 不同。线程被抢占、等待 I/O 的时间都会计入这个时钟测得的耗时；整个系统挂起期间的时间则不计入。[Linux clock_gettime][clock-gettime]

**`system_clock`：用来记录事件发生在什么时候**

`system_clock` 表示系统的日历时间，适合记录行情到达、订单发出等事件的日期和时刻。从 C++20 起，标准明确规定它使用 Unix 时间：从 1970 年 1 月 1 日 00:00:00 UTC 起计算经过的时间，不计闰秒。[C++ system_clock][cpp-system-clock]

在上述 Linux 实现中，`system_clock::now()` 读取的是 `CLOCK_REALTIME`。[libstdc++ 的时钟读取实现][libstdcpp-chrono-source] 管理员改时间或同步程序执行跳变校正时，它的读数可以向前跳，也可以向后跳。[Linux clock_gettime][clock-gettime]

例如，一段代码实际运行了 2 秒，期间日历时间被拨回 30 秒，用 `system_clock` 的结束读数减去开始读数，就会得到约 -28 秒；同一段代码用 `steady_clock` 测量，仍是约 2 秒。因此，日志记录时刻用 `system_clock`，计算处理耗时用 `steady_clock`。

**`high_resolution_clock`：名字强调分辨率，是否单调要看实现**

`high_resolution_clock` 表示实现中计时刻度最短的时钟。标准允许它直接作为 `system_clock` 或 `steady_clock` 的别名；采用别名时，两者使用相同的底层时钟，精度也相同。[C++ high_resolution_clock][cpp-high-resolution-clock]

例如，libstdc++ 把它定义为 `system_clock` 的别名，因此也会受日历时间跳变影响；libc++ 在支持单调时钟的平台上，则把它定义为 `steady_clock` 的别名。同一段使用 `high_resolution_clock` 的代码，更换标准库后，时钟行为就可能不同。[libstdc++ 类型定义][libstdcpp-chrono-header]、[libc++ 类型定义][libcpp-high-resolution-clock]

用纳秒表示读数，只是选择了纳秒作为单位。实际能分辨的间隔、与参考时间的误差、调用一次的耗时，都要在目标机器上测量；它们不会因为单位是纳秒就自动达到 1 纳秒。当 `high_resolution_clock::is_steady` 为 `true` 时，它保证读数不倒退。[C++ Clock 要求][cpp-clock-requirements]

### 5.3 glibc 与 vDSO：一次函数调用未必进入内核

在使用 glibc 的 Linux 程序中，应用通过 glibc 调用 `clock_gettime`。当平台、时钟种类和内核实现支持时，glibc 会调用 vDSO（virtual Dynamic Shared Object）：这是内核映射到进程地址空间的小型共享对象，代码在用户态运行。[Linux vDSO][vdso]

以动态链接 glibc 的 Linux x86-64 程序为例，`libc.so.6` 和动态链接器 `ld-linux` 都映射在进程的用户虚拟地址空间中。内核还会映射两块相关区域：`[vdso]` 存放可在用户态执行的代码，`[vvar]` 存放时间基准、换算参数等数据。应用只能读取这些时间数据，由内核负责更新。[Linux vDSO 映射实现][vdso-mapping]

启动时，内核通过辅助向量中的 `AT_SYSINFO_EHDR` 告知 vDSO 的基址。glibc 找到需要的函数入口并保存下来，之后读取时间时就能直接调用。[Linux vDSO][vdso]、[glibc vDSO 初始化][glibc-vdso-setup]

![进程内存中的 libc、vDSO、vvar，以及读取时间和系统调用回退路径](assets/hft-04/vdso-memory-dataflow.png)

图中左侧展示同一进程中的内存区域，右侧展示调用和数据流。内存区域的顺序与大小仅作示意；快速路径以 `CLOCK_MONOTONIC` 等受支持的高精度时钟、TSC 被选作计数来源的情况为例。

在快速读取路径中，glibc 调用 `__vdso_clock_gettime`。vDSO 先读取 `[vvar]` 中的时间基准与换算参数，再读取 TSC，计算当前时间。内核更新参数与应用读取可以并发发生，因此 vDSO 会检查序列号；如果读取期间数据发生更新，就在用户态重新读取，避免混用两轮更新的参数。成功后，vDSO 把秒和纳秒写入调用者提供的 `timespec`，再沿调用链返回。[Linux vDSO 实现][vdso-source]

系统调用回退有两个入口：

- **glibc 没有找到可用的 vDSO 入口**：直接发起系统调用，让内核处理这次查询。[glibc clock_gettime 实现][glibc-clock-gettime]
- **已经进入 vDSO，但这次查询无法走快速路径**：例如请求的 `clockid` 或当前硬件计时模式不受支持，vDSO 会在内部执行回退代码，通过 `syscall` 指令进入内核。[Linux vDSO 实现][vdso-source]、[x86-64 回退代码][vdso-fallback]

PHC 等设备时钟有自己的读取路径，访问设备的开销要另外测量。

这条调用链上，glibc 提供应用接口，vDSO 执行用户态读取，内核 timekeeping 管理时间状态，硬件计数器提供计数。在以 TSC 为 clocksource 的快速路径中，vDSO 本身也要读 TSC，再完成时间换算和序列号检查。

### 5.4 直接读 TSC 时，要同时控制测量边界

TSC 速率稳定，也要在正确的位置读取，才能测准耗时。CPU 的乱序执行会改变读取时机：在满足指令依赖和体系结构规则的前提下，处理器可以让后面的指令先执行，或让多条指令的执行过程重叠。**源代码中的计时起点和终点，未必就是实际执行时的测量边界。**

例如，被测代码先加载数据，再根据数据完成计算。如果结束处直接使用裸 `RDTSC`，一次可能的执行交错如下：

```text
希望测到：起点读数 → 发起加载 → 数据返回 → 计算完成 → 终点读数
可能发生：起点读数 → 发起加载 → 终点读数 → 数据返回 → 计算完成
```

`RDTSC` 不会自动等待此前所有指令完成，所以加载仍在等待数据时，终点读数就可能已经取得，漏掉后半段耗时。起点同样存在问题：被测指令可以在起点读数完成前开始执行。区间外的工作还可能与被测代码重叠、争用资源，使结果混入额外等待。这类误差来自测量边界，增加采样次数或校准 TSC 频率无法修复错误的指令顺序。[Intel SDM：RDTSC、RDTSCP][intel-sdm-vol2]

在支持 `RDTSCP` 的 Intel x86-64 处理器上，测量本核执行区间可以采用下面的指令结构。这里省略了保存读数和计算差值的指令，展示的是最终机器码中的排序关系：

```text
LFENCE
RDTSC       ← 取得起点计数
LFENCE

被测指令

RDTSCP      ← 取得终点计数
LFENCE
```

起点前的 `LFENCE` 等待此前指令在本核完成执行；起点后的 `LFENCE` 阻止被测指令越过读钟位置提前执行。终点的 `RDTSCP` 等待此前指令执行及加载完成，后面的 `LFENCE` 再阻止后续指令越过终点读钟位置。[Intel SDM：LFENCE、RDTSCP][intel-sdm-vol2]

上述结构不会等待此前的存储操作全部变为全局可见。若测量目标包含这一步，可以按 Intel 文档在终点 `RDTSCP` 前加入 `MFENCE`；若起点也要等待此前存储全局可见，可以使用 `MFENCE; LFENCE; RDTSC`。如果要测到消费线程已经读取并处理完数据，则由消费端打点或确认；存储全局可见时，消费线程还未必完成这些工作。[Intel SDM：RDTSC、RDTSCP][intel-sdm-vol2]

编译器重排发生在更早的阶段。即使 CPU 屏障安排正确，编译器也可能提前计算表达式、把计算移出区间，或删除没有被使用的结果。以 GCC 内联汇编为例，`asm volatile` 可以防止读钟汇编因输出未使用而被删除，或被当成不变结果移出循环；`memory` clobber 约束编译器对内存访问的移动。纯寄存器计算仍可能跨越这些汇编。被测结果要被实际使用，输入输出约束也要表达必要的数据依赖，最后检查优化后的汇编，确认目标计算仍在计时区间内。[GCC 扩展汇编][gcc-extended-asm]

屏障和读钟本身也有成本，测得的差值会包含部分测量开销。可以先测同样指令结构的空区间，观察基线及其波动，再测目标代码。屏障会改变指令原本可以重叠的执行过程，因此空区间耗时无法作为一个通用常数，从任意结果中精确扣除。在交易热路径的每个阶段都加屏障，还会扰动被测程序；评估打点方案时，也要观察提高打点频率后延迟怎样变化。

### 5.5 读得准与执行得准是两个问题

用单调时钟计算出一个撤单期限，只说明目标时刻如何表示。线程何时醒来、何时获得 CPU、何时完成发单，仍受调度和执行路径影响。

假设线程要等待 20 µs 再继续执行。调用 `nanosleep()` 或 `clock_nanosleep()` 后，线程会进入休眠，定时器到期后还要等待调度器让它重新运行。接口接收纳秒单位的参数，实际恢复时间却还包含唤醒和调度的等待。[Linux clock_nanosleep][clock-nanosleep]

如果线程已经运行在专用 CPU 上，而且允许等待期间持续占用 CPU，对 20 µs 这样的短等待，**我会优先选择忙等：反复读取单调时钟，到期限就退出循环。** 这样省去了主动休眠后的唤醒和调度过程。DPDK v25.11 的 `rte_delay_us()` 默认采用这一类实现：读取计数器，检查经过的时间，在循环中执行 `rte_pause()`。[DPDK 延时实现][dpdk-delay-source]

这种短等待的一个实际用途，是按原始节奏回放行情，测试系统处理真实流量的表现。Beeks 在 2024 年介绍 mdPlay 的技术文章中，比较了三种回放方案：先录制某大型交易所繁忙时段的一小时行情，再回放并重新抓包，比较逐包间隔和累计时间偏差。回放用于算法验证、压力测试和故障复现，还原行情的微突发是其中一个关键要求。[Beeks：Multicast Market Data Replay][beeks-market-replay]

例如，抓包记录中的三个行情报文按下面的时间到达。表中选取 20 µs 作为间隔，实际回放使用每个报文自己的时间戳。

| 报文 | 相对首包的到达时间 | 回放时的目标发送时刻 |
|---|---:|---|
| A | 0 µs | `t0` |
| B | 20 µs | `t0 + 20 µs` |
| C | 40 µs | `t0 + 40 µs` |

如果把三个报文紧挨着发送，就人为制造了更密集的突发；如果每次原本想等 20 µs，却等了 80 µs，被测系统就有了更多空闲时间，测试压力偏低。精确控制等待时间，是为了尽量保持原始流量节奏。

软件回放可以先确定起点 `t0`，再按表中的绝对期限提交报文。如果从 `t0` 到准备好 B 已经过了 3 µs，就只等待剩下约 17 µs。C 的目标时刻仍是 `t0 + 40 µs`，这样不会把前面发送和等待的额外开销逐次累加到后续期限。

这段软件等待控制的是应用提交发送的时刻，报文真正离开网卡还受发送队列和设备处理影响。Beeks 的对比中，一种方案在软件中等待发送时机；**在那次测试中表现最好的方案则把报文提前交给专用网卡，由网卡自己的时钟控制发送**。[Beeks 的回放实现与测试][beeks-market-replay]

<a id="sec-6"></a>
## 6. 网卡和交换机如何给事件打时间戳

### 6.1 打点位置决定了延迟包含什么

应用在接收函数返回数据后读取时间，记录的是应用已经拿到数据的时刻。在常规内核收包路径中，报文此前已经经过网卡、DMA（直接内存访问）、驱动和内核网络栈，还可能经历调度等待。采用内核旁路后，处理路径会变化，但从硬件收包到应用取得数据仍然有一段过程。

硬件时间戳由设备在报文经过指定位置时生成，减少了应用调度对记录时刻的影响。打点可以位于 PHY（物理层）、MAC（介质访问控制层）或其他参考位置。解释一个时间戳时，还要看设备记录的是报文首部还是尾部到达的时刻，以及是否做了固定延迟补偿。

发送方向也一样：应用提交发送请求后，数据还可能在队列里等待；设备要等报文经过发送打点位置时，才生成硬件时间戳。用提交时刻还是硬件发送时刻作为终点，决定了测量是否包含这段排队与设备处理。

### 6.2 Linux socket 与专用 SDK 如何交付时间戳

在标准 socket 路径中，应用配置设备与驱动支持的硬件打点能力，并通过 `SO_TIMESTAMPING` 请求相应类型的时间戳。接收时间戳通过 `recvmsg()` 的辅助数据返回；发送时间戳则在稍后通过 `recvmsg(..., MSG_ERRQUEUE)` 读取。[Linux 网络时间戳][timestamping]

这条路径将时间戳与业务 payload 分开交付。多个发送请求同时在途时，时间戳返回顺序可以与 `send()` 调用顺序不同，Linux 提供时间戳关联 ID 等机制，用来找到原来的发送事件。TCP 还存在字节流与报文边界的差异：一条业务消息可以跨包，一个包也可以包含多条业务消息，因此将报文时间戳对应到订单，还要依据消息层的规则。

专用网卡还可以把时间戳写进 SDK 约定的接收元数据。以 Cisco Nexus SmartNIC（原 ExaNIC）及 `libexanic v2.7.6` 为例，网卡通过 DMA 将接收数据写入主机内存中的环形缓冲区。这个缓冲区由 128 字节的块（chunk）组成：前 120 字节是帧数据区，后 8 字节是时间戳、长度和状态等元数据。SDK 通过用户态映射读取这片接收内存。[Cisco 接收架构][exanic-internals]、[SDK 接收映射][exanic-rx-mapping]

![ExaNIC 接收缓冲区的 chunk 布局、时间戳字段与 SDK 输出](assets/hft-04/exanic-rx-timestamp-layout.png)

图中的 `payload[120]` 存放以太网帧的字节片段，包括相应位置的协议头。**后面的 8 字节是网卡写入主机内存时附带的元数据，不是网络报文自身的尾部。** 一个较长的帧会占用多个 chunk；`length = 0` 表示当前 120 字节已满、后面还有块，非零则表示这是最后一块，并给出这块有效的帧字节数。[接收块结构定义][exanic-rx-layout]

`timestamp` 占 4 字节，记录帧开始到达时网卡时钟计数的低 32 位。同一帧的各个 chunk 携带相同的时间戳，因此 SDK 从末块取出这个值时，取得的仍是帧开始到达的时刻。其余字段分别记录帧状态、命中的过滤规则，以及环形缓冲区绕回的代数。[接收块结构定义][exanic-rx-layout]、[Cisco 接收接口说明][exanic-lib]

应用调用完整帧接收接口时，SDK 分别返回帧字节与时间戳：

```c
// rx 已指向接收缓冲区。
char buf[2048];
exanic_cycles32_t timestamp;
ssize_t len = exanic_receive_frame(rx, buf, sizeof(buf), &timestamp);
// len > 0 时，buf 中有 len 字节的完整帧，timestamp 中有硬件时间戳。
```

SDK 将各个 chunk 的有效帧字节拼接、复制到 `buf`，并把时间戳写入独立变量 `timestamp`。`buf` 中包含以太网帧头、数据和 FCS（帧校验序列），不包含那 8 字节接收元数据。`timestamp` 的原始值以网卡时钟计数为单位；接收后及时用 `exanic_expand_timestamp()` 恢复高位、扩展为 64 位，可以避免低 32 位回绕产生的歧义，再通过 `exanic_cycles_to_ns()` 等接口换算时间。[SDK 帧接收实现][exanic-rx-source]、[Cisco 时间戳接口说明][exanic-lib]

### 6.3 镜像报文中的时间戳与旁路测量

交换机还可以复制业务报文，在镜像副本上附加硬件时间戳，再送给旁路采集机分析。交易程序照常处理业务流量，采集机负责解码时间戳、关联报文和统计延迟，减少业务线程为观测而逐包读钟的工作。

以 Cisco Nexus 3550-F Fusion 为例，它在报文第一 bit 到达入口端口时记录时间，等镜像副本从镜像端口输出时，再把已记录的时间戳写入副本。时间戳对应业务报文进入交换机的时刻，早于镜像副本发出的时刻。[Cisco 镜像与时间戳说明][fusion-mirror]

在它的 `timestamp append` 模式下，镜像帧的布局如下。这里省略前导码等物理层字段，帧数据包括以太网头和上层协议数据：

```text
原始帧：  [ 帧数据 ][ 原 FCS：4 B ]
镜像副本：[ 帧数据 ][ 时间戳计数：4 B ][ 新 FCS：4 B ]
```

时间戳占据原 FCS 的位置，设备再为输出帧计算新的 FCS，镜像帧总长度增加 4 字节。这个字段保存的是硬件计数器的低 32 位；设备还会周期性发送 keyframe，提供完整计数与 UTC 时间的对应关系。采集端据此恢复时间，Cisco 也提供了现成的解码程序。[Cisco 镜像与时间戳说明][fusion-mirror]、[Cisco 时间戳解码工具][fusion-decoder]

这里的时间戳作为镜像帧的字节在线路上传输。6.2 节中的 ExaNIC 接收元数据则位于主机的 DMA 缓冲区，交换机复制网络报文时无法取得服务器内存中的那份元数据。

测量一段网络路径的延迟时，可以在两个位置分别镜像同一个业务报文：

```text
业务路径：发送端 → A 的入口（记录 tA）→ 网络路径 → B 的入口（记录 tB）→ 接收端

镜像路径：A 的副本及 tA ──┐
                         ├→ 旁路采集机 → 配对同一报文 → 计算 tB − tA
          B 的副本及 tB ──┘
```

换算到共同时间基准后，假如同一个报文在 A 入口的时间为 `1,000,000 ns`，在 B 入口的时间为 `1,000,800 ns`，两个入口之间的延迟就是 `800 ns`。这段时间包含 A 收包后的转发和两点间的网络传输，终点停在 B 的入口。

即使两份副本经过不同的镜像队列、先后顺序发生变化，只要原始时间戳保留、报文配对正确，仍然可以计算上述差值。

在 HFT 中，可以让行情和订单经过同一台交换机，从外部测量 tick-to-order 延迟。以 Fusion 为例，端口 1 接行情网络，端口 2 接交易服务器，端口 3 接旁路采集机，对端口 1、2 的入口流量配置带时间戳的镜像：

1. 行情的第一 bit 到达端口 1，硬件记录 `t_tick`，业务报文沿正常路径转发给服务器。
2. 镜像副本通过端口 3 发往采集机，携带刚才记录的 `t_tick`。这个过程由交换机完成，不用等服务器的接收函数返回，也不用等策略处理行情。
3. 服务器处理行情并发出订单。订单的第一 bit 返回交换机、到达端口 2 时，硬件记录 `t_order`，再将带时间戳的订单副本送给采集机；原订单继续沿正常交易链路转发。

打时间戳和发送镜像副本是两个时刻。在 Fusion 的 `timestamp append` 模式下，镜像路径采用存储转发：收齐整帧后再发送，镜像输出繁忙时还会排队。副本携带的仍是入口处已经记录好的时间；业务转发路径不因这个镜像模式而增加等待。服务器可以在行情镜像副本仍在排队时处理行情并发出订单。[Cisco 镜像转发方式][fusion-mirror]

采集机建立行情与订单的对应关系后，计算 `t_order − t_tick`。这里两个打点共用交换机的硬件计数器，测量边界是 **“行情进入交换机 → 订单返回交换机”**。它包含交换机向服务器的转发、两者之间的往返链路、网卡收发和服务器内部处理。终点停在订单返回交换机入口，尚未包含后续到交易所的传输、撮合或确认。

这种外部测量适合以下场景：

- **生产监控与故障回溯。** 采集和分析运行在独立机器上，交易线程省去了为这项外部测量额外逐包读钟、写采集日志的工作。采集机可以统计开盘、行情突发时的响应分布，保留延迟异常时对应的行情和订单。例如，策略函数自身计时仍然很短，但网卡或处理队列开始积压，外部测得的 tick-to-order 就会变长。
- **第三方系统的外部评估。** 对不方便修改内部代码的交易系统，可以从网络入口和出口观察其表现。解析协议并建立输入与输出的业务关系后，就能按统一的外部测量边界比较结果，避开各家内部日志对“处理完成”的不同定义。

行情和订单是不同消息。计算 tick-to-order 时，要依据触发规则、业务标识或应用提供的关联记录，确定哪条行情触发了哪笔订单，无法配对的样本单独记录。若要进一步确定慢在行情解析、策略计算还是发单队列，就结合服务器内部的分段打点分析。

<a id="sec-7"></a>
## 7. 多台设备如何同步时钟

### 7.1 同步需要估计偏移，还要处理传输延迟

主时钟发出“现在是某个时刻”的消息后，消息还要经过网络才能到达接收端。如果接收端直接把时钟设成消息里的时间，就会落后这段传输时间。

NTP（Network Time Protocol）和 PTP（Precision Time Protocol）都要估计时间偏移与网络延迟。NTP 常用于同步系统日历时间；PTP 可以利用设备的硬件时间戳，并让交换机参与对时，适合受控网络中的精细同步。同步误差取决于参考源、打点位置、网络路径和时钟控制算法。[NTPv4 RFC][ntp-rfc] [LinuxPTP ptp4l][ptp4l]

LinuxPTP 是在 Linux 上实现 PTP 协议的开源软件包。PTP 规定设备之间怎样交换授时信息，LinuxPTP 提供的程序则负责收发这些消息，并持续校正本机时钟。`ptp4l` 和 `phc2sys` 是其中两个常用程序。[LinuxPTP 项目介绍][linuxptp]

### 7.2 用四个时间戳理解 PTP

以主时钟为参考，从时钟的读数减去同一时刻主时钟的读数，得到偏移 `θ`：正值表示从时钟偏快，负值表示偏慢。下面用一次端到端消息交换计算这个偏移，假定交换期间 `θ` 近似不变，并暂时忽略打点噪声和中间设备的修正项。

![PTP 四时间戳与双向路径延迟](assets/hft-04/ptp-four-timestamps.png)

四个时间戳分别是：

| 时间戳 | 事件 | 使用的时钟 |
|---|---|---|
| `t1` | 主时钟发送 Sync | 主时钟 |
| `t2` | 从时钟接收 Sync | 从时钟 |
| `t3` | 从时钟发送 Delay_Req | 从时钟 |
| `t4` | 主时钟接收 Delay_Req | 主时钟 |

令主到从的延迟为 `df`，从到主的延迟为 `dr`，则：

$$
t_2-t_1=d_f+\theta,\qquad
t_4-t_3=d_r-\theta
$$

如果往返路径对称，即 `df = dr = d`，就能得到：

$$
\widehat{\theta}=\frac{(t_2-t_1)-(t_4-t_3)}{2},\qquad
\widehat d=\frac{(t_2-t_1)+(t_4-t_3)}{2}
$$

从时钟自己记录了 `t2` 和 `t3`，还需要主时钟把 `t1` 和 `t4` 告诉它。在两步模式下，主时钟先发送 Sync，再通过 `Follow_Up` 消息补充它的准确发送时刻 `t1`；收到 `Delay_Req` 后，通过 `Delay_Resp` 消息返回接收时刻 `t4`。拿齐这四个时间戳，从时钟就能按上述公式估计时间偏移和链路延迟。[NIST PTP 测量说明][nist-ptp]

### 7.3 硬件时间戳无法自动消除路径不对称

如果两个方向延迟不同，却仍按对称公式估计偏移，会得到：

$$
\widehat{\theta}-\theta=\frac{d_f-d_r}{2}
$$

例如两个方向的剩余延迟相差 400 ns，估计出的偏移就会多出或少掉 200 ns。增加采样次数有助于观察随机波动，却消除不了这种固定的方向差异。

硬件打点主要减少软件收发路径带来的时间波动。两个方向的光纤长度、设备内部延迟或打点位置不同，仍会造成路径不对称。LinuxPTP 的 `ptp4l` 提供 `delayAsymmetry`、`ingressLatency` 和 `egressLatency` 等配置，用于填写有校准依据的补偿值。[LinuxPTP ptp4l][ptp4l]

### 7.4 交换机中的边界时钟与透明时钟

**主时钟 G 提供参考时间，交易服务器 A、B 通过交换机 S 接入授时网络，各自把网卡 PHC 对准 G。** A、B 分别跟随参考时钟，不互相对时。如果 S 只做普通转发，PTP 报文在两个方向上的排队时间不同，就会干扰偏移估计。[Cisco：交换机延迟与 PTP][cisco-ptp-switch]

S 可以配置成边界时钟，也可以配置成透明时钟。下面用 A 的对时过程说明两种方式，B 独立执行相同流程。两组计算都采用两步消息模式，时间单位为 **μs**，时间戳省略共同的日期时间前缀；假定每轮交换期间时钟偏移不变，扣除交换机驻留时间后，两个方向的链路延迟相等。

**边界时钟 BC（Boundary Clock）：G 先给 S 对时，S 再给 A、B 对时**

S 的上联端口作为从时钟跟随 G，下联端口作为主时钟给 A、B 授时。G 发来的对时消息在 S 结束，S 用自己的硬件时钟生成下游的 Sync 消息，因此这是两段独立的对时交换。[TI：边界时钟][ti-ptp]

![边界时钟的两段对时：先校正交换机时钟，再校正服务器 PHC](assets/hft-04/ptp-boundary-worked-example.png)

第一段，G 与 S 交换消息，记录到：

| 时间戳 | 事件 | 读取的时钟 | 读数（μs） |
|---|---|---|---:|
| `t1` | G 发出 Sync | G | 100 |
| `t2` | S 收到 Sync | S | 112 |
| `t3` | S 发出 Delay_Req | S | 210 |
| `t4` | G 收到 Delay_Req | G | 202 |

S 在本地记录 `t2`、`t3`，再通过 `Follow_Up`、`Delay_Resp` 取得 G 记录的 `t1`、`t4`，便可以计算：

```text
正向读数差 = 112 − 100 = 12 μs
反向读数差 = 202 − 210 = −8 μs

S 相对 G 的偏移 = [12 − (−8)] / 2 = +10 μs
单向链路延迟   = [12 + (−8)] / 2 =   2 μs
```

正向的 `12 μs` 包含“链路延迟 + S 的偏移”，反向的 `−8 μs` 包含“链路延迟 − S 的偏移”。两者相减，链路延迟抵消，剩下两倍的偏移，所以再除以 2 就得到偏移。相加则会抵消偏移，得到往返链路延迟。

`+10 μs` 表示 S 比 G 快了 10 μs。反向读数差为负，是因为发送端和接收端使用不同的时钟；真正的反向链路延迟仍是正数。

为方便继续演算，设 S 校正后已与 G 对齐。第二段由 S 给 A 对时，另一次交换得到：

| 时间戳 | 事件 | 读取的时钟 | 读数（μs） |
|---|---|---|---:|
| `t1` | S 发出新的 Sync | S | 1000 |
| `t2` | A 收到 Sync | A 的 PHC | 1033 |
| `t3` | A 发出 Delay_Req | A 的 PHC | 2030 |
| `t4` | S 收到 Delay_Req | S | 2003 |

```text
正向读数差 = 1033 − 1000 =  33 μs
反向读数差 = 2003 − 2030 = −27 μs

A 相对 S 的偏移 = [33 − (−27)] / 2 = +30 μs
单向链路延迟   = [33 + (−27)] / 2 =   3 μs
```

A 据此得知自己的 PHC 比 S 快了 30 μs，可以进行校正。S 已经跟随 G，A 对准 S 就间接对准了 G；S 剩余的同步误差也会传给下游。

**透明时钟 TC（Transparent Clock）：A 与 G 对时，S 提供转发耗时**

把 S 换成透明时钟后，A 的对时对象是 G。下面采用端到端（E2E）方式：G 与 A 交换四个时间戳，S 记录正向 Sync 和反向 Delay_Req 各自在交换机内停留了多久，并把这些驻留时间随 PTP 消息交给 A。[Cisco：透明时钟][cisco-ptp-switch]

![透明时钟的双向报文时间线、驻留时间补偿与 PHC 偏移计算](assets/hft-04/ptp-transparent-worked-example.png)

这一轮交换，G 与 A 记录到：

| 时间戳 | 事件 | 读取的时钟 | 读数（μs） |
|---|---|---|---:|
| `t1` | G 发出 Sync | G | 1000 |
| `t2` | A 收到 Sync | A 的 PHC | 1043 |
| `t3` | A 发出 Delay_Req | A 的 PHC | 2030 |
| `t4` | G 收到 Delay_Req | G | 2007 |

S 用自己的同一个计时基准记录报文进入、离开的时刻：

| 报文方向 | 进入 S（μs） | 离开 S（μs） | S 内部的驻留时间 |
|---|---:|---:|---:|
| 正向 Sync：G → A | 5002 | 5010 | `5010 − 5002 = 8 μs` |
| 反向 Delay_Req：A → G | 6003 | 6005 | `6005 − 6003 = 2 μs` |

S 的读数与 G、A 相差很大，仍然可以计算驻留时间：两次打点都来自 S 的同一个时钟，相减就能得到报文经过 S 的耗时。这一步依赖 S 的计时频率准确，无须让 S 的读数与 G、A 相同。

在图示的两步模式中，A 从本地网卡取得 `t2`、`t3`，从 `Follow_Up` 取得 `t1` 及正向驻留时间修正，从 `Delay_Resp` 取得 `t4` 及反向驻留时间修正。这样，A 就有了四个端点时间戳和两项修正量。S 的原始进出读数只用于在本地计算修正量，无须全部传给 A。[TI：透明时钟的消息处理][ti-ptp]

先从两个方向的读数差里，分别扣除交换机已经测出的驻留时间：

```text
扣除正向驻留后的读数差 = (1043 − 1000) − 8 =  35 μs
扣除反向驻留后的读数差 = (2007 − 2030) − 2 = −25 μs
```

再按双向测量的办法求偏移和剩余链路延迟：

```text
A 相对 G 的偏移     = [35 − (−25)] / 2 = +30 μs
剩余单向链路延迟   = [35 + (−25)] / 2 =   5 μs
```

算出的 `5 μs` 是 G 到 S、S 到 A 两段链路的单向延迟之和，图中分别为 2 μs 和 3 μs。A 通过这轮消息交换就能估计总和，无须预先知道每段链路用了多久。

如果忽略 S 提供的修正，直接拿原始的 `43 μs` 和 `−23 μs` 计算，就会得到 `(43 − (−23)) / 2 = 33 μs`，把 A 的偏移多估计了 3 μs。误差来自两个方向驻留时间的不对称：`(8 − 2) / 2 = 3 μs`。因此，**8 μs 和 2 μs 是报文在交换机内的耗时，30 μs 才是这次测出的 PHC 偏移。**

这里补偿的是交换机内部的停留时间，剩余链路仍依赖双向对称假设。采用 P2P（Peer-to-Peer，逐跳）方式时，设备还会测量相邻端口之间的链路延迟，并把它纳入修正。[TI：逐跳链路测量][ti-ptp]

**根据部署选择 BC 或 TC**

两种方式都能让 A、B 跟随 G，也都能为跨机延迟测量提供时间基准。选择主要取决于同步网络怎样组织：

| 部署需求 | 可优先评估的方式 | 原因 |
|---|---|---|
| 保留服务器与 G 的端到端对时关系，同时补偿中间交换机的转发等待 | TC | 交换机逐包测量 PTP 驻留时间，交给终端补偿；例如几台交易服务器经支持 TC 的交换机跟随 G。[Juniper：透明时钟][juniper-ptp-tc] |
| 给多个机柜、大量服务器分层授时，分担上游的 PTP 交互负载 | BC | 每个 BC 跟随上游，再负责给自己的下游授时，减少主时钟直接服务的对象。[Juniper：边界时钟][juniper-ptp-bc] |

例如采用 E2E 对时的 10 个机柜，每柜 40 台服务器：如果沿途只有 TC，各服务器的 Delay_Req 仍要到达 G，由 G 响应；如果每柜部署一个 BC，柜内服务器就向本柜 BC 发请求，G 主要处理这 10 个 BC 的对时请求。BC 在这里的价值是分担授时负载，实际精度还取决于各层时钟和链路质量。

**测出偏移后，如何把 PHC 对准参考时钟**

在上述部署中，服务器上的 `ptp4l` 收集硬件时间戳和修正信息，计算本机 PHC 相对参考时钟的偏移，再由时钟控制算法决定怎样调整 PHC。BC 模式下 A 的直接参考是 S，TC 模式下则是 G。

如果采用直接跳时，校正量就是偏移的相反数：A 快 30 μs，就将当前 PHC 时间减去 30 μs；若 B 在自己的交换中测得慢 20 μs，就将当前 PHC 时间加上 20 μs。以同一个参考时刻为例：

| 设备 | 测得偏移 | 调整时的 PHC 读数（μs） | 校正量 | 校正后的读数（μs） |
|---|---:|---:|---:|---:|
| A | +30 μs | 1,000,030 | −30 μs | 1,000,000 |
| B | −20 μs | 999,980 | +20 μs | 1,000,000 |

校正量要加到**执行调整那一刻的 PHC 当前读数**上。`t1`、`t2` 等时间戳记录的是过去的时刻，直接把它们写回时钟，会漏掉测量后已经过去的时间。[LinuxPTP 的 PHC 校正逻辑][linuxptp-clock-adjust]

另一种做法是平滑调频：让偏快的 PHC 暂时走慢一点，或让偏慢的 PHC 暂时走快一点，逐渐消除已有偏移。是否直接跳时，由 `ptp4l` 的配置和控制算法状态决定；程序也会持续交换 PTP 消息、调整走时速度，以跟踪晶振的频率变化。[LinuxPTP ptp4l][ptp4l]

例如在路径稳定、期间未调整时钟的条件下，两次相隔 1 秒的测量分别得到 `+30 μs`、`+32 μs`，说明 PHC 每秒额外快了约 2 μs，也就是约 2 ppm。将它的走时速度降低约 2 ppm，可以抑制偏移继续增长；已有的偏移则由跳时或一段时间的平滑调频来消除。实际控制算法会结合连续样本，避免把单次网络测量的波动当成频率变化。

Linux 将网卡 PHC 暴露为 `/dev/ptpN` 这样的设备。`ptp4l` 通过 `clock_adjtime()` 等内核接口提交时间或频率调整，网卡驱动再把调整应用到 PHC 硬件；后续硬件时间戳就由校正后的 PHC 产生。[Linux PHC 接口][linux-phc]

以上修正信息针对 PTP 对时报文。若想导出普通行情包在 S 内部的停留时间，还要确认交换机是否另有业务报文打点与导出功能。

### 7.5 Linux 上需要明确谁在调整哪个时钟

以服务器使用网卡硬件时间戳、跟随网络主时钟的部署为例，LinuxPTP 中的两个程序分别负责一段同步：

- `ptp4l` 负责通过网络对时。它与上游主时钟交换 PTP 报文，利用收发时间戳估计偏移和链路延迟，再校正网卡的 PHC。
- `phc2sys` 负责本机内的时钟同步。在这个部署中，它以已经对准的 PHC 为参考，持续校正 Linux 系统时钟，也就是应用通过 `CLOCK_REALTIME` 读取的时间。[LinuxPTP ptp4l][ptp4l]、[phc2sys][phc2sys]

同步方向是：**网络主时钟 →（`ptp4l`）网卡 PHC →（`phc2sys`）Linux 系统时钟**。PHC 与系统时钟是两个独立的时钟，只校正 PHC，系统时钟并不会自动对准参考。同步方向也可以按部署配置反过来，例如服务器向网络提供时间时，可以先用系统时钟校正 PHC，再向其他设备授时。

`phc2sys` 还会处理 PTP 时间尺度与系统时间之间的整秒关系。使用自动配置或等待 `ptp4l` 的方式，可以取得相应偏移信息，避免在程序里永久写死秒数。`phc2sys -a` 本身不包含系统时钟，常用配置通过增加 `-r` 将系统时钟纳入同步。[LinuxPTP phc2sys][phc2sys]

`chronyd` 是 chrony 软件包中负责时间同步的后台进程。它的常见用法是通过 NTP 与时间服务器交换消息，估计本机系统时钟的偏移和走时速度，再持续校正系统时间。

在 PTP 部署中，`phc2sys` 以网卡 PHC 为参考；在 NTP 部署中，`chronyd` 以 NTP 时间服务器为参考。两者校正的是同一个系统时钟。如果同时独立调整，一个进程的动作会改变另一个进程正在观察的偏移，造成相互干扰。因此，配置时由一个服务负责实际校正系统时钟。

<a id="sec-8"></a>
## 8. 如何比较不同时钟域的时间戳

### 8.1 单机内部就可能需要转换

计算“行情到达网卡，到应用取得行情”的延迟，最直接的做法是：**应用取得行情后，再读取生成接收时间戳的那个 PHC，用当前读数减去报文的硬件时间戳 `H0`。** 两个读数来自同一个时钟，测量期间没有时钟跳变，就可以直接求差。这个方法的终点是实际读到 PHC 的时刻，比应用刚取得行情稍晚；发起读取后、硬件实际采样之前花掉的时间，也会计入结果。

是否改用 TSC，主要取决于逐报文读取 PHC 的成本。在 Linux 常规接口下，应用打开 `/dev/ptpN`，将文件描述符转换成时钟 ID，再调用 `clock_gettime()`。这种设备时钟读取会进入内核，由驱动访问网卡时钟，不走普通系统时钟的 vDSO 快速路径。[Linux PHC 接口][linux-phc]、[vDSO 实现][vdso-source]

从访问路径看，CPU 主动读网卡 PHC 通常比直接读自身的 TSC 开销更大，也会引入设备访问的延迟波动。每处理一个报文都读一次 PHC，就会让每个报文多出一次设备访问。这个成本占整条处理链路的多大比例，取决于网卡、驱动或 SDK 的读取方式，以及其他步骤的耗时，要在目标环境中测量。

随报文交付的硬件时间戳由网卡在收包时记录，再通过描述符或其他元数据交给应用。应用读取的是保存好的时间戳，这一步无须主动查询 PHC 的当前时间。[Linux 网络时间戳][timestamping]

如果逐报文读 PHC 的开销已经影响处理延迟，可以定期采集 TSC 与 PHC 的对应读数，建立换算关系；处理报文时只读 TSC，之后再换算成 PHC 时间。校准阶段仍然要读 PHC，但一组有效的换算参数可以用于许多次报文打点。这样减少了设备访问次数，同时引入了校准和换算误差。

采用这种方式后，网卡的时间戳 `H0` 来自 PHC，程序在 `C0` 处取得的是 TSC 计数。两者的单位和起点不同，先把 TSC 读数换算成 PHC 时间，才能计算时间差。

换算时，**先记录同一时刻两个时钟各自的读数，再根据 TSC 增加的计数，推算 PHC 经过了多久。**

这组对应读数由程序主动采集。例如，应用在开始测量前运行一段校准代码：用 TSC 读取指令取得 CPU 当前计数，再通过网卡 PHC 的读时钟接口取得 PHC 当前时间，把两项读数保存下来；稍后再采集一组，用来估计两个时钟的换算比例。

从采集到的记录中选一组作为换算起点，这组记录就是“基准”。基准由程序选择，可以取开始测量时的一组读数，无须从开机时开始计时。下面选择第一组读数作为基准，计算时暂且把每组中的两次读取看作发生在同一时刻；两次读取的实际时间差在 9.2 节说明。

| 校准时刻 | TSC 读数（计数） | PHC 读数（ns） |
|---|---:|---:|
| 第一次，选作基准 | 1,000,000 | 5,000,000 |
| 稍后一次 | 1,003,000 | 5,001,000 |

程序将第一组的 TSC 计数 `1,000,000` 保存为 `c0`，将对应的 PHC 时间 `5,000,000 ns` 保存为 `b`。之后提到“基准 TSC 计数”和“基准 PHC 时间”，指的就是这两个保存的值。也可以选其他有效的对应记录作为基准，但 `c0` 和 `b` 要来自同一组对应关系。

这段时间内，TSC 增加了 `3000`，PHC 增加了 `1000 ns`，因此可以估计：**TSC 每增加 3 个计数，PHC 就前进 1 ns。**

现在程序在取得行情时读到 TSC 为 `1,006,000`。利用前面保存的基准和估计出的换算比例，计算分三步：

```text
1. 本次 TSC 减去保存的基准计数：1,006,000 − 1,000,000 = 6000 个计数
2. 这些计数对应多久：  6000 ÷ 3 = 2000 ns
3. 加回基准 PHC 时间： 5,000,000 + 2000 = 5,002,000 ns
```

程序打点时对应的 PHC 时间估计为 `5,002,000 ns`。把上述三步合在一起，就得到换算公式：

$$
t_{\mathrm{PHC}}\approx a\,(c_{\mathrm{TSC}}-c_0)+b
$$

| 符号 | 含义 | 本例中的值 |
|---|---|---:|
| `c_TSC` | 要换算的那次 TSC 读数 | 1,006,000 |
| `c0` | 校准基准时刻的 TSC 读数 | 1,000,000 |
| `a` | 每个 TSC 计数对应多少 PHC 纳秒 | 1/3 ns/计数 |
| `b` | 同一个基准时刻的 PHC 读数 | 5,000,000 ns |
| `t_PHC` | 换算得到的 PHC 时间 | 5,002,000 ns |

`c_TSC − c0` 是“经过了多少个计数”，乘以 `a` 就变成“经过了多少纳秒”，加上 `b` 才得到 PHC 上的具体时刻。先减去基准，还能避免直接把很大的绝对计数转换成浮点数时损失精度。

如果网卡记录的行情到达时间 `H0` 是 `5,001,500 ns`，现在两端都用 PHC 时间表示，就能估计这段延迟：

```text
程序取得行情的时间 − 行情到达网卡的时间
= 5,002,000 − 5,001,500
= 500 ns
```

### 8.2 两台机器的单向延迟，需要两端可比较

设 A 的发送时间戳误差为 `eA`，B 的接收时间戳误差为 `eB`，真实单向延迟为 `L`，则：

$$
\widehat L=L+e_B-e_A
$$

测得负延迟时，可以从三个方向排查：两个打点的时钟是否已对准，时间尺度是否一致，发送和接收记录是否属于同一个事件。即使两台机器都运行了 PTP，也要确认实际生成时间戳的时钟受到了同步，并评估剩余误差。

测量往返时间时，A 可以用同一个时钟记录请求发出和响应返回，在本地完成求差。该区间包含去程、B 的处理与回程，直接除以二会把处理时间与路径不对称一起带入单向网络延迟的估计。

<a id="sec-9"></a>
## 9. 检查时钟稳定性

判断一个时钟是否稳定，要观察它**相对另一个更可信的参考时钟**如何变化。在第 7、8 章的部署中，网卡 PHC 跟随主时钟，软件再建立 TSC 计数到 PHC 时间的换算关系；TSC 本身仍在自由计数，没有被校正成 PHC 时间。检查时分别观察这两层关系，以及不同 socket 的 TSC 是否一致：

| 检查对象 | 参考 | 主要指标 | 结果直接影响什么 |
|---|---|---|---|
| PHC 对主时钟 | 主时钟；最好再有一路独立的 GNSS PPS | offset 的分布与峰值、`freq` 的变化、主时钟切换和失锁事件 | 跨机时间戳能不能比较 |
| TSC 对 PHC | 已同步的 PHC | 采样窗口宽度、按 8.1 节换算外推的误差 | 用 TSC 换算出的硬件时间准不准 |
| 不同 socket 的 TSC | 同一台机器上的另一颗 CPU | 偏移区间及其变化、TSC_ADJUST 和内核同步记录 | 两个线程分别打点后，读数能否直接相减 |

一次测量会受到哪些误差影响，取决于读数经过的换算和同步路径。例如，把 TSC 换算成 PHC 时间，再与另一台机器的 PHC 时间戳相减，就同时受到换算误差和两台机器同步误差的影响。

### 9.1 PHC 与主时钟

`pmc` 可以向正在运行的 `ptp4l` 查询当前同步状态。下面两条命令只读状态，不会调整时钟：

```bash
pmc -u -b 0 'GET TIME_STATUS_NP'   # master_offset、gmIdentity
pmc -u -b 0 'GET PORT_DATA_SET'    # portState
```

第一条命令回复中的 `master_offset` 和 `gmIdentity` 分别表示最近一次偏差估计与当前最终主时钟的身份：

```text
master_offset              20
gmIdentity                 001122.fffe.334455
```

这里 `master_offset 20` 表示 PHC 相对上游主时钟的最近一次偏差估计为 +20 ns；`gmIdentity` 表示当前跟随的是哪台最终主时钟。`portState` 则来自第二条命令的回复；在本文的客户端部署中，跟随上游的端口正常进入 `SLAVE` 状态。`pmc` 默认连接 `/var/run/ptp4l`、使用 domain 0，部署不同时用 `-s`、`-d` 指定。[pmc 手册][pmc]、[pmc 输出实现][linuxptp-pmc-output]

`ptp4l` 的日志采用另一种格式。逐次输出会把偏差写成 `master offset`，中间是空格，并带上 `s2` 等校时状态：

```text
ptp4l[1000.000]: master offset 20 s2 freq +1200 path delay 500
```

`summary_interval` 控制日志统计窗口；窗口内包含多次更新时，输出改为下面的汇总形式：

```text
ptp4l[1016.000]: rms   24 max   70 freq  +1200 +/-   8 delay   500 +/-  12
```

`rms 24 max 70` 表示这个窗口内偏差的 RMS 是 24 ns，最大绝对偏差是 70 ns。它们由窗口内多个偏差样本统计得到，替代了逐次日志中的 `master offset`。这行汇总日志不包含 `gmIdentity`、`portState` 或 `s2`；主时钟身份和端口状态仍通过前面的 `pmc` 命令查询。`freq +1200 +/- 8` 是频率控制值的均值和标准差，单位为 ppb，程序把它取反后作用到 PHC 上。`delay` 是 PTP 估计的单向路径延迟。[ptp4l 手册][ptp4l]、[ptp4l 日志与校正逻辑][linuxptp-clock-log]

结合 `pmc` 查询结果、逐次日志和汇总日志，可以观察以下变化：

- **偏差有没有变大**。观察汇总日志中的 `max` 和 RMS 随时间怎样变化；如果还要计算偏差的分位数，需要保留逐次偏差样本。出现尖峰时，再结合同期的网络和主时钟切换记录排查。
- **频率补偿量有没有变化**。`freq` 是时钟控制器为了跟上主时钟计算的补偿量，观察它随时间的变化，可以近似了解 PHC 晶振相对主时钟的频率误差。均值缓慢移动，并与机房温度一起变化，是晶振受环境影响的典型表现。
- **同步状态有没有变**。持续查询 `pmc`，可以发现 `gmIdentity` 改变或端口离开 `SLAVE`；逐次日志中的 `s0`/`s1` 则反映校时控制器尚未锁定或请求跳时。这些变化发生时，相关时间段的时间戳要单独检查。

### 9.2 TSC 与 PHC

8.1 节用两组 TSC–PHC 对应读数建立了换算关系 $t_{\mathrm{PHC}}\approx a\,(c_{\mathrm{TSC}}-c_0)+b$。这里检查的是换算结果有多准，误差主要来自三个方面：

1. **采样窗口**。每组中的两个读数先后取得，两次读取之间的时间差会影响对应关系。
2. **两个时钟的相对频率在变**。TSC 按本地计数频率推进，PHC 则接受 `ptp4l` 的频率调整，所以斜率 `a` 会随时间变化。
3. **距上次校准过了多久**。换算参数使用得越久，斜率误差累积得越多。

采集 TSC–PHC 对应读数时，可以按“TSC 前读数 → PHC 读数 → TSC 后读数”的顺序读取，并按 5.4 节约束指令顺序。PHC 的采样时刻落在两次 TSC 读取之间；这个窗口越宽，对应关系就越不确定。用两次 TSC 读数的中点与 PHC 配对时，半个窗口宽度可以表示这项采样不确定性。

内核还提供了 PHC 与**系统时钟**的交叉时间戳接口。`PTP_SYS_OFFSET_PRECISE` 依赖设备和驱动支持，返回设备时间、系统日历时间和原始单调时间；部分平台借助 Intel ART 等硬件建立对应关系。`PTP_SYS_OFFSET_EXTENDED` 则在读取 PHC 前后记录系统时间，返回“系统时间前 / PHC / 系统时间后”三项读数。这些接口返回的是系统时钟读数，并非原始 TSC 计数，不能直接当作 8.1 节的 TSC–PHC 校准数据。[PTP UAPI][ptp-uapi]

下面的 `testptp` 命令可用于检查设备支持哪种交叉时间戳能力，以及系统时钟与 PHC 采样窗口有多宽：

```bash
testptp -d /dev/ptp0 -X      # 交叉时间戳：成功说明网卡和驱动支持 PRECISE
testptp -d /dev/ptp0 -x 10   # 10 组“系统时间前 / PHC / 系统时间后”读数，观察窗口宽度
```

### 9.3 跨 Socket / NUMA Node 的 TSC

同一台机器不同核心上的 TSC 读数未必相同。如果两个线程分别在不同 socket 上记录起点和终点，读数相减时就会带入跨 socket 的偏移。检查的重点是：**这个偏移有多大，是否已经影响耗时测量。**

**先看系统层面的证据：**

```bash
# invariant TSC：频率恒定，不随 C-state 停止
grep -o -w -E 'constant_tsc|nonstop_tsc' /proc/cpuinfo | sort | uniq -c

# 内核启动和 CPU 上线时的同步检查结果
dmesg | grep -i -E 'TSC synchronization|TSC warp|TSC ADJUST|unstable'

# IA32_TSC_ADJUST（MSR 0x3B）：每个逻辑 CPU 一份，记录 TSC 被软件写过的偏移量
sudo modprobe msr && sudo rdmsr -a -d 0x3b | sort | uniq -c
```

Linux 会检查同一个 socket 内各逻辑 CPU 的 `TSC_ADJUST` 是否一致；不同 socket 为了补偿偏移，可以保留不同的调整值。因此，看到全机读数不全相同，要结合 CPU 拓扑和内核日志判断。几条关键日志的含义如下：

| 日志 | 含义 |
|---|---|
| `TSC ADJUST differs within socket(s), fixing all errors` | 同一个 socket 内各 CPU 的 TSC_ADJUST 不一致，通常是固件写过，内核已把它们统一 |
| `TSC ADJUST compensate: CPU... observed ... warp` | 内核检测到跨 socket 偏移，并通过 TSC_ADJUST 做了补偿 |
| `Measured ... cycles TSC warp between CPUs, turning off TSC clock.` | 补偿后仍然不同步，内核放弃使用 TSC 作为 clocksource |

内核的检查逻辑位于 `arch/x86/kernel/tsc_sync.c`。新上线的 CPU 和一颗已在线的 CPU 抢同一把自旋锁，轮流读 TSC，看后读到的值是否比先读到的小。一个 package 的第一颗 CPU 测 20 ms，后续 CPU 测 2 ms。[Linux tsc_sync.c][tsc-sync-source]

这个检查检测的是读数是否回跳，并不直接给出精确偏移。锁在两颗 CPU 之间交接也要花时间，会掩盖较小的偏移。源码注释提到的约 200 个 TSC 周期是同步机制的开销量级，随 CPU、节点距离和频率变化，不能当作所有平台都满足的误差上界。检查通过只说明这轮检查没有发现回跳。

**双向乒乓测量可以把偏移限制在一个区间内。** 假定一个短测量窗口内，两边的 TSC 计数单位相同，偏移近似不变。CPU A 把自己的 TSC 写进一条缓存行，CPU B 看到后立即读自己的 TSC，得到 $d_1=\delta+L_1$；B 再把自己的 TSC 写回去，A 看到后得到 $d_2=-\delta+L_2$。其中 $\delta=\mathrm{TSC}_B-\mathrm{TSC}_A$ 是两颗 CPU 的偏移，$L_1$、$L_2$ 是从发送方打点到接收方打点经过的时间，以 TSC 计数表示，均大于 0。重复多轮，两个方向各取最小值：

$$
-\min d_2 \;<\; \delta \;<\; \min d_1,\qquad
\hat\delta=\frac{\min d_1-\min d_2}{2},\qquad
\text{半宽}=\frac{\min d_1+\min d_2}{2}
$$

在上述条件下，“传递耗时大于 0”给出了左侧的偏移区间。如果两个方向的最短传递耗时相同，中点估计就等于真实偏移；无法确认这一点时，中点只是区间内的一个估计值，距真实偏移最多相差半个区间宽度。

```cpp
// tsc_skew.cpp：估计两颗逻辑 CPU 之间的 TSC 偏移区间。只读 TSC，不写 MSR。
// g++ -std=c++17 -O2 -pthread tsc_skew.cpp -o tsc_skew && ./tsc_skew 0 <另一 socket 上的 CPU>
#include <pthread.h>
#include <sched.h>
#include <x86intrin.h>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <limits>
#include <thread>

struct alignas(64) Mailbox { std::atomic<std::uint64_t> tsc{0}; };
Mailbox a_to_b, b_to_a;                 // 两个方向各用一条缓存行
std::atomic<int> ready{0};

inline std::uint64_t read_tsc() {       // 与 5.4 节相同：RDTSCP 后接 LFENCE
    unsigned aux;
    std::uint64_t t = __rdtscp(&aux);
    _mm_lfence();
    return t;
}

void pin_and_wait(int cpu) {
    cpu_set_t s;
    CPU_ZERO(&s);
    CPU_SET(cpu, &s);
    if (pthread_setaffinity_np(pthread_self(), sizeof s, &s) != 0) {
        std::perror("pthread_setaffinity_np");
        std::exit(1);
    }
    ready.fetch_add(1);
    while (ready.load() < 2) _mm_pause();
}

// 发送方写出自己的 TSC；接收方看到新值后立即读自己的 TSC，差值 = 偏移 + 传递耗时
void run(int cpu, Mailbox& out, Mailbox& in, bool send_first, long rounds, std::int64_t* min_d) {
    pin_and_wait(cpu);
    std::uint64_t seen = 0;
    std::int64_t best = std::numeric_limits<std::int64_t>::max();
    for (long i = 0; i < rounds; ++i) {
        if (send_first) out.tsc.store(read_tsc(), std::memory_order_release);
        std::uint64_t got;
        while ((got = in.tsc.load(std::memory_order_acquire)) == seen) _mm_pause();
        std::int64_t d = static_cast<std::int64_t>(read_tsc() - got);
        if (d < best) best = d;
        seen = got;
        if (!send_first) out.tsc.store(read_tsc(), std::memory_order_release);
    }
    *min_d = best;
}

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: %s cpuA cpuB [rounds]\n", argv[0]);
        return 2;
    }
    int a = std::atoi(argv[1]), b = std::atoi(argv[2]);
    long rounds = argc > 3 ? std::atol(argv[3]) : 1000000;
    std::int64_t d1 = 0, d2 = 0;        // d1: A→B，d2: B→A
    std::thread tb(run, b, std::ref(b_to_a), std::ref(a_to_b), false, rounds, &d1);
    std::thread ta(run, a, std::ref(a_to_b), std::ref(b_to_a), true, rounds, &d2);
    ta.join();
    tb.join();
    std::printf("CPU%d vs CPU%d: offset(B-A) in (%lld, %lld) cycles, midpoint %.1f, half-width %.1f\n",
                a, b, static_cast<long long>(-d2), static_cast<long long>(d1),
                (d1 - d2) / 2.0, (d1 + d2) / 2.0);
    std::printf("%s\n", (d1 <= 0 || d2 <= 0) ? "interval excludes 0: offset observable"
                                             : "interval contains 0: no offset observable at this resolution");
}
```

用 `lscpu -p=CPU,SOCKET,NODE` 查看逻辑 CPU 所属的 socket 和 NUMA node，从各组中选一个 CPU，与 CPU 0 逐一配对测量。结果以 TSC 计数为单位，除以 TSC 频率（MHz）再乘 1000，就能换算成 ns。各项结果可以这样理解：

- **区间包含 0**：现有样本无法排除两个 TSC 已对齐，也无法确认偏移就是 0。若直接相减而不补偿，绝对偏移的上界取两个区间端点绝对值中的较大者；若先按中点补偿，剩余偏移的上界才是半宽。
- **区间不包含 0**：这轮测量已经观察到偏移，可以结合 `TSC_ADJUST`、dmesg 和平台配置排查原因，包括固件或虚拟化层是否调整过 TSC。
- **偏移是否随时间变化**：间隔一段时间重复测量，比较整段偏移区间，而不只比较中点。中点在不确定范围内波动，不能据此判断时钟漂移；若区间持续移动到无法由采样误差解释的位置，再检查频率差或时钟调整。invariant TSC 描述本地计数速率的稳定性，不保证不同 socket 的 TSC 已对齐或严格同频。[Linux 多路 TSC 说明][linux-tsc-sync]
- **区间宽度受传递路径限制**：跨 socket 访问要经过 UPI、Infinity Fabric 等互联，得到的偏移区间往往比同 socket 更宽。区间越宽，能确定的偏移范围就越粗；半宽表示按中点补偿后的不确定性，不是一条固定的“最小可测偏移”界线。
- **NUMA node 不等于 socket**：开启 SNC（Intel）或 NPS（AMD）后，一个 socket 会分成多个 NUMA node，但仍属于同一个 CPU package。跨 NUMA node 的结果要结合 socket 拓扑解释，跨 package 的配对尤其值得检查。

如果业务要求的误差小于这个半宽，可以把两个打点放在同一个 socket 上，减少跨 socket 互联带来的测量不确定性；测量网卡收发事件时，也可以统一使用同一个 PHC 生成的硬件时间戳。

## 参考资料

- [Intel Software Developer's Manual][intel-sdm]：TSC、invariant TSC、`RDTSC` 与 `RDTSCP`。
- [Intel SDM 指令集参考卷][intel-sdm-vol2]：`RDTSC`、`RDTSCP`、`LFENCE` 与 `MFENCE` 的排序语义。
- [GCC 扩展汇编][gcc-extended-asm]：`asm volatile`、`memory` clobber 与编译器重排。
- [Linux timekeeping][timekeeping]、[vDSO 手册][vdso]与[vDSO 源码][vdso-source]：计数器、时间换算与用户态读取路径。
- [Linux clock_gettime][clock-gettime]与[clock_nanosleep][clock-nanosleep]：时钟语义、动态 clock ID 与期限。
- [Linux 网络时间戳][timestamping]、[PHC 接口][linux-phc]与[PTP UAPI][ptp-uapi]：硬件打点、时间戳交付和交叉时间戳。
- [LinuxPTP ptp4l][ptp4l]、[phc2sys][phc2sys]与[pmc][pmc]：同步职责、时间尺度和管理接口。
- [testptp][testptp]与[Linux tsc_sync.c][tsc-sync-source]：PHC 引脚、外部时间戳、交叉时间戳与内核的跨 CPU TSC 同步检查。
- [NIST 频率稳定性分析][nist-stability]与[PTP 测量说明][nist-ptp]：稳定性统计、测量条件和同步模型。

[clock-gettime]: https://man7.org/linux/man-pages/man2/clock_gettime.2.html
[vdso]: https://man7.org/linux/man-pages/man7/vdso.7.html
[timekeeping]: https://docs.kernel.org/timers/timekeeping.html
[vdso-source]: https://github.com/torvalds/linux/blob/v6.6/lib/vdso/gettimeofday.c
[vdso-mapping]: https://github.com/torvalds/linux/blob/v6.6/arch/x86/entry/vdso/vma.c
[vdso-fallback]: https://github.com/torvalds/linux/blob/v6.6/arch/x86/include/asm/vdso/gettimeofday.h
[glibc-vdso-setup]: https://github.com/bminor/glibc/blob/glibc-2.39/sysdeps/unix/sysv/linux/dl-vdso-setup.h
[glibc-clock-gettime]: https://github.com/bminor/glibc/blob/glibc-2.40/sysdeps/unix/sysv/linux/clock_gettime.c
[linux-phc]: https://docs.kernel.org/driver-api/ptp.html
[timestamping]: https://docs.kernel.org/networking/timestamping.html
[ptp-uapi]: https://github.com/torvalds/linux/blob/master/include/uapi/linux/ptp_clock.h
[linuxptp]: https://www.linuxptp.org/
[ptp4l]: https://www.linuxptp.org/documentation/ptp4l/
[linuxptp-clock-adjust]: https://github.com/richardcochran/linuxptp/blob/227bbd4715bf4cb735be4947e987032f41457585/clock.c#L2114-L2148
[linuxptp-clock-log]: https://github.com/richardcochran/linuxptp/blob/227bbd4715bf4cb735be4947e987032f41457585/clock.c#L2114-L2165
[linuxptp-phc2sys-log]: https://github.com/richardcochran/linuxptp/blob/227bbd4715bf4cb735be4947e987032f41457585/phc2sys.c#L617-L680
[linuxptp-sysoff]: https://github.com/richardcochran/linuxptp/blob/227bbd4715bf4cb735be4947e987032f41457585/sysoff.c
[phc2sys]: https://www.linuxptp.org/documentation/phc2sys/
[testptp]: https://github.com/torvalds/linux/blob/v6.12/tools/testing/selftests/ptp/testptp.c
[tsc-sync-source]: https://github.com/torvalds/linux/blob/v6.12/arch/x86/kernel/tsc_sync.c
[pmc]: https://www.linuxptp.org/documentation/pmc/
[linuxptp-pmc-output]: https://github.com/richardcochran/linuxptp/blob/227bbd4715bf4cb735be4947e987032f41457585/pmc.c#L413-L433
[chronyc]: https://chrony-project.org/doc/4.8/chronyc.html
[chrony-intro]: https://chrony-project.org/
[chrony-conf]: https://chrony-project.org/doc/4.8/chrony.conf.html
[allantools]: https://allantools.readthedocs.io/en/stable/readme_copy.html
[intel-sdm]: https://cdrdv2-public.intel.com/868137/325462-089-sdm-vol-1-2abcd-3abcd-4.pdf
[nist-quartz]: https://www.nist.gov/pml/time-and-frequency-division/popular-links/time-frequency-z/time-and-frequency-z-q-ra
[nist-atomic]: https://www.nist.gov/pml/time-and-frequency-division/popular-links/time-frequency-z/time-and-frequency-z-am-b
[nist-disciplined]: https://www.nist.gov/pml/time-and-frequency-division/popular-links/time-frequency-z/time-and-frequency-z-d
[nist-stability]: https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication1065.pdf
[nist-ptp]: https://nvlpubs.nist.gov/nistpubs/TechnicalNotes/NIST.TN.2030.pdf
[ti-ptp]: https://www.ti.com/lit/pdf/snla104
[cisco-ptp-switch]: https://www.cisco.com/c/en/us/td/docs/IIOT/switches/ie3x00/config/sys-mgmt/sys-mgmt-ie3x00/g_configuring-precision-time-protocol/precision-time-protocol.html
[juniper-ptp-bc]: https://www.juniper.net/documentation/us/en/software/junos/time-mgmt/topics/concept/ptp-boundary-clock-overview.html
[juniper-ptp-tc]: https://www.juniper.net/documentation/us/en/software/junos/time-mgmt/topics/concept/ptp-transparent-clocks.html
[ntp-rfc]: https://www.rfc-editor.org/rfc/rfc5905.html
[ethtool]: https://man7.org/linux/man-pages/man8/ethtool.8.html
[cpp-clocks]: https://eel.is/c++draft/time.clock
[cpp-system-clock]: https://eel.is/c++draft/time.clock.system.overview
[cpp-high-resolution-clock]: https://eel.is/c++draft/time.clock.hires
[cpp-clock-requirements]: https://eel.is/c++draft/time.clock.req
[libstdcpp-chrono-source]: https://github.com/gcc-mirror/gcc/blob/releases/gcc-14.2.0/libstdc%2B%2B-v3/src/c%2B%2B11/chrono.cc#L50-L94
[libstdcpp-chrono-header]: https://github.com/gcc-mirror/gcc/blob/releases/gcc-14.2.0/libstdc%2B%2B-v3/include/bits/chrono.h#L1225-L1287
[libcpp-high-resolution-clock]: https://github.com/llvm/llvm-project/blob/llvmorg-19.1.0/libcxx/include/__chrono/high_resolution_clock.h#L25-L29
[linux-timekeeping-source]: https://github.com/torvalds/linux/blob/v6.12/kernel/time/timekeeping.c#L1441-L1482
[clock-nanosleep]: https://man7.org/linux/man-pages/man2/clock_nanosleep.2.html
[gcc-extended-asm]: https://gcc.gnu.org/onlinedocs/gcc/Extended-Asm.html
[intel-sdm-vol2]: https://cdrdv2.intel.com/v1/dl/getContent/671110
[dpdk-delay-source]: https://github.com/DPDK/dpdk/blob/v25.11/lib/eal/common/eal_common_timer.c
[arm-waiting]: https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/multi-threaded-applications-arm
[accurate-sleep-blog]: https://blog.bearcats.nl/accurate-sleep-function/
[osnoise]: https://docs.kernel.org/trace/osnoise-tracer.html
[timer-slack]: https://man7.org/linux/man-pages/man2/PR_SET_TIMERSLACK.2const.html
[beeks-market-replay]: https://beeksgroup.com/blog/multicast-market-data-replay/
[exanic-internals]: https://www.cisco.com/c/en/us/td/docs/dcn/nexus3550/smartnic/sw/user-guide/cisco-nexus-smartnic-user-guide/internals.html
[exanic-lib]: https://www.cisco.com/c/en/us/td/docs/dcn/nexus3550/smartnic/sw/user-guide/cisco-nexus-smartnic-user-guide/libexanic.html
[exanic-rx-layout]: https://github.com/cisco/exanic-software/blob/430e5eb35d455b06e84ba8053b1456c86d3f21a2/libs/exanic/fifo_if.h#L15-L73
[exanic-rx-source]: https://github.com/cisco/exanic-software/blob/430e5eb35d455b06e84ba8053b1456c86d3f21a2/libs/exanic/fifo_rx.c#L365-L445
[exanic-rx-mapping]: https://github.com/cisco/exanic-software/blob/430e5eb35d455b06e84ba8053b1456c86d3f21a2/libs/exanic/fifo_rx.c#L84-L85
[fusion-mirror]: https://www.cisco.com/c/en/us/td/docs/dcn/nexus3550/3550-f/sw/user-guide/cisco-nexus-3550-f-fusion-user-guide/mirror.html
[fusion-decoder]: https://github.com/CiscoDevNet/n3550-timestamp-decoder
[arista-capture-timestamps]: https://www.arista.com/assets/data/pdf/Whitepapers/Overview_Arista_Timestamps.pdf
[trading-passive-monitoring-case]: https://www.arista.com/assets/data/pdf/CaseStudies/CaseStudy-InvestmentBank.pdf
[bipm-utc]: https://www.bipm.org/en/time-department/utc
[linux-tsc-sync]: https://docs.kernel.org/virt/kvm/x86/timekeeping.html#tsc-and-multi-socket-numa
