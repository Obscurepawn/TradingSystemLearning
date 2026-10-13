#pragma once

#include <algorithm>
#include <atomic>
#include <bit>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <functional>
#include <initializer_list>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <type_traits>
#include <utility>
#include <vector>

#if defined(__i386__) || defined(__x86_64__)
#include <immintrin.h>
#endif

// Independent C++20 implementation of the Disruptor's core semantics.
// See README.md for the ownership, callback and lifecycle contracts.
namespace disruptor {

enum class ProducerMode { Single, Multi };
enum class WaitStrategy { BusySpin, Yielding, Blocking };

class Closed : public std::runtime_error {
public:
    Closed() : std::runtime_error("Disruptor is closed or halted") {}
};

namespace detail {
inline void cpu_relax() noexcept {
#if defined(__i386__) || defined(__x86_64__)
    _mm_pause();
#elif defined(__aarch64__) || defined(__arm__)
    __asm__ __volatile__("yield");
#else
    std::atomic_signal_fence(std::memory_order_seq_cst);
#endif
}

class Waiter {
public:
    explicit Waiter(WaitStrategy strategy) : strategy_(strategy) {
        if (strategy != WaitStrategy::BusySpin &&
            strategy != WaitStrategy::Yielding &&
            strategy != WaitStrategy::Blocking) {
            throw std::invalid_argument("Unknown wait strategy");
        }
    }

    template<class Predicate>
    void wait(Predicate ready) {
        if (strategy_ == WaitStrategy::Blocking) {
            std::unique_lock lock(mutex_);
            cv_.wait(lock, ready);
        } else {
            while (!ready()) {
                if (strategy_ == WaitStrategy::Yielding) {
                    std::this_thread::yield();
                } else {
                    cpu_relax();
                }
            }
        }
    }

    void signal_all() noexcept {
        if (strategy_ == WaitStrategy::Blocking) {
            // State was changed before taking this mutex. The handshake
            // prevents a change/notification from falling between the
            // waiter's predicate check and its atomic unlock-and-wait.
            { std::lock_guard lock(mutex_); }
            cv_.notify_all();
        }
    }

private:
    const WaitStrategy strategy_;
    std::mutex mutex_;
    std::condition_variable cv_;
};
} // namespace detail

template<class Event, ProducerMode Mode = ProducerMode::Multi>
class Disruptor {
public:
    using sequence_type = std::int64_t;

    class ConsumerHandle {
    public:
        ConsumerHandle() = default;
    private:
        friend class Disruptor;
        ConsumerHandle(const Disruptor* owner, std::size_t index)
            : owner_(owner), index_(index) {}
        const Disruptor* owner_ = nullptr;
        std::size_t index_ = 0;
    };

    explicit Disruptor(std::size_t capacity,
                       WaitStrategy strategy = WaitStrategy::BusySpin,
                       std::size_t max_batch = 64)
        : capacity_(checked_capacity(capacity)), mask_(capacity_ - 1),
          max_batch_(checked_batch(max_batch)),
          slots_(std::make_unique<Slot[]>(capacity_)), waiter_(strategy) {
        static_assert(std::is_default_constructible_v<Event>,
                      "Event must be default-constructible; slots are preallocated");
        static_assert(Mode == ProducerMode::Single || Mode == ProducerMode::Multi);
    }

    Disruptor(const Disruptor&) = delete;
    Disruptor& operator=(const Disruptor&) = delete;
    Disruptor(Disruptor&&) = delete;
    Disruptor& operator=(Disruptor&&) = delete;

    ~Disruptor() {
        // Destruction from a callback, or while another control thread is
        // using this object, is forbidden. Join external callers first.
        halt();
        wait_for_publishers();
        join_workers();
    }

    // Register in topological order. Each handle must name an earlier node
    // in this instance; this makes cycles impossible by construction.
    template<class Handler>
    ConsumerHandle add_consumer(std::string name,
                                std::initializer_list<ConsumerHandle> dependencies,
                                Handler&& handler) {
        static_assert(std::is_invocable_r_v<void, Handler&, Event&,
                                            sequence_type, bool>);
        if (start_called_ || aborted_.load(std::memory_order_acquire)) {
            throw std::logic_error("Consumer graph is frozen");
        }
        for (const auto& worker : workers_) {
            if (worker->name == name) {
                throw std::invalid_argument("Duplicate consumer name");
            }
        }
        auto worker = std::make_unique<Worker>();
        worker->name = std::move(name);
        worker->handler = std::forward<Handler>(handler);
        for (const auto handle : dependencies) {
            auto* dependency = &checked_worker(handle);
            if (std::find(worker->dependencies.begin(), worker->dependencies.end(),
                          dependency) != worker->dependencies.end()) {
                throw std::invalid_argument("Duplicate dependency");
            }
            worker->dependencies.push_back(dependency);
        }
        const auto index = workers_.size();
        workers_.push_back(std::move(worker));
        return ConsumerHandle(this, index);
    }

    // add_consumer(), start(), shutdown() are serialized control operations.
    // Publication may begin only after start() has returned successfully.
    void start() {
        if (start_called_ || aborted_.load(std::memory_order_acquire)) {
            throw std::logic_error("Disruptor cannot be started twice/restarted");
        }
        if (workers_.empty()) {
            throw std::logic_error("At least one consumer is required");
        }
        start_called_ = true;
        // A terminal consumer cannot overtake any ancestor. Gating on all
        // terminal nodes therefore protects every reader of a slot.
        std::vector<bool> has_successor(workers_.size(), false);
        for (const auto& worker : workers_) {
            for (const auto* dependency : worker->dependencies) {
                for (std::size_t i = 0; i < workers_.size(); ++i) {
                    if (workers_[i].get() == dependency) has_successor[i] = true;
                }
            }
        }
        for (std::size_t i = 0; i < workers_.size(); ++i) {
            if (!has_successor[i]) gates_.push_back(&workers_[i]->completed);
        }
        try {
            for (auto& worker : workers_) {
                worker->thread = std::thread([this, node = worker.get()] {
                    run(*node);
                });
            }
        } catch (...) {
            halt();
            join_workers();
            throw;
        }
        admission_.store(0, std::memory_order_release);
        started_.store(true, std::memory_order_release);
    }

    // The writer must fill every field needed for the NEW logical event.
    // Slots retain previous contents. Do not retain Event& after returning.
    // noexcept is required so a claimed sequence can never be abandoned.
    template<class Writer>
    sequence_type publish(Writer&& writer) {
        check_writer<Writer>();
        Publication call(*this);
        for (;;) {
            if (aborted_.load(std::memory_order_acquire)) throw_closed();
            if (const auto sequence = try_claim()) {
                write_and_publish(*sequence, writer);
                return *sequence;
            }
            waiter_.wait([this] {
                return aborted_.load(std::memory_order_acquire) || has_capacity();
            });
        }
    }

    // nullopt means insufficient capacity at this attempt. Closed/failure
    // throws. This is not a wait-free API: Multi mode can retry a CAS, and
    // Blocking mode still uses the notification mutex after publication.
    template<class Writer>
    std::optional<sequence_type> try_publish(Writer&& writer) {
        check_writer<Writer>();
        Publication call(*this);
        if (aborted_.load(std::memory_order_acquire)) throw_closed();
        const auto sequence = try_claim();
        if (sequence) write_and_publish(*sequence, writer);
        return sequence;
    }

    // Close admission atomically, finish all calls already admitted, drain
    // every published event through EVERY consumer, then join the workers.
    // This may run concurrently with publication, but only one controller
    // calls shutdown. A callback/writer must eventually return.
    void shutdown() {
        if (active_consumer_ == this || inside_publication()) {
            throw std::logic_error("shutdown from this instance's callback is forbidden");
        }
        if (!started_.load(std::memory_order_acquire)) {
            throw std::logic_error("shutdown requires a successful start");
        }
        if (!shutdown_done_) {
            admission_.fetch_or(closed_bit, std::memory_order_acq_rel);
            waiter_.signal_all();
            wait_for_publishers();
            drain_target_.store(claimed(), std::memory_order_relaxed);
            draining_.store(true, std::memory_order_release);
            waiter_.signal_all();
            join_workers();
            shutdown_done_ = true;
        }
        rethrow_if_failed();
        if (aborted_.load(std::memory_order_acquire)) throw Closed();
    }

    // Abort request; safe from another thread or a consumer callback.
    // Does not join or promise delivery. A claimed writer still publishes;
    // callbacks currently executing are not forcibly interrupted.
    void halt() noexcept {
        admission_.fetch_or(closed_bit, std::memory_order_acq_rel);
        aborted_.store(true, std::memory_order_release);
        waiter_.signal_all();
    }

    void rethrow_if_failed() const {
        std::exception_ptr failure;
        { std::lock_guard lock(failure_mutex_); failure = failure_; }
        if (failure) std::rethrow_exception(failure);
    }

    [[nodiscard]] std::size_t capacity() const noexcept { return capacity_; }
    // Reservation frontier, NOT a contiguous publication frontier.
    [[nodiscard]] sequence_type claimed() const noexcept {
        return next_claim_.value.load(std::memory_order_acquire) - 1;
    }
    [[nodiscard]] sequence_type progress(ConsumerHandle handle) const {
        return checked_worker(handle).completed.value.load(std::memory_order_acquire);
    }

private:
    struct alignas(64) Sequence {
        explicit Sequence(sequence_type initial = -1) : value(initial) {}
        std::atomic<sequence_type> value;
    };
    struct alignas(64) Slot {
        std::atomic<sequence_type> published{-1};
        Event event{};
    };
    struct Worker {
        std::string name;
        Sequence completed;
        std::vector<Worker*> dependencies;
        std::function<void(Event&, sequence_type, bool)> handler;
        std::thread thread;
    };

    class Publication {
    public:
        explicit Publication(Disruptor& owner) : owner_(owner) {
            owner_.enter();
            previous_ = active_publication_;
            active_publication_ = this;
        }
        ~Publication() {
            active_publication_ = previous_;
            owner_.admission_.fetch_sub(1, std::memory_order_acq_rel);
            owner_.waiter_.signal_all();
        }
        Publication(const Publication&) = delete;
        Publication& operator=(const Publication&) = delete;
    private:
        friend class Disruptor;
        Disruptor& owner_;
        Publication* previous_ = nullptr;
    };

    static constexpr std::uint64_t closed_bit = std::uint64_t{1} << 63;
    static constexpr std::uint64_t count_mask = closed_bit - 1;
    inline static thread_local const Disruptor* active_consumer_ = nullptr;
    inline static thread_local Publication* active_publication_ = nullptr;

    bool inside_publication() const noexcept {
        for (auto* call = active_publication_; call; call = call->previous_) {
            if (&call->owner_ == this) return true;
        }
        return false;
    }

    static std::size_t checked_capacity(std::size_t capacity) {
        if (capacity < 2 || !std::has_single_bit(capacity) ||
            capacity > static_cast<std::uint64_t>(
                std::numeric_limits<sequence_type>::max() / 2)) {
            throw std::invalid_argument("Capacity must be a power of two >= 2, within int64 range");
        }
        return capacity;
    }
    static std::size_t checked_batch(std::size_t batch) {
        if (batch == 0 || batch > static_cast<std::uint64_t>(
                                      std::numeric_limits<sequence_type>::max())) {
            throw std::invalid_argument("max_batch must be positive, within int64 range");
        }
        return batch;
    }
    template<class Writer>
    static constexpr void check_writer() {
        static_assert(std::is_nothrow_invocable_r_v<void, Writer&, Event&, sequence_type>,
                      "Publish writer must be noexcept(Event&, int64_t)");
    }
    Worker& checked_worker(ConsumerHandle handle) const {
        if (handle.owner_ != this || handle.index_ >= workers_.size()) {
            throw std::invalid_argument("Invalid/foreign consumer handle");
        }
        return *workers_[handle.index_];
    }

    void enter() {
        if (active_consumer_ == this || inside_publication()) {
            throw std::logic_error("Publishing to this Disruptor from its callback is forbidden");
        }
        if (!started_.load(std::memory_order_acquire)) {
            throw std::logic_error("Publication requires start()");
        }
        auto state = admission_.load(std::memory_order_acquire);
        for (;;) {
            if (state & closed_bit) throw_closed();
            if ((state & count_mask) == count_mask) {
                throw std::overflow_error("Too many concurrent publishers");
            }
            if (admission_.compare_exchange_weak(state, state + 1,
                    std::memory_order_acq_rel, std::memory_order_acquire)) return;
        }
    }
    [[noreturn]] void throw_closed() const {
        rethrow_if_failed();
        throw Closed();
    }
    void wait_for_publishers() {
        waiter_.wait([this] {
            return (admission_.load(std::memory_order_acquire) & count_mask) == 0;
        });
    }
    void join_workers() {
        for (auto& worker : workers_) {
            if (worker->thread.joinable()) worker->thread.join();
        }
    }

    [[nodiscard]] bool capacity_for(sequence_type candidate) const noexcept {
        const auto wrap_point = candidate - static_cast<sequence_type>(capacity_);
        for (const auto* gate : gates_) {
            // Reader's release -> producer's acquire protects slot reuse,
            // including reads/writes performed by the gate's ancestors.
            if (gate->value.load(std::memory_order_acquire) < wrap_point) return false;
        }
        return true;
    }
    [[nodiscard]] bool has_capacity() const noexcept {
        return capacity_for(next_claim_.value.load(std::memory_order_relaxed));
    }

    std::optional<sequence_type> try_claim() {
        auto candidate = next_claim_.value.load(std::memory_order_relaxed);
        for (;;) {
            if (aborted_.load(std::memory_order_acquire)) throw_closed();
            // Never perform signed overflow; no sequence generation reset.
            if (candidate == std::numeric_limits<sequence_type>::max()) {
                throw std::overflow_error("Sequence space exhausted; create a new instance");
            }
            if (!capacity_for(candidate)) return std::nullopt;
            if constexpr (Mode == ProducerMode::Single) {
                // Contract: at most ONE active publication call in Single mode.
                next_claim_.value.store(candidate + 1, std::memory_order_relaxed);
                return candidate;
            } else {
                if (next_claim_.value.compare_exchange_weak(candidate, candidate + 1,
                        std::memory_order_relaxed, std::memory_order_relaxed)) {
                    return candidate;
                }
                // Recheck capacity for the newly observed candidate.
            }
        }
    }

    [[nodiscard]] Slot& slot(sequence_type sequence) noexcept {
        return slots_[static_cast<std::size_t>(sequence) & mask_];
    }
    template<class Writer>
    void write_and_publish(sequence_type sequence, Writer& writer) noexcept {
        auto& entry = slot(sequence);
        std::invoke(writer, entry.event, sequence);
        // Reservation order can differ from completion order. Publishing the
        // absolute generation of THIS slot cannot accidentally publish a hole.
        entry.published.store(sequence, std::memory_order_release);
        waiter_.signal_all();
    }

    [[nodiscard]] bool drained(sequence_type next) const noexcept {
        return draining_.load(std::memory_order_acquire) &&
               next > drain_target_.load(std::memory_order_relaxed);
    }

    // A sequence barrier: predecessor completion bounds + a CONTIGUOUS run
    // of individually published slots. No "largest published" shortcut.
    sequence_type available_batch(const Worker& worker, sequence_type next) noexcept {
        auto bound = next_claim_.value.load(std::memory_order_acquire) - 1;
        for (const auto* dependency : worker.dependencies) {
            bound = std::min(bound,
                dependency->completed.value.load(std::memory_order_acquire));
        }
        if (bound < next) return next - 1;
        const auto count = std::min(static_cast<std::uint64_t>(bound - next) + 1,
                                    static_cast<std::uint64_t>(max_batch_));
        const auto end = next + static_cast<sequence_type>(count) - 1;
        for (auto sequence = next; sequence <= end; ++sequence) {
            // Producer's release -> consumer's acquire makes the event visible.
            if (slot(sequence).published.load(std::memory_order_acquire) != sequence) {
                return sequence - 1;
            }
        }
        return end;
    }

    void record_failure(std::exception_ptr error) noexcept {
        { std::lock_guard lock(failure_mutex_); if (!failure_) failure_ = error; }
        // Do not hold failure_mutex_ while notifying Blocking waiters.
        halt();
    }
    void run(Worker& worker) noexcept {
        const auto* previous = active_consumer_;
        active_consumer_ = this;
        try {
            sequence_type next = 0;
            while (!aborted_.load(std::memory_order_acquire) && !drained(next)) {
                const auto available = available_batch(worker, next);
                if (available < next) {
                    waiter_.wait([this, &worker, next] {
                        return aborted_.load(std::memory_order_acquire) || drained(next) ||
                               available_batch(worker, next) >= next;
                    });
                    continue;
                }
                for (auto sequence = next; sequence <= available; ++sequence) {
                    if (aborted_.load(std::memory_order_acquire)) break;
                    worker.handler(slot(sequence).event, sequence, sequence == available);
                }
                if (aborted_.load(std::memory_order_acquire)) break;
                worker.completed.value.store(available, std::memory_order_release);
                next = available + 1;
                waiter_.signal_all();
            }
        } catch (...) {
            record_failure(std::current_exception());
        }
        active_consumer_ = previous;
    }

    const std::size_t capacity_;
    const std::size_t mask_;
    const std::size_t max_batch_;
    std::unique_ptr<Slot[]> slots_;
    std::vector<std::unique_ptr<Worker>> workers_;
    std::vector<Sequence*> gates_;
    Sequence next_claim_{0};
    alignas(64) std::atomic<std::uint64_t> admission_{closed_bit};
    std::atomic<bool> started_{false};
    std::atomic<bool> aborted_{false};
    std::atomic<bool> draining_{false};
    std::atomic<sequence_type> drain_target_{-1};
    bool start_called_ = false;
    bool shutdown_done_ = false;
    detail::Waiter waiter_;
    mutable std::mutex failure_mutex_;
    std::exception_ptr failure_;
};
} // namespace disruptor
