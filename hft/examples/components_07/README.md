# SBE 布局、版本与视图实验

实验为[开源项目解析篇](../../HFT_Engineering_07_开源项目解析.md)提供实际字节结果。`run.py` 使用 SBE 1.40.2 生成两个版本的 C++ 访问器，在临时目录编译运行，并用独立的 Python 编码核对消息字节。程序不修改系统配置，不保留生成头文件和二进制产物。

## 运行

使用 Python 3、JDK 17 或更新版本，以及支持 C++20、AddressSanitizer、UndefinedBehaviorSanitizer 的 C++ 编译器。第一次运行会从 Maven Central 下载固定版本生成器，并校验 SHA-256。

```bash
JAVA_BIN=/path/to/jdk/bin/java python3 hft/examples/components_07/run.py
```

`JAVA_BIN` 未设置时使用 PATH 中的 `java`；`CXX` 未设置时使用 PATH 中的 `c++`。本次运行使用：

```bash
JAVA_BIN=/opt/homebrew/opt/openjdk/bin/java python3 hft/examples/components_07/run.py
```

macOS ARM64、Apple Clang 17.0.0、OpenJDK 24.0.2 的结果：

```text
POD size=40 sourceSeq offset=8
v0=86 v1=90 cross-decode=4/4 truncated-prefixes=90/90 order-check=pass view-alias=pass
v1 hex=28000100070001002a000000e90300000000000044d61200000000000300000000000000e703000000000000050000001000020044d61200000000000300000000000000e0d512000000000007000000000000000400584e4153
independent Python wire oracle=pass
```

## 文件和验证对象

| 文件 | 用途 |
| --- | --- |
| [event-v0.xml](event-v0.xml) | 36 字节固定区，后接两字段档位组与字符串 |
| [event-v1.xml](event-v1.xml) | 固定区末尾增加 `flags`，固定区变为 40 字节 |
| [codec_check.cpp](codec_check.cpp) | 四组版本交叉读取、截断输入、顺序检查和缓冲区视图实验 |
| [run.py](run.py) | 生成、消毒器编译、运行和独立字节核对 |

`codec_check.cpp` 的 `validate` 是针对本例 v0/v1 的入口校验器，检查头、固定区、组项宽度与数量、字符串长度和整帧边界；它拒绝其他版本。90 个截断前缀在进入生成访问器之前被拒绝。它没有检查实际合约列表、业务数量范围或通用 UTF-8，因此这些断言用于验证这份 schema 的字节布局。

视图实验保留 `getSourceAsStringView()` 的结果，然后修改底层字节，确认视图同步读到新值。这个实验让缓冲区始终存活，用确定的别名变化演示生命周期问题，不通过解引用已释放内存制造崩溃。

生成器固定为 `uk.co.real-logic:sbe-all:1.40.2`，SHA-256 为 `f2ffd992f69e3e88b674f36a56424704bda89a98e18bd4c73054b0ec6da196a4`，对应[官方源码 commit](https://github.com/aeron-io/simple-binary-encoding/tree/05b076c0c64d7164b56db2f9b5e6ca476ac46072)。
