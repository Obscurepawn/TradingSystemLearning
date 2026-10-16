# 时钟相对稳定性采样

这个示例比较两只时钟的读数，保存相对偏差随时间的变化，估计观测窗口内的平均相对频率差。它适合文章中的实验和离线分析，不用于交易热路径，也不把 Python 调用耗时解释为硬件时钟的读取性能。

`collect.py` 需要 Linux 和 Python 3.8+；`analyze.py` 与合成数据测试只需要 Python 标准库。采样器只调用读时钟接口，以 `O_RDONLY` 打开 PHC，不设置系统时间、不调整频率、不改变 PTP 配置。读取 `/dev/ptpN` 需要相应设备权限。

## 采样

在此目录执行：

```bash
# 观察系统校正后时间相对于 RAW 的变化；约 10 分钟。
python3 collect.py --target realtime --reference raw \
  --interval 0.1 --count 6001 --output realtime_vs_raw.csv

# 读取网卡 PHC，与系统 TAI 时间比较。
python3 collect.py --target /dev/ptp0 --reference tai \
  --interval 0.1 --count 6001 --output phc_vs_tai.csv
```

支持 `realtime`、`monotonic`、`raw`、`tai` 和 `/dev/ptpN`。相应常量、内核或驱动能力不存在时会报错。PHC 的设备编号需要先根据实际网卡确认；PHC 与 `CLOCK_TAI` 的时间尺度和系统 TAI 偏移也需要核对，不能因名称接近就认定它们已同步。

程序对每个样本按顺序读取：

```text
reference_before → target → reference_after
```

CSV 保存三个原始整数纳秒值及 `bracket_ns = reference_after - reference_before`。用独立于这些被测读数的 `time.monotonic_ns()` 调度采样，并保存计划与实际开始时间；这不代表调度时钟来自独立的物理振荡器。调度落后时跳过错过的时隙，不连续补采。

全部样本先存入内存，采样结束后才写 CSV，过程中不逐样本打印。内存使用随样本数增长，不适合无限期运行。按 Ctrl+C 会保存已有样本并以非零状态退出；强制终止进程或机器故障会丢失尚未落盘的样本。输出路径必须不存在，以免覆盖已有实验数据。

## 分析

```bash
python3 analyze.py realtime_vs_raw.csv --series realtime_vs_raw_series.csv

# 根据实际观测选择阈值；这里的 10000 ns 只是命令示例。
python3 analyze.py phc_vs_tai.csv --max-bracket-ns 10000 \
  --series phc_vs_tai_filtered_series.csv
```

程序向标准输出返回 JSON，`--series` 额外保存每个保留样本的相对偏差和拟合残差。原始 CSV 始终保留；启用 bracket 过滤时同时报告总样本数、保留数、剔除数，以及过滤前后的 bracket 分布。不要只展示过滤后结果而隐去剔除比例。

对第 \(i\) 组读数定义：

$$
r_i=\frac{r_i^-+r_i^+}{2},\qquad
o_i=t_i-r_i,\qquad
x_i=\frac{r_i-r_0}{10^9},\qquad
y_i=o_i-o_0.
$$

其中 \(x_i\) 的单位是秒，\(y_i\) 的单位是纳秒。最小二乘拟合 \(y_i=\alpha+\beta x_i+e_i\)，得到：

$$
\text{相对频率差（ppm）}=\frac{\beta}{1000}.
$$

正值表示目标时钟相对于参考时钟走得更快。代码先用整数减去起点，再转换为浮点数，避免直接对约 \(10^{18}\) 量级的 Unix 时间纳秒值做浮点拟合。中点和偏差在转换前用二倍整数表示，保留 0.5 ns；这些数值分辨率不代表测量准确度。

| 输出 | 含义 |
|---|---|
| `initial_target_minus_reference_ns_exact` | 第一个保留样本的目标时间减参考中点；可能包含不同时钟的起点差异 |
| `relative_offset_change_ns` | 相对第一个保留样本的偏差变化分布 |
| `relative_frequency_ppm` | 整个保留窗口内拟合出的平均相对频率差 |
| `absolute_residual_ns` | 去掉拟合常数和线性趋势后的绝对残差分布 |
| `bracket_all_nonnegative_ns` / `bracket_kept_ns` | 过滤前后非负读取区间宽度的分布 |
| `observed_reversals` | 原始记录中直接可见的时钟反向读数计数 |

分位数使用 nearest-rank 定义，样本较少时 p99 接近或等于最大值。程序检测到反向读数后仍输出原始偏差统计，但不拟合频率；需要检查记录并按连续阶段拆分。程序没有实现完整跳变检测，向前跳时、伺服状态变化以及参考源切换仍可能污染拟合。即使没有反向读数，也应检查偏差曲线和同步日志。

## 结果的边界

**相对变化不等于绝对准确度。** 两只钟共同偏离 UTC 时，它们的差值仍可能很稳定。参考时钟本身的频率变化也会进入观测结果。`realtime`、`monotonic`、`raw` 可能由同一个 Linux clocksource 派生；`REALTIME` 与 `RAW` 的比较适合观察系统校正等相对变化，不能独立证明底层振荡器相对外部标准的稳定性。

**bracket 描述顺序读取的时间区间。** 在参考时间连续递增、三次读取按预期顺序执行，且目标读数对应的采样时刻确实落在两次参考读取之间时，用前后参考读数的中点近似目标读数对应的参考时间。Python 调用、系统调用、调度和中断都会影响区间宽度。`bracket / 2` 只描述这种夹取方法的时间定位尺度，不是相对 UTC 的误差上界，也不能排除固定读取偏差。更紧的 PHC／系统交叉时间戳需要考察内核、驱动与设备支持。

**拟合残差包含测量路径和同步伺服的影响。** 小残差不代表绝对时间准确，也不能单独归因于振荡器稳定。已同步时钟的结果描述“振荡器与校正机制共同作用”的状态。要研究自由运行或失锁后的保持能力，应在获准的独立实验环境中安排状态变化，并记录温度、负载和参考源状态；本程序不改变这些状态。

这个程序只做全窗口线性拟合，不计算 Allan 偏差，不替代针对不同平均时间的频率稳定度分析。建议至少查看三张图：偏差变化随时间、拟合残差随时间、bracket 宽度随时间；对主钟切换、失锁和重锁阶段分别分析。

## 验证与资料

```bash
python3 -m unittest discover -s . -p 'test_clock_stability.py' -v
```

合成测试验证已知 +10 ppm 频率差、纳秒级残差、改变时间起点后的拟合不变性、过滤计数、反向读数抑制拟合，以及离线 CLI 不覆盖文件。开发环境为 macOS ARM64，已运行这些测试和 CLI 检查；Linux 采样及真实 PHC 尚未实机验证，本文没有由该示例产生的硬件稳定性结论。

- [Python：`clock_gettime_ns`、`monotonic_ns` 和 `sleep`](https://docs.python.org/3/library/time.html)：整数纳秒接口与采样调度语义。
- [Linux man-pages：`clock_gettime`](https://man7.org/linux/man-pages/man3/clock_gettime.3.html)：clock ID 语义和 `FD_TO_CLOCKID` 转换。
- [Linux 内核：PTP hardware clock infrastructure](https://docs.kernel.org/driver-api/ptp.html)：PHC 字符设备及 POSIX 时钟接口。
