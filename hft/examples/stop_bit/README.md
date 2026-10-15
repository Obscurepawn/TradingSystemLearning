
# Stop-bit 整数与增量行情解析示例

对应 [HFT C++ 低延迟文章](../../HFT_Engineering_02_C++低延迟编程基础.md) 的第 2.6、2.7 节。

- `stop_bit.hpp`：可移植 C++20 header，非空无符号 uint64_t 的 FAST 风格最短 stop-bit 编码，以及四字段教学行情的流式解析器。不是完整 FAST 实现，不支持 signed、nullable、presence map 或字典操作。
- `test.cpp`：参考实现、随机/边界及输入切分测试；参考解码器使用 GCC/Clang 支持的 128 位整数。
- `bench.cpp`：合成数据的批量平均耗时基准；使用 GCC/Clang 的编译器 barrier 防止消除结果。基准不需要第三方库。

在仓库根目录执行：

```sh
c++ -std=c++20 -O1 -g -Wall -Wextra -Wpedantic -fsanitize=address,undefined hft/examples/stop_bit/test.cpp -o /tmp/hft-stop-bit-test
/tmp/hft-stop-bit-test
c++ -std=c++20 -O3 -DNDEBUG -Wall -Wextra -Wpedantic hft/examples/stop_bit/bench.cpp -o /tmp/hft-stop-bit-bench
/tmp/hft-stop-bit-bench
```

测试依赖 `assert`，不要为测试加 `-DNDEBUG`。ASan/UBSan 用于正确性检查，性能测量使用另一份无 sanitizer 的程序。编解码及 `feed` 本身不分配内存；测试和基准在准备数据时使用 vector。

本次验证环境：Apple M1 Pro / arm64，Apple Clang 17.0.0。测试通过，文章代码块提取编译通过。基准数字和分布记录在文章中，仅代表这台机器的合成工作负载，未做绑核、真实行情回放、网络或 p99 测量，不宣称跨平台最优。

调用约定：`decode` 遇到 `need_more` 时保留 `UIntState` 并只传入新字节；`invalid` 后弃用状态。`QuoteStream` 的错误状态不可恢复，应按连接恢复协议创建新实例。回调需 `noexcept`、同步接纳消息且不得重入；引用不能在回调结束后继续持有。EOF 由调用方检查 `at_message_boundary()`，空输入不等于 EOF。
