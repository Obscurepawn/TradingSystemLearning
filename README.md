# TradingSystemLearning

交易系统与高频交易工程的学习笔记，包含 C++ 性能优化、时钟与网络、交易所协议、回测、策略原理和系统设计。可以直接在线阅读；配套代码的依赖与运行步骤见各示例目录。

## HFT Engineering 从入门到入土

1. [建立对于 HFT 的基础认识](hft/HFT_Engineering_01_建立对于HFT的基础认识.md)
2. [C++内存与计算优化](hft/HFT_Engineering_02_C++内存与计算优化.md)
3. [C++线程通信与性能测量](hft/HFT_Engineering_03_C++线程通信与性能测量.md)
4. [高频交易系统的部署](hft/HFT_Engineering_04_高频交易系统的部署.md)
5. [高频交易系统中的时钟](hft/HFT_Engineering_05_高频交易系统中的时钟.md)
6. [网络通信：从行情进入主机到订单发出](hft/HFT_Engineering_06_网络通信.md)
7. [开源项目解析：编码、通信、记录与日志](hft/HFT_Engineering_07_开源项目解析.md)
8. [交易所协议：报文、订单簿与断线恢复](hft/HFT_Engineering_08_交易所协议.md)
9. [高频回测：时间、队列与市场反应](hft/HFT_Engineering_09_高频回测.md)
10. [出现概率万分之一的 Jitter 应该如何采样](hft/HFT_Engineering_10_出现概率万分之一的Jitter应该如何采样.md)
11. [高性能订单簿和撮合引擎的常见实现](hft/HFT_Engineering_11_高性能订单簿和撮合引擎的常见实现.md)
12. [高频交易中的特征计算实现](hft/HFT_Engineering_12_高频交易中的特征计算实现.md)

## 加密货币系统原理：从公链到交易所

1. [Bitcoin 是怎样运行的](hft/blockchain/Blockchain_01_比特币的基本原理.md)

## 文献与演讲精读

- [The Latency Slippage：缓存、并行访存与跨核交接](literature/The_Latency_Slippage_详细解读.md)
- [When Nanoseconds Matter：订单簿、通信与性能测量](literature/When_Nanoseconds_Matter_详细解读.md)

## 专题研究

- [高性能订单簿与撮合引擎：市场规则、数据结构与 C++ 实现](hft/HFT_Engineering_11_高性能订单簿和撮合引擎的常见实现.md)
- [从离散 ID 到稠密 ID：如何做高性能映射](hft/离散ID高效转换为稠密ID.md)

## 经典高频交易策略

[策略系列目录](hft/classic_strategies/)按文件编号排列，覆盖套利、做市、订单流、执行算法与加密市场机制。可从[期现套利](hft/classic_strategies/HFT_Strategy_01_期现套利.md)开始阅读，或通过[收益验证与策略总表](hft/classic_strategies/HFT_Strategy_19_收益验证与策略总表.md)比较各篇主题。

## 系统设计

- [分布式持仓服务（Positions Service）](system_design/distributed_account_service.md)

## 配套代码与实验

| 示例 | 内容与运行说明 |
| --- | --- |
| [C++20 Disruptor](hft/disruptor_cpp20/README.md) | 环形缓冲区、事件广播与消费者依赖 |
| [Stop-bit 编码](hft/examples/stop_bit/README.md) | 整数编码、增量行情解析、测试与基准 |
| [20 µs 等待](hft/examples/precise_sleep/README.md) | Linux 忙等、绝对时间睡眠与混合等待 |
| [时钟相对稳定性](hft/examples/clock_stability/README.md) | 时钟读数采样与离线分析 |
| [SBE 布局与版本](hft/examples/components_07/README.md) | 消息编码、访问器与字节核对 |
| [高频回测](hft/examples/backtest_09/README.md) | 合成事件、FIFO 队列与撤单竞态实验 |
