#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <cstddef>
#include <cstdio>
#include <linux/mman.h>
#include <sys/mman.h>
#include <unistd.h>

int main() {
    constexpr std::size_t huge_page = 2ULL * 1024 * 1024;
    constexpr std::size_t bytes = 64ULL * 1024 * 1024;
    static_assert(bytes % huge_page == 0);

    void* addr = mmap(nullptr, bytes, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB | MAP_HUGE_2MB,
                      -1, 0);
    if (addr == MAP_FAILED) {
        std::perror("mmap(MAP_HUGETLB)");
        return 1;
    }

    // 每个大页写入一个字节，完成写入路径的 prefault。
    // volatile 只用于保留这些写操作，不提供线程同步。
    auto* memory = static_cast<volatile unsigned char*>(addr);
    for (std::size_t offset = 0; offset < bytes; offset += huge_page) {
        memory[offset] = 0;
    }

    std::printf("PID=%ld, addr=%p, bytes=%zu, prefault complete\n",
                static_cast<long>(getpid()), addr, bytes);
    std::puts("Inspect /proc/PID/smaps, then press Enter to release.");
    std::fflush(stdout);
    (void)std::getchar();

    if (munmap(addr, bytes) == -1) {
        std::perror("munmap");
        return 1;
    }
    return 0;
}
