// mmap_dontneed_demo.cpp
// 演示：MAP_SHARED 文件映射的脏页，如何用 syscall 主动刷盘 + 主动释放内存。
//
// 流程（对应 Linux 内核语义，见 mm/fadvise.c 的 generic_fadvise()）：
//   1. 创建文件 → ftruncate 预留大小 → mmap(MAP_SHARED)
//   2. 写满数据（产生脏页）
//   3. 只调 fadvise(DONTNEED)：页仍被本进程映射 → 被内核静默跳过（教学点）
//   4. 刷盘：Linux 用 sync_file_range（WAIT_AFTER 同步等回写），macOS 退化为 msync
//   5. madvise(MADV_DONTNEED)：解除本进程页表映射（VMA 保留），RSS 立即降
//   6. fadvise(POSIX_FADV_DONTNEED)：丢弃 page cache 中的页，物理内存归还 buddy
//   7. 重新访问第 0 页：从文件重新读回 → 证明数据已被持久化，没有被丢坏
//
// 每步用 mincore() 统计“驻留页数”，直观看到页什么时候被移出。
//
// 编译（Linux / macOS 均可）：
//   g++ -O2 -Wall -std=c++17 -o mmap_dontneed_demo mmap_dontneed_demo.cpp
// 运行：
//   ./mmap_dontneed_demo
//
// 注意：Linux 上第 3 步“只 fadvise”会被跳过；第 6 步才真正释放 page cache。
// macOS 的 posix_fadvise(POSIX_FADV_DONTNEED) 是 no-op，因此只演示到进程侧释放。

#define _GNU_SOURCE

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#include <unistd.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <cerrno>

#ifdef __linux__
#include <linux/fs.h> // sync_file_range 需要的常量实际上在 <fcntl.h> 下，保险起见不引入
#endif

static size_t g_page_size = 0;

// 统计 [addr, addr+len) 范围内驻留在物理内存里的页数
static size_t resident_pages(const void* addr, size_t len) {
    const size_t npages = (len + g_page_size - 1) / g_page_size;
    std::vector<char> vec(npages);
    if (mincore(addr, len, vec.data()) != 0) {
        perror("mincore");
        return static_cast<size_t>(-1);
    }
    size_t n = 0;
    for (char c : vec)
        n += (c & 1u) ? 1u : 0u;
    return n;
}

static void print_state(const char* tag, const void* addr, size_t len) {
    const size_t r = resident_pages(addr, len);
    printf("%-38s resident %5zu / %zu pages\n", tag, r, len / g_page_size);
    fflush(stdout);
}

int main() {
    g_page_size = static_cast<size_t>(sysconf(_SC_PAGESIZE));
    const size_t len   = 8u * 1024u * 1024u;      // 8 MiB
    const size_t npages = len / g_page_size;       // 2048 页（4K 页）

    const char* path = "/tmp/mmio_dontneed_demo.bin";

    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) { perror("open"); return 1; }
    unlink(path);                                   // 打开后立即删除：进程退出即自动清理

    if (ftruncate(fd, static_cast<off_t>(len)) != 0) { perror("ftruncate"); return 1; }

    char* base = static_cast<char*>(
        mmap(nullptr, len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0));
    if (base == MAP_FAILED) { perror("mmap"); return 1; }

#ifdef __linux__
    printf("platform: linux (sync_file_range + fadvise 全流程)\n");
#else
    printf("platform: non-linux (fadvise 为 no-op，仅演示进程侧释放)\n");
#endif
    printf("file size: %zu MiB, pages: %zu, page size: %zu\n\n",
           len >> 20, npages, g_page_size);

    // 1) 写满：产生脏页
    for (size_t i = 0; i < npages; i++)
        memset(base + i * g_page_size, 0xAB, g_page_size);
    print_state("1. after write (dirty)", base, len);

    // 2) 只调 fadvise：页仍被本进程映射，内核会静默跳过（源码见 fadvise.c）
#ifdef __linux__
    if (posix_fadvise(fd, 0, static_cast<off_t>(len), POSIX_FADV_DONTNEED) != 0)
        perror("fadvise #1");
#else
    printf("2. fadvise: no-op on this platform, skipped\n");
#endif
    print_state("2. fadvise alone (still mapped)", base, len);

    // 3) 刷盘：
    //    Linux: sync_file_range 同步等待写回完成（WAIT_BEFORE|WRITE|WAIT_AFTER）
    //    其它:  msync 同步刷盘
#ifdef __linux__
    if (sync_file_range(fd, 0, static_cast<off_t>(len),
            SYNC_FILE_RANGE_WAIT_BEFORE | SYNC_FILE_RANGE_WRITE |
            SYNC_FILE_RANGE_WAIT_AFTER) != 0)
        perror("sync_file_range");
#else
    if (msync(base, len, MS_SYNC) != 0)
        perror("msync");
#endif
    print_state("3. after sync (clean)", base, len);

    // 4) 解除本进程的页表映射（VMA 保留，页内容已刷盘所以丢弃安全）
    //    Linux: 立即生效，mincore 归零
    //    macOS: 对 MAP_SHARED 文件映射是 no-op（实测驻留页不变）——这本身是个教学点
    if (madvise(base, len, MADV_DONTNEED) != 0)
        perror("madvise");
#ifdef __linux__
    print_state("4. after madvise(DONTNEED)", base, len);
#else
    print_state("4. madvise(DONTNEED) no-op on macOS", base, len);
#endif

    // 5) 丢弃 page cache：此刻页已无人映射且已干净 → invalidate 成功
#ifdef __linux__
    if (posix_fadvise(fd, 0, static_cast<off_t>(len), POSIX_FADV_DONTNEED) != 0)
        perror("fadvise #2");
    print_state("5. after fadvise(DONTNEED)", base, len);
#else
    print_state("5. fadvise no-op (page cache kept by kernel)", base, len);
#endif

    // 6) 重新访问第 0 页：
    //    Linux: 页已被丢弃 → 从磁盘重新读回（证明数据确实已持久化）
    //    macOS: 页从未被丢 → 读的是内存里的值
    const char v = base[0];
#ifdef __linux__
    printf("\n6. re-touch page 0 -> byte = 0x%02X (从磁盘重新读回，期望 0xAB)\n",
           static_cast<unsigned char>(v));
#else
    printf("\n6. re-touch page 0 -> byte = 0x%02X (页未丢，读自内存)\n",
           static_cast<unsigned char>(v));
#endif
    print_state("   resident after re-touch", base, len);

    munmap(base, len);
    close(fd);
    printf("\ndone.\n");
    return 0;
}
