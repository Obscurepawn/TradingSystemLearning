# 20 µs 等待演示

这个 Linux C++17 程序演示三种等待方式，每次运行只等待一次，目标固定为 20 µs：

| 参数 | 等待方式 |
|---|---|
| `spin`（默认） | 持续读取 `CLOCK_MONOTONIC`，直到期限 |
| `sleep` | 通过 `clock_nanosleep` 睡到同一只时钟的绝对期限 |
| `hybrid` | 请求提前 10 µs 唤醒，再读钟忙等到期限；已经进入忙等窗口时直接忙等 |

10 µs 忙等窗口只是演示参数。20 µs 总等待时间很短，睡眠唤醒延迟可能超过剩余窗口；睡过最终期限后，忙等无法追回时间。混合模式不保证比纯忙等更准，也不保证在这个时间尺度上明显节省 CPU。纯忙等会在等待期间持续占用 CPU，仍可能被中断或抢占。

## 编译与使用

在仓库根目录编译：

```bash
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic \
  hft/examples/precise_sleep/precise_sleep.cpp -o /tmp/precise_sleep
```

可用以下命令选择模式：

```bash
/tmp/precise_sleep           # 默认 spin
/tmp/precise_sleep sleep
/tmp/precise_sleep hybrid
```

需要固定 CPU 时，先查询当前进程允许使用的 CPU，再选择其中一个。亲和性只限制线程在哪个 CPU 上执行，独占 CPU 还需要安排其他线程和中断。

```bash
taskset -pc $$
# 假设 CPU 2 在上面的允许列表中：
taskset -c 2 /tmp/precise_sleep spin
```

程序不调整亲和性、实时调度策略或 timer slack。Linux 的 timer slack 允许内核在一定窗口内合并定时器到期事件，以减少唤醒次数；demo 使用线程继承的值。通过 `prctl(PR_SET_TIMERSLACK, 1UL, 0UL, 0UL, 0UL)` 可将本线程的合并余量设为 1 ns，传入 `0UL` 则恢复默认值。缩小 slack 只减少合并带来的延迟，1 ns 不是总唤醒误差的保证。[Linux timer slack](https://man7.org/linux/man-pages/man2/PR_SET_TIMERSLACK.2const.html)

## 代码与输出的边界

程序读取一次 `start`，构造 `deadline = start + 20000 ns`，等待函数返回后立即读取 `end`。输出包含 `actual_wait_ns = end - start` 和带符号的 `overshoot_ns = end - deadline`。它们包含调用路径和读钟的影响；单次输出不能说明 p99、最坏等待时间或三种模式的性能差异。`end` 对应用户态等待返回后的读钟位置，业务发单和设备发送还在后续执行路径中。

`clock_nanosleep` 被信号中断后，以同一个绝对目标重试；其他错误直接报出。忙等使用 x86 `PAUSE` 或 ARM64 `yield` CPU hint；ARM64 的这条指令不会把线程交还给操作系统调度器，程序没有使用 `std::this_thread::yield()`。

非 Linux 平台可以编译，运行时提示不支持并以状态码 2 退出。本示例只进行编译与静态审阅，不运行等待实验，不提供延迟实测结果。

## 实现依据

- [DPDK v25.11 延时实现](https://github.com/DPDK/dpdk/blob/v25.11/lib/eal/common/eal_common_timer.c)：`rte_delay_us` 默认绑定到计数器轮询和 `rte_pause()` 忙等实现。
- [Arm：The when, why and how of waiting and backoff](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/multi-threaded-applications-arm)：按固定频率计数器判断等待是否结束，以及 CPU 提示指令的适用条件。
- [Making an accurate Sleep() function](https://blog.bearcats.nl/accurate-sleep-function/)：先休眠再忙等的折中。原文讨论游戏帧节奏，demo 只借鉴思路，没有采用其中的毫秒级参数或精度结论。
- [Linux clock_nanosleep](https://man7.org/linux/man-pages/man2/clock_nanosleep.2.html)：绝对期限、信号中断、返回值和调度延迟。
