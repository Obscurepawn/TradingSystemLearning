#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string_view>
#include <system_error>

#ifdef __linux__
#include <atomic>
#include <cerrno>
#include <time.h>
#if defined(__x86_64__) || defined(__i386__)
#include <immintrin.h>
#endif
#endif

namespace {

enum class Mode { spin, sleep, hybrid };

Mode parse_mode(std::string_view text) {
    if (text == "spin") {
        return Mode::spin;
    }
    if (text == "sleep") {
        return Mode::sleep;
    }
    if (text == "hybrid") {
        return Mode::hybrid;
    }
    throw std::invalid_argument("mode must be spin, sleep or hybrid");
}

#ifdef __linux__

constexpr std::int64_t kTargetNs = 20'000;
// Demonstration value, not a measured or generally optimal spin window.
constexpr std::int64_t kSpinWindowNs = 10'000;

std::int64_t now_ns() {
    timespec ts{};
    if (::clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        throw std::system_error(errno, std::generic_category(), "clock_gettime");
    }
    return static_cast<std::int64_t>(ts.tv_sec) * 1'000'000'000 + ts.tv_nsec;
}

void cpu_relax() {
#if defined(__x86_64__) || defined(__i386__)
    _mm_pause();
#elif defined(__aarch64__)
    // CPU hint: this does not yield the thread to the OS scheduler.
    asm volatile("yield");
#else
    // Compiler barrier only; other architectures may provide a better CPU hint.
    std::atomic_signal_fence(std::memory_order_seq_cst);
#endif
}

void sleep_until(std::int64_t target_ns) {
    const timespec target{static_cast<time_t>(target_ns / 1'000'000'000),
                          static_cast<long>(target_ns % 1'000'000'000)};
    for (;;) {
        // clock_nanosleep returns the error code directly, rather than in errno.
        const int rc = ::clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &target, nullptr);
        if (rc == 0) {
            return;
        }
        if (rc != EINTR) {
            throw std::system_error(rc, std::generic_category(), "clock_nanosleep");
        }
        // Retry the same absolute target after a signal; do not move the deadline.
    }
}

void spin_until(std::int64_t deadline_ns) {
    while (now_ns() < deadline_ns) {
        cpu_relax();
    }
}

void wait_until(Mode mode, std::int64_t deadline_ns) {
    if (mode == Mode::sleep) {
        sleep_until(deadline_ns);
        return;
    }
    if (mode == Mode::hybrid) {
        const auto wake_target_ns = deadline_ns - kSpinWindowNs;
        if (now_ns() < wake_target_ns) {
            sleep_until(wake_target_ns);
        }
        // A late wakeup may already be past the final deadline. It cannot be undone.
    }
    spin_until(deadline_ns);
}

#endif // __linux__

} // namespace

int main(int argc, char** argv) {
    try {
        if (argc > 2) {
            throw std::invalid_argument("usage: precise_sleep [spin|sleep|hybrid]");
        }
        const std::string_view mode_name = argc == 2 ? argv[1] : "spin";
        const auto mode = parse_mode(mode_name);
#ifdef __linux__
        const auto start_ns = now_ns();
        const auto deadline_ns = start_ns + kTargetNs;
        wait_until(mode, deadline_ns);
        const auto end_ns = now_ns(); // Endpoint: immediately after the wait returns.
        std::cout << "mode=" << mode_name
                  << ", target_ns=" << kTargetNs
                  << ", actual_wait_ns=" << end_ns - start_ns
                  << ", overshoot_ns=" << end_ns - deadline_ns << '\n';
        if (end_ns < deadline_ns) {
            std::cerr << "precise_sleep: observed an unexpected early return\n";
            return 1;
        }
        return 0;
#else
        (void)mode;
        std::cerr << "precise_sleep: this timing demo requires Linux\n";
        return 2;
#endif
    } catch (const std::exception& error) {
        std::cerr << "precise_sleep: " << error.what() << '\n';
        return 1;
    }
}
