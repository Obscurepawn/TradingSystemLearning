# 验证记录

验证日期：2026-10-12。编译器：GCC 13.3.0，C++20，Linux。

| 构建/检查 | 结果 |
| --- | --- |
| Release，`-O2 -DNDEBUG -Wall -Wextra -Wpedantic -Werror` | 28/28 项测试通过，无编译警告 |
| ThreadSanitizer，`-O1 -g -fsanitize=thread` | 28/28 项测试通过，无数据竞争报告 |
| AddressSanitizer + UndefinedBehaviorSanitizer | 28/28 项测试通过，无内存访问或未定义行为报告 |
| 四生产者、菱形依赖示例 | 发布 40000 条；A、B、C 分别消费并验证全部 40000 条 |

ASan/UBSan 运行设置了 `ASAN_OPTIONS=detect_leaks=0`。当前环境的 LeakSanitizer 无法读取所需的进程信息，因此未完成内存泄漏检查。CMake 配置已提供，但当前环境没有 CMake，以上验证均通过直接调用 g++ 完成。

测试集的全局用例覆盖非法配置、生命周期、消费者回调重入拒绝、writer 回调重入拒绝，以及跨队列嵌套调用后的线程局部状态恢复。BusySpin、Yielding、Blocking 三种等待策略分别运行以下八组用例：

1. 单生产者 4097 条事件，容量 8，反复绕环并验证全部事件按序到达。
2. 四生产者共 4000 条事件，容量 16，两个独立消费者分别检查全部事件无遗漏、无重复。
3. A/B 写不同字段，C 等待两者完成，验证依赖可见性与槽位复用。
4. 阻塞慢消费者、填满容量 2 的缓冲区，检查 try_publish 返回空值且不占用序号；释放后恢复发布。
5. 暂停序号 0 的 writer，先发布序号 1，检查消费者不会越过未发布的空洞。
6. writer 尚未完成时并发关闭入口，检查新发布被拒绝、已接纳事件仍被排空。
7. 无事件时正常关闭或中止，检查等待中的消费者能够退出。
8. 消费者抛异常，检查等待容量的生产者及依赖消费者被唤醒，原始异常被保留并重抛。

用例通过不等于形式化证明；这些测试没有提供性能测量，也没有覆盖所有平台或所有应用回调。

## 复现命令

在项目根目录执行：

```bash
g++ -std=c++20 -O2 -DNDEBUG -pthread -Wall -Wextra -Wpedantic -Werror \
    -Iinclude tests/test_disruptor.cpp -o test_release
./test_release

g++ -std=c++20 -O1 -g -pthread -fsanitize=thread -fno-omit-frame-pointer \
    -Iinclude tests/test_disruptor.cpp -o test_tsan
./test_tsan

g++ -std=c++20 -O1 -g -pthread -fsanitize=address,undefined -fno-omit-frame-pointer \
    -Iinclude tests/test_disruptor.cpp -o test_asan
ASAN_OPTIONS=detect_leaks=0 ./test_asan
```
