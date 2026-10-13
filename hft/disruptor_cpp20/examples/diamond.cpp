#include "disruptor.hpp"

#include <array>
#include <cstdint>
#include <exception>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <vector>

struct Event {
    std::uint64_t id = 0;
    std::uint64_t left = 0;
    std::uint64_t right = 0;
};

int main() {
    try {
        constexpr std::size_t producer_count = 4;
        constexpr std::uint64_t per_producer = 10'000;
        constexpr std::uint64_t total = producer_count * per_producer;

        // Each callback owns its counter; main reads counters only after shutdown joins.
        std::uint64_t count_a = 0;
        std::uint64_t count_b = 0;
        std::uint64_t count_c = 0;
        std::int64_t next_sequence = 0;
        std::vector<std::uint8_t> seen(static_cast<std::size_t>(total), 0);
        std::array<std::uint64_t, producer_count> published{};
        std::mutex error_mutex;
        std::exception_ptr producer_error;
        std::vector<std::thread> producers;
        producers.reserve(producer_count);

        disruptor::Disruptor<Event, disruptor::ProducerMode::Multi> bus(
            1024, disruptor::WaitStrategy::Blocking, 64);

        // A and B run independently and modify different scalar memory locations.
        const auto a = bus.add_consumer("A", {},
            [&](Event& event, std::int64_t, bool) {
                event.left = event.id * 3 + 1;
                ++count_a;
            });
        const auto b = bus.add_consumer("B", {},
            [&](Event& event, std::int64_t, bool) {
                event.right = event.id * 7 + 2;
                ++count_b;
            });
        const auto c = bus.add_consumer("C", {a, b},
            [&](Event& event, std::int64_t sequence, bool) {
                if (sequence != next_sequence || event.id >= total ||
                    event.left != event.id * 3 + 1 ||
                    event.right != event.id * 7 + 2 ||
                    seen[static_cast<std::size_t>(event.id)] != 0) {
                    throw std::runtime_error("diamond validation failed");
                }
                seen[static_cast<std::size_t>(event.id)] = 1;
                ++next_sequence;
                ++count_c;
            });

        bus.start();
        try {
            for (std::size_t producer = 0; producer < producer_count; ++producer) {
                producers.emplace_back([&, producer] {
                    try {
                        for (std::uint64_t i = 0; i < per_producer; ++i) {
                            const auto id = static_cast<std::uint64_t>(producer) *
                                            per_producer + i;
                            bus.publish([id](Event& event, std::int64_t) noexcept {
                                event.id = id;
                                event.left = 0;
                                event.right = 0;
                            });
                            ++published[producer];
                        }
                    } catch (...) {
                        {
                            std::lock_guard<std::mutex> lock(error_mutex);
                            if (!producer_error) {
                                producer_error = std::current_exception();
                            }
                        }
                        bus.halt();
                    }
                });
            }
        } catch (...) {
            // Thread creation can fail; stop first so existing publishers can exit.
            bus.halt();
            for (auto& producer : producers) {
                producer.join();
            }
            throw;
        }

        for (auto& producer : producers) {
            producer.join();
        }
        try {
            bus.shutdown();
        } catch (const disruptor::Closed&) {
            // An explicit halt reports Closed after joining consumers. Preserve
            // the application publisher failure that requested that halt.
            if (producer_error) {
                std::rethrow_exception(producer_error);
            }
            throw;
        }
        if (producer_error) {
            std::rethrow_exception(producer_error);
        }

        std::uint64_t published_total = 0;
        for (const auto count : published) {
            published_total += count;
        }
        const auto last = static_cast<std::int64_t>(total) - 1;
        if (published_total != total || count_a != total || count_b != total ||
            count_c != total || bus.claimed() != last ||
            bus.progress(a) != last || bus.progress(b) != last ||
            bus.progress(c) != last) {
            throw std::runtime_error("unexpected completion counts");
        }

        std::cout << "published=" << published_total
                  << " A=" << count_a << " B=" << count_b
                  << " C=" << count_c << " verified=" << count_c << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "diamond: " << error.what() << '\n';
        return 1;
    }
}
