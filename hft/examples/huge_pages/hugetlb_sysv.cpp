#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <cstddef>
#include <cstdio>
#include <sys/shm.h>
#include <unistd.h>

// 部分 libc 头文件未提供此宏；Linux 用 flags 的第 26 位起编码 log2(页大小)。
#ifndef SHM_HUGE_2MB
#define SHM_HUGE_2MB (21 << 26)
#endif

int main() {
    constexpr std::size_t huge_page = 2ULL * 1024 * 1024;
    constexpr std::size_t bytes = 64ULL * 1024 * 1024;
    static_assert(bytes % huge_page == 0);

    const int shmid = shmget(IPC_PRIVATE, bytes,
                            IPC_CREAT | SHM_HUGETLB | SHM_HUGE_2MB | 0600);
    if (shmid == -1) {
        std::perror("shmget(SHM_HUGETLB)");
        return 1;
    }

    void* addr = shmat(shmid, nullptr, 0);
    if (addr == reinterpret_cast<void*>(-1)) {
        std::perror("shmat");
        if (shmctl(shmid, IPC_RMID, nullptr) == -1) {
            std::perror("shmctl(IPC_RMID)");
        }
        return 1;
    }

    // 当前进程已经挂接；标记删除后，最后一次 detach 才真正释放。
    if (shmctl(shmid, IPC_RMID, nullptr) == -1) {
        std::perror("shmctl(IPC_RMID)");
        std::fprintf(stderr, "Check remaining segment: ipcs -m -i %d\n", shmid);
        (void)shmdt(addr);
        return 1;
    }

    auto* memory = static_cast<volatile unsigned char*>(addr);
    for (std::size_t offset = 0; offset < bytes; offset += huge_page) {
        memory[offset] = 0;
    }

    std::printf("PID=%ld, shmid=%d, addr=%p, bytes=%zu, prefault complete\n",
                static_cast<long>(getpid()), shmid, addr, bytes);
    std::puts("Inspect /proc/PID/smaps, then press Enter to detach.");
    std::fflush(stdout);
    (void)std::getchar();

    if (shmdt(addr) == -1) {
        std::perror("shmdt");
        return 1;
    }
    return 0;
}
