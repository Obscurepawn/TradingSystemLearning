#include "disruptor.hpp"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <functional>
#include <iostream>
#include <latch>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

class TestFailure : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

void require(bool condition, const char* message) {
    if (!condition) throw TestFailure(message);
}

template <class Exception, class Function>
void require_throws(Function&& function, const char* message) {
    try {
        std::forward<Function>(function)();
    } catch (const Exception&) {
        return;
    }
    throw TestFailure(message);
}

// The watchdog also catches deadlocks inside destructors or thread joins. It does
// not decide whether any behavior is correct: individual checks do that.
class Watchdog {
public:
    Watchdog() : worker_([this] {
        std::unique_lock lock(mutex_);
        if (!condition_.wait_for(lock, std::chrono::seconds(60),
                                 [this] { return done_; })) {
            std::cerr << "TEST WATCHDOG: process failed to finish within 60 s\n";
            std::_Exit(124);
        }
    }) {}

    ~Watchdog() {
        {
            std::lock_guard lock(mutex_);
            done_ = true;
        }
        condition_.notify_one();
        worker_.join();
    }

private:
    std::mutex mutex_;
    std::condition_variable condition_;
    bool done_ = false;
    std::thread worker_;
};

class Gate {
public:
    void wait() {
        std::unique_lock lock(mutex_);
        condition_.wait(lock, [this] { return open_; });
    }
    void open() {
        {
            std::lock_guard lock(mutex_);
            open_ = true;
        }
        condition_.notify_all();
    }

private:
    std::mutex mutex_;
    std::condition_variable condition_;
    bool open_ = false;
};

class ThreadErrors {
public:
    template <class Function>
    void run(Function&& function) noexcept {
        try {
            std::forward<Function>(function)();
        } catch (...) {
            std::lock_guard lock(mutex_);
            if (!error_) error_ = std::current_exception();
        }
    }
    void rethrow() {
        std::lock_guard lock(mutex_);
        if (error_) std::rethrow_exception(error_);
    }

private:
    std::mutex mutex_;
    std::exception_ptr error_;
};

template <class Predicate>
void wait_until(Predicate&& predicate, const char* message) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    while (!predicate()) {
        if (std::chrono::steady_clock::now() >= deadline) throw TestFailure(message);
        std::this_thread::yield();
    }
}

struct Event {
    std::int64_t value = -1;
    std::int64_t a = -1;
    std::int64_t b = -1;
    int producer = -1;
    int ordinal = -1;
};

using disruptor::ProducerMode;
using disruptor::WaitStrategy;
using Ring = disruptor::Disruptor<Event>;
using SingleRing = disruptor::Disruptor<Event, ProducerMode::Single>;

constexpr WaitStrategy strategies[] = {
    WaitStrategy::BusySpin, WaitStrategy::Yielding, WaitStrategy::Blocking
};

const char* strategy_name(WaitStrategy strategy) {
    switch (strategy) {
        case WaitStrategy::BusySpin: return "BusySpin";
        case WaitStrategy::Yielding: return "Yielding";
        case WaitStrategy::Blocking: return "Blocking";
    }
    return "unknown";
}

void test_single_producer_wrap(WaitStrategy strategy) {
    constexpr std::int64_t total = 4097;
    SingleRing ring(8, strategy, 3);
    std::int64_t expected = 0;
    std::size_t batch_ends = 0;
    const auto consumer = ring.add_consumer("ordered", {},
        [&](Event& event, std::int64_t sequence, bool end_of_batch) {
            require(sequence == expected, "single producer: nonsequential delivery");
            require(event.value == sequence * 17 + 3, "single producer: overwritten event");
            ++expected;
            if (end_of_batch) ++batch_ends;
        });
    require(ring.capacity() == 8, "capacity does not match construction");
    require(ring.claimed() == -1, "initial claimed sequence must be -1");
    require(ring.progress(consumer) == -1, "initial consumer progress must be -1");
    ring.start();
    for (std::int64_t i = 0; i < total; ++i) {
        const auto sequence = ring.publish([](Event& event, std::int64_t s) noexcept {
            event.value = s * 17 + 3;
        });
        require(sequence == i, "single producer: publication sequence mismatch");
    }
    ring.shutdown();
    require(expected == total, "single producer: shutdown did not drain");
    require(batch_ends != 0, "single producer: missing end_of_batch notification");
    require(ring.progress(consumer) == total - 1, "final consumer progress mismatch");
    require(ring.claimed() == total - 1, "final claimed sequence mismatch");
    ring.shutdown();  // Idempotence after a successful close.
    require_throws<disruptor::Closed>([&] {
        ring.publish([](Event&, std::int64_t) noexcept {});
    }, "publish after shutdown must report Closed");
    require_throws<disruptor::Closed>([&] {
        (void)ring.try_publish([](Event&, std::int64_t) noexcept {});
    }, "try_publish after shutdown must report Closed, not a full ring");
}

void test_multi_producer_broadcast(WaitStrategy strategy) {
    constexpr int producer_count = 4;
    constexpr int per_producer = 1000;
    constexpr int total = producer_count * per_producer;
    Ring ring(16, strategy, 7);
    std::vector<int> seen_a(total, 0), seen_b(total, 0);
    std::int64_t next_a = 0, next_b = 0;
    const auto consume = [&](std::vector<int>& seen, std::int64_t& next,
                             Event& event, std::int64_t sequence) {
        require(sequence == next, "multi producer: delivery out of sequence");
        require(event.value == sequence * 31 + 11, "multi producer: incomplete or overwritten slot");
        require(event.producer >= 0 && event.producer < producer_count,
                "multi producer: invalid producer identity");
        require(event.ordinal >= 0 && event.ordinal < per_producer,
                "multi producer: invalid producer ordinal");
        ++seen[event.producer * per_producer + event.ordinal];
        ++next;
    };
    const auto a = ring.add_consumer("independent-a", {},
        [&](Event& e, std::int64_t s, bool) { consume(seen_a, next_a, e, s); });
    const auto b = ring.add_consumer("independent-b", {},
        [&](Event& e, std::int64_t s, bool) { consume(seen_b, next_b, e, s); });
    ring.start();
    std::latch ready(producer_count);
    Gate begin;
    ThreadErrors errors;
    std::vector<std::thread> producers;
    for (int producer = 0; producer < producer_count; ++producer) {
        producers.emplace_back([&, producer] {
            ready.count_down();
            begin.wait();
            errors.run([&] {
                for (int ordinal = 0; ordinal < per_producer; ++ordinal) {
                    ring.publish([=](Event& event, std::int64_t sequence) noexcept {
                        event.value = sequence * 31 + 11;
                        event.producer = producer;
                        event.ordinal = ordinal;
                    });
                }
            });
        });
    }
    ready.wait();
    begin.open();
    for (auto& producer : producers) producer.join();
    ring.shutdown();
    errors.rethrow();
    require(next_a == total && next_b == total, "broadcast consumer missed an event");
    require(ring.progress(a) == total - 1 && ring.progress(b) == total - 1,
            "broadcast consumer final progress mismatch");
    for (int i = 0; i < total; ++i) {
        require(seen_a[i] == 1 && seen_b[i] == 1,
                "broadcast must deliver each producer event exactly once to each consumer");
    }
}

void test_diamond_dependencies(WaitStrategy strategy) {
    SingleRing ring(8, strategy, 5);
    constexpr std::int64_t total = 2049;
    const auto a = ring.add_consumer("A", {}, [](Event& e, std::int64_t, bool) {
        e.a = e.value * 2;
    });
    const auto b = ring.add_consumer("B", {}, [](Event& e, std::int64_t, bool) {
        e.b = e.value * 3;
    });
    std::int64_t checked = 0;
    const auto c = ring.add_consumer("C", {a, b},
        [&](Event& e, std::int64_t s, bool) {
            require(s == checked, "diamond join delivered out of order");
            require(e.value == s + 1, "diamond join read overwritten event");
            require(e.a == e.value * 2 && e.b == e.value * 3,
                    "diamond join ran before both predecessors completed");
            ++checked;
        });
    ring.start();
    for (std::int64_t i = 0; i < total; ++i) {
        ring.publish([](Event& e, std::int64_t s) noexcept {
            e.value = s + 1;
            e.a = -1;
            e.b = -1;
        });
    }
    ring.shutdown();
    require(checked == total && ring.progress(c) == total - 1,
            "diamond join did not drain every event");
}

void test_try_publish_backpressure(WaitStrategy strategy) {
    Ring ring(2, strategy);
    std::latch entered(1);
    Gate release;
    std::int64_t received = 0;
    const auto consumer = ring.add_consumer("slow", {},
        [&](Event& e, std::int64_t s, bool) {
            if (s == 0) {
                entered.count_down();
                release.wait();
            }
            require(e.value == s, "backpressure failed to prevent slot overwrite");
            ++received;
        });
    ring.start();
    ring.publish([](Event& e, std::int64_t s) noexcept { e.value = s; });
    entered.wait();
    ring.publish([](Event& e, std::int64_t s) noexcept { e.value = s; });
    bool called = false;
    const auto full = ring.try_publish([&](Event&, std::int64_t) noexcept { called = true; });
    const auto claimed_while_full = ring.claimed();
    const auto progress_while_blocked = ring.progress(consumer);
    release.open();  // Release before any check can throw and enter destruction.
    require(!full, "try_publish must return nullopt while full");
    require(!called, "a failed try_publish must not invoke its writer");
    require(claimed_while_full == 1, "a failed try_publish consumed a sequence");
    require(progress_while_blocked == -1, "progress advanced before handler completion");
    wait_until([&] { return ring.progress(consumer) == 1; }, "consumer failed to resume");
    const auto reused = ring.try_publish([](Event& e, std::int64_t s) noexcept { e.value = s; });
    require(reused && *reused == 2, "try_publish did not reuse freed capacity");
    ring.shutdown();
    require(received == 3, "backpressure test did not receive all admitted events");
}

void test_publication_gap(WaitStrategy strategy) {
    Ring ring(4, strategy);
    std::latch first_writer_entered(1);
    Gate release_first_writer;
    std::atomic<int> received{0};
    std::int64_t next = 0;
    ring.add_consumer("ordered", {}, [&](Event& e, std::int64_t s, bool) {
        require(s == next, "consumer crossed an unpublished sequence gap");
        require(e.value == s + 100, "consumer read a not-yet-published event");
        ++next;
        received.fetch_add(1, std::memory_order_relaxed);
    });
    ring.start();
    ThreadErrors errors;
    std::thread first([&] {
        errors.run([&] {
            const auto sequence = ring.publish([&](Event& e, std::int64_t s) noexcept {
                first_writer_entered.count_down();
                release_first_writer.wait();
                e.value = s + 100;
            });
            require(sequence == 0, "first reserved sequence must be zero");
        });
    });
    first_writer_entered.wait();
    const auto second = ring.publish([](Event& e, std::int64_t s) noexcept { e.value = s + 100; });
    const auto before_release = received.load(std::memory_order_relaxed);
    release_first_writer.open();
    first.join();
    ring.shutdown();
    errors.rethrow();
    require(second == 1, "second producer did not reserve sequence one");
    require(before_release == 0, "consumer ran while sequence zero was unpublished");
    require(next == 2, "publication gap did not close and drain");
}

void test_shutdown_waits_for_admitted_writer(WaitStrategy strategy) {
    Ring ring(8, strategy);
    std::latch writer_entered(1), shutdown_entered(1);
    Gate release_writer;
    std::atomic<bool> shutdown_done{false};
    std::int64_t received = 0;
    ring.add_consumer("drain", {}, [&](Event& e, std::int64_t s, bool) {
        require(e.value == s, "shutdown drained an incomplete event");
        require(s == received, "shutdown drain sequence mismatch");
        ++received;
    });
    ring.start();
    ThreadErrors errors;
    std::thread publisher([&] {
        errors.run([&] {
            ring.publish([&](Event& e, std::int64_t s) noexcept {
                writer_entered.count_down();
                release_writer.wait();
                e.value = s;
            });
        });
    });
    writer_entered.wait();
    std::thread closer([&] {
        errors.run([&] {
            shutdown_entered.count_down();
            ring.shutdown();
            shutdown_done.store(true, std::memory_order_release);
        });
    });
    shutdown_entered.wait();
    bool observed_closed = false;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    while (!observed_closed && std::chrono::steady_clock::now() < deadline) {
        try {
            (void)ring.try_publish([](Event& e, std::int64_t s) noexcept { e.value = s; });
        } catch (const disruptor::Closed&) {
            observed_closed = true;
        }
        std::this_thread::yield();
    }
    const bool finished_too_soon = shutdown_done.load(std::memory_order_acquire);
    const auto final_claimed = ring.claimed();
    release_writer.open();
    publisher.join();
    closer.join();
    errors.rethrow();
    require(observed_closed, "shutdown failed to close publication admissions");
    require(!finished_too_soon, "shutdown returned before an admitted writer finished");
    require(shutdown_done.load(std::memory_order_acquire), "shutdown failed to finish");
    require(received == final_claimed + 1, "shutdown failed to drain all admitted writers");
}

void test_idle_waiters(WaitStrategy strategy) {
    {
        Ring ring(4, strategy);
        std::atomic<int> calls{0};
        const auto a = ring.add_consumer("idle-a", {},
            [&](Event&, std::int64_t, bool) { ++calls; });
        ring.add_consumer("idle-b", {a}, [&](Event&, std::int64_t, bool) { ++calls; });
        ring.start();
        ring.shutdown();
        require(calls.load() == 0, "idle shutdown manufactured an event");
    }
    {
        Ring ring(4, strategy);
        ring.add_consumer("idle-halt", {}, [](Event&, std::int64_t, bool) {});
        ring.start();
        ring.halt();  // Destructor must wake and join idle waiters after abort.
    }
}

void test_handler_failure_and_waiter_wakeup(WaitStrategy strategy) {
    Ring ring(2, strategy);
    std::latch handler_entered(1), publisher_started(1);
    Gate fail_now;
    std::atomic<int> downstream_calls{0};
    std::atomic<bool> publisher_returned{false};
    const auto a = ring.add_consumer("failing", {}, [&](Event&, std::int64_t, bool) {
        handler_entered.count_down();
        fail_now.wait();
        throw std::runtime_error("intentional handler failure");
    });
    ring.add_consumer("waiting-dependent", {a},
        [&](Event&, std::int64_t, bool) { ++downstream_calls; });
    ring.start();
    ring.publish([](Event&, std::int64_t) noexcept {});
    handler_entered.wait();
    ring.publish([](Event&, std::int64_t) noexcept {});
    ThreadErrors errors;
    std::thread blocked_publisher([&] {
        errors.run([&] {
            publisher_started.count_down();
            bool interrupted = false;
            try {
                ring.publish([](Event&, std::int64_t) noexcept {});
            } catch (const disruptor::Closed&) {
                interrupted = true;
            } catch (const std::runtime_error& e) {
                interrupted = std::string(e.what()) == "intentional handler failure";
            }
            require(interrupted, "full-ring publisher was not interrupted on failure");
            publisher_returned.store(true, std::memory_order_release);
        });
    });
    publisher_started.wait();
    fail_now.open();
    std::exception_ptr shutdown_error;
    try {
        ring.shutdown();
    } catch (...) {
        shutdown_error = std::current_exception();
    }
    blocked_publisher.join();
    errors.rethrow();
    require(publisher_returned.load(std::memory_order_acquire), "publisher failed to wake");
    require(shutdown_error != nullptr, "shutdown did not rethrow handler failure");
    try {
        std::rethrow_exception(shutdown_error);
    } catch (const std::runtime_error& e) {
        require(std::string(e.what()) == "intentional handler failure",
                "shutdown changed the original exception");
    }
    require(downstream_calls.load() == 0, "dependent ran after its predecessor failed");
    for (int attempt = 0; attempt < 2; ++attempt) {
        bool original = false;
        try {
            if (attempt == 0) ring.rethrow_if_failed();
            else ring.shutdown();
        } catch (const std::runtime_error& e) {
            original = std::string(e.what()) == "intentional handler failure";
        }
        require(original, "stored failure was not preserved across repeated observation");
    }
}

void test_invalid_configuration_and_lifecycle() {
    require_throws<std::exception>([] { Ring invalid(0); }, "zero capacity was accepted");
    require_throws<std::exception>([] { Ring invalid(3); }, "non-power-of-two capacity was accepted");
    {
        Ring ring(4);
        require_throws<std::logic_error>([&] { ring.start(); }, "start without consumers was accepted");
    }
    {
        Ring ring(4);
        require_throws<std::logic_error>([&] {
            ring.publish([](Event&, std::int64_t) noexcept {});
        }, "publish before start was accepted");
        require_throws<std::logic_error>([&] {
            (void)ring.try_publish([](Event&, std::int64_t) noexcept {});
        }, "try_publish before start was accepted");
        require_throws<std::logic_error>([&] { ring.shutdown(); }, "shutdown before start was accepted");
        ring.add_consumer("first", {}, [](Event&, std::int64_t, bool) {});
        ring.start();
        require_throws<std::logic_error>([&] { ring.start(); }, "duplicate start was accepted");
        require_throws<std::logic_error>([&] {
            ring.add_consumer("late", {}, [](Event&, std::int64_t, bool) {});
        }, "topology changed after start");
        ring.shutdown();
    }
}

void test_callback_reentrancy_rejected() {
    Ring ring(4, WaitStrategy::Blocking);
    bool publish_rejected = false, shutdown_rejected = false;
    ring.add_consumer("reentrant", {}, [&](Event&, std::int64_t, bool) {
        try {
            ring.publish([](Event&, std::int64_t) noexcept {});
        } catch (const std::logic_error&) {
            publish_rejected = true;
        }
        try {
            ring.shutdown();
        } catch (const std::logic_error&) {
            shutdown_rejected = true;
        }
    });
    ring.start();
    ring.publish([](Event&, std::int64_t) noexcept {});
    ring.shutdown();
    require(publish_rejected && shutdown_rejected,
            "callback reentrancy was not rejected before it could deadlock");
}

void test_writer_reentrancy_rejected() {
    Ring ring(4, WaitStrategy::Blocking);
    bool publish_rejected = false, try_publish_rejected = false;
    bool shutdown_rejected = false, unexpected_exception = false;
    bool nested_writer_called = false;
    std::int64_t received = 0;
    ring.add_consumer("writer-reentrancy", {}, [&](Event& event, std::int64_t s, bool) {
        require(s == received && event.value == s + 100,
                "outer writer event was corrupted by rejected reentrancy");
        ++received;
    });
    ring.start();
    const auto sequence = ring.publish([&](Event& event, std::int64_t s) noexcept {
        try {
            ring.publish([&](Event&, std::int64_t) noexcept { nested_writer_called = true; });
        } catch (const std::logic_error&) {
            publish_rejected = true;
        } catch (...) {
            unexpected_exception = true;
        }
        try {
            (void)ring.try_publish([&](Event&, std::int64_t) noexcept {
                nested_writer_called = true;
            });
        } catch (const std::logic_error&) {
            try_publish_rejected = true;
        } catch (...) {
            unexpected_exception = true;
        }
        try {
            ring.shutdown();
        } catch (const std::logic_error&) {
            shutdown_rejected = true;
        } catch (...) {
            unexpected_exception = true;
        }
        event.value = s + 100;
    });
    // A completed writer must restore its thread-local publication context.
    const auto following = ring.publish([](Event& event, std::int64_t s) noexcept {
        event.value = s + 100;
    });
    ring.shutdown();
    require(publish_rejected && try_publish_rejected && shutdown_rejected,
            "writer reentrancy must reject publish, try_publish, and shutdown");
    require(!unexpected_exception, "writer reentrancy threw an unexpected exception type");
    require(!nested_writer_called, "rejected nested publication invoked its writer");
    require(sequence == 0 && following == 1 && received == 2,
            "rejected writer reentrancy leaked admission state or claimed a sequence");
}

void test_nested_queue_writer_context_restored() {
    Ring a(8, WaitStrategy::Blocking), b(8, WaitStrategy::Blocking);
    std::int64_t received_a = 0, received_b = 0;
    a.add_consumer("A", {}, [&](Event& e, std::int64_t s, bool) {
        require(s == received_a && e.value == 100 + s, "nested queue A delivery mismatch");
        ++received_a;
    });
    b.add_consumer("B", {}, [&](Event& e, std::int64_t s, bool) {
        require(s == received_b && e.value == 200 + s, "nested queue B delivery mismatch");
        ++received_b;
    });
    a.start();
    b.start();
    bool ancestor_publish_rejected = false, ancestor_try_rejected = false;
    bool ancestor_shutdown_rejected = false, restored_outer_rejected = false;
    bool unexpected_exception = false, nested_writer_called = false;
    std::int64_t nested_b_sequence = -1, second_b_sequence = -1;
    const auto outer_a_sequence = a.publish([&](Event& event_a, std::int64_t s_a) noexcept {
        try {
            nested_b_sequence = b.publish([&](Event& event_b, std::int64_t s_b) noexcept {
                try {
                    a.publish([&](Event&, std::int64_t) noexcept { nested_writer_called = true; });
                } catch (const std::logic_error&) {
                    ancestor_publish_rejected = true;
                } catch (...) {
                    unexpected_exception = true;
                }
                try {
                    (void)a.try_publish([&](Event&, std::int64_t) noexcept {
                        nested_writer_called = true;
                    });
                } catch (const std::logic_error&) {
                    ancestor_try_rejected = true;
                } catch (...) {
                    unexpected_exception = true;
                }
                try {
                    a.shutdown();
                } catch (const std::logic_error&) {
                    ancestor_shutdown_rejected = true;
                } catch (...) {
                    unexpected_exception = true;
                }
                event_b.value = 200 + s_b;
            });
        } catch (...) {
            unexpected_exception = true;
        }
        // Popping B's context must restore A, rather than clear the entire chain.
        try {
            a.publish([&](Event&, std::int64_t) noexcept { nested_writer_called = true; });
        } catch (const std::logic_error&) {
            restored_outer_rejected = true;
        } catch (...) {
            unexpected_exception = true;
        }
        // B is no longer active, so publishing to it again must be allowed.
        try {
            const auto result = b.try_publish([](Event& event_b, std::int64_t s_b) noexcept {
                event_b.value = 200 + s_b;
            });
            if (result) second_b_sequence = *result;
        } catch (...) {
            unexpected_exception = true;
        }
        event_a.value = 100 + s_a;
    });
    // Neither queue may remain in the TLS chain after the outer writer returns.
    const auto next_a_sequence = a.publish([](Event& e, std::int64_t s) noexcept {
        e.value = 100 + s;
    });
    const auto next_b_sequence = b.publish([](Event& e, std::int64_t s) noexcept {
        e.value = 200 + s;
    });
    a.shutdown();
    b.shutdown();
    require(ancestor_publish_rejected && ancestor_try_rejected && ancestor_shutdown_rejected,
            "nested cross-queue writer failed to reject reentry into an ancestor queue");
    require(restored_outer_rejected, "nested writer did not restore its outer TLS context");
    require(!unexpected_exception && !nested_writer_called,
            "nested writer admitted forbidden reentry or rejected a different queue");
    require(outer_a_sequence == 0 && next_a_sequence == 1 && received_a == 2,
            "queue A publication state was not restored after nested writers");
    require(nested_b_sequence == 0 && second_b_sequence == 1 && next_b_sequence == 2 && received_b == 3,
            "queue B publication state was not restored after nested writers");
}

}  // namespace

int main() {
    Watchdog watchdog;
    int passed = 0;
    const auto run = [&](const std::string& name, const auto& test) {
        std::cerr << "[ RUN      ] " << name << '\n';
        test();
        ++passed;
        std::cerr << "[       OK ] " << name << '\n';
    };
    try {
        run("invalid_configuration_and_lifecycle", test_invalid_configuration_and_lifecycle);
        run("callback_reentrancy_rejected", test_callback_reentrancy_rejected);
        run("writer_reentrancy_rejected", test_writer_reentrancy_rejected);
        run("nested_queue_writer_context_restored", test_nested_queue_writer_context_restored);
        for (const auto strategy : strategies) {
            const auto name = std::string(strategy_name(strategy)) + "/";
            run(name + "single_producer_wrap", [&] { test_single_producer_wrap(strategy); });
            run(name + "multi_producer_broadcast", [&] { test_multi_producer_broadcast(strategy); });
            run(name + "diamond_dependencies", [&] { test_diamond_dependencies(strategy); });
            run(name + "try_publish_backpressure", [&] { test_try_publish_backpressure(strategy); });
            run(name + "publication_gap", [&] { test_publication_gap(strategy); });
            run(name + "shutdown_waits_for_admitted_writer", [&] { test_shutdown_waits_for_admitted_writer(strategy); });
            run(name + "idle_waiters", [&] { test_idle_waiters(strategy); });
            run(name + "handler_failure_and_waiter_wakeup", [&] { test_handler_failure_and_waiter_wakeup(strategy); });
        }
    } catch (const std::exception& e) {
        std::cerr << "[  FAILED  ] " << e.what() << '\n';
        return 1;
    } catch (...) {
        std::cerr << "[  FAILED  ] non-standard exception\n";
        return 1;
    }
    std::cout << "All " << passed << " tests passed.\n";
    return 0;
}
