#include <algorithm>
#include <cassert>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <queue>
#include <sstream>
#include <string>
#include <stdexcept>
#include <utility>
#include <tuple>
#include <vector>

// One visible FIFO price level, integer quantities, virtual microseconds.
// This is a mechanism experiment; there is no wall-clock performance benchmark.
struct Order { std::string id; int qty; };
struct Event {
    int time, phase;
    std::uint64_t sequence;
    std::function<void()> action;
};
struct Later {
    bool operator()(const Event& a, const Event& b) const {
        return std::tie(a.time, a.phase, a.sequence) >
               std::tie(b.time, b.phase, b.sequence);
    }
};
enum class QueueModel { Exact, CancelFromBack, RandomUnits };
struct Config {
    std::string name = "baseline";
    int feed_delay = 30, entry_delay = 20, cancel_delay = 20, report_delay = 20;
    int b_cancel = 20, extra_a_cancel = 0;
    bool touch = false, uniform = false, add_tail = false, cancel_first = false;
    bool reactive_b = false;
    QueueModel queue = QueueModel::Exact;
    std::uint32_t seed = 20261019;
};
struct StatePoint { int time, depth, trades; };
struct Result {
    int filled = 0, canceled = 0, first_fill = -1, cancel_time = -1;
    int known_filled = 0, ack_time = -1;
    std::string fills, events;
    std::vector<StatePoint> points;
};
class Simulation {
    Config c;
    std::priority_queue<Event, std::vector<Event>, Later> events;
    std::vector<Order> book;
    std::uint64_t serial = 0;
    std::uint32_t rng;
    int now = 0, market_trades = 0, local_depth = 0, local_leaves = 0;
    bool local_pending_cancel = false, background_keeps_b = false;
    Result result;
    std::ostringstream fill_log, event_log;
    int remaining() const {
        for (const auto& o : book) if (o.id == "me") return o.qty;
        return 0;
    }
    int depth() const {
        int q = 0;
        for (const auto& o : book) q += o.qty;
        return q;
    }
    void log(const std::string& kind, int qty = 0) {
        event_log << now << ',' << kind << ',' << qty << ',' << remaining()
                  << ',' << result.known_filled << ',' << local_leaves << ','
                  << local_pending_cancel << ',' << local_depth << '\n';
    }
    void schedule(int time, int phase, std::function<void()> fn) {
        assert(time >= now);
        events.push({time, phase, serial++, std::move(fn)});
    }
    void clean() {
        std::erase_if(book, [](const Order& o) { return o.qty == 0; });
    }
    void publish_depth() {
        const int q = depth();
        schedule(now + c.feed_delay, 2, [this, q] {
            local_depth = q;
            log("PUBLIC_DEPTH", q);
        });
    }
    void fill(int qty) {
        if (!qty) return;
        if (result.first_fill < 0) result.first_fill = now;
        result.filled += qty;
        const int exchange_time = now;
        fill_log << now << ',' << now + c.report_delay << ",me,BUY,100,"
                 << qty << ',' << result.filled << '\n';
        log("EXCHANGE_FILL", qty);
        schedule(now + c.report_delay, 1, [this, qty, exchange_time] {
            (void)exchange_time;
            result.known_filled += qty;
            local_leaves -= qty;
            assert(local_leaves >= 0);
            log("PRIVATE_FILL", qty);
        });
    }
    void sell(int qty) {
        // Deliberately wrong benchmark: a touch grants the whole order a fill.
        if (c.touch && remaining() > 0) {
            const int q = remaining();
            for (auto& o : book) if (o.id == "me") o.qty = 0;
            clean();
            fill(q);
        }
        for (auto& o : book) {
            const int q = std::min(qty, o.qty);
            o.qty -= q;
            qty -= q;
            market_trades += q;
            if (o.id == "me") fill(q);
            if (qty == 0) break;
        }
        clean();
        log("SELL_PROCESSED");
        publish_depth();
    }
    std::uint32_t random_word() {
        rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;
        return rng;
    }
    // Rejection sampling avoids modulo bias, with a fixed generator algorithm.
    int random_index(int bound) {
        const auto b = static_cast<std::uint32_t>(bound);
        const auto threshold = static_cast<std::uint32_t>(-b) % b;
        std::uint32_t x;
        do { x = random_word(); } while (x < threshold);
        return static_cast<int>(x % b);
    }
    void cancel_external(const std::string& id, int qty) {
        if (c.queue == QueueModel::Exact || remaining() == 0) {
            for (auto& o : book) if (o.id == id) {
                const int removed = std::min(qty, o.qty);
                o.qty -= removed;
                qty -= removed;
            }
        } else if (c.queue == QueueModel::CancelFromBack) {
            // Cancel behind first, then ahead when the rear cannot absorb it.
            for (auto it = book.rbegin(); it != book.rend() && qty; ++it) {
                if (it->id == "me") continue;
                const int removed = std::min(qty, it->qty);
                it->qty -= removed;
                qty -= removed;
            }
        } else {
            // Every external unit is equally likely to be canceled.
            while (qty-- > 0) {
                int total = 0;
                for (const auto& o : book) if (o.id != "me") total += o.qty;
                assert(total > 0);
                int pick = random_index(total);
                for (auto& o : book) if (o.id != "me") {
                    if (pick < o.qty) { --o.qty; break; }
                    pick -= o.qty;
                }
            }
        }
        clean();
        log("EXTERNAL_CANCEL");
        publish_depth();
    }
public:
    explicit Simulation(Config cfg) : c(std::move(cfg)), rng(c.seed) {}
    Result run() {
        fill_log << "exchange_us,known_us,order_id,side,price,qty,cumulative\n";
        event_log << "time_us,event,qty,exchange_leaves,known_filled,local_leaves,pending_cancel,local_depth\n";
        schedule(0, 0, [this] {
            book = {{"A", 40}, {"B", 20}};
            publish_depth();
            schedule(now + c.feed_delay, 3, [this] {
                log("INITIAL_SIGNAL");
                schedule(now + 10, 3, [this] {
                    local_leaves = 20;
                    log("SEND_NEW", 20);
                    schedule(now + c.entry_delay, 0, [this] {
                        book.push_back({"me", 20});
                        log("ACCEPT", 20);
                        publish_depth();
                        if (c.reactive_b) schedule(now + 10, 3, [this] {
                            log("B_OBSERVES_MY_ORDER");
                            schedule(now + 3, 3, [this] {
                                background_keeps_b = true;
                                log("B_DECIDES_TO_KEEP");
                            });
                        });
                        schedule(now + c.report_delay, 1, [this] {
                            result.ack_time = now;
                            log("PRIVATE_ACK", 20);
                        });
                    });
                });
            });
        });
        if (c.add_tail) schedule(65, 0, [this] {
            book.push_back({"C", 40});
            log("TAIL_ADD", 40);
            publish_depth();
        });
        // The uniform variant keeps event order and endpoints 70 and 130.
        // Only five external event times change: 70,75,85,90,130 -> 70,85,100,115,130.
        const int cancel_b_at = c.uniform ? 85 : 75;
        const int bad_signal_at = c.uniform ? 100 : 85;
        const int second_sell_at = c.uniform ? 115 : 90;
        schedule(70, 0, [this] { sell(30); });
        schedule(cancel_b_at, 0, [this] {
            if (!background_keeps_b) cancel_external("B", c.b_cancel);
            else log("B_SKIPS_PLANNED_CANCEL");
        });
        if (c.extra_a_cancel) schedule(80, 0, [this] {
            cancel_external("A", c.extra_a_cancel);
        });
        schedule(bad_signal_at, 0, [this] {
            schedule(now + c.feed_delay, 3, [this] {
                log("BAD_SIGNAL");
                schedule(now + 5, 3, [this] {
                    local_pending_cancel = true;
                    log("SEND_CANCEL");
                    schedule(now + c.cancel_delay, c.cancel_first ? -1 : 0, [this] {
                        result.cancel_time = now;
                        const int q = remaining();
                        result.canceled = q;
                        for (auto& o : book) if (o.id == "me") o.qty = 0;
                        clean();
                        log(q ? "CANCEL_ACCEPT" : "CANCEL_REJECT", q);
                        publish_depth();
                        schedule(now + c.report_delay, 1, [this, q] {
                            local_leaves -= q;
                            local_pending_cancel = false;
                            log(q ? "PRIVATE_CANCEL" : "PRIVATE_CANCEL_REJECT", q);
                        });
                    });
                });
            });
        });
        schedule(second_sell_at, 0, [this] { sell(15); });
        schedule(130, 0, [this] { sell(10); });
        while (!events.empty()) {
            auto e = events.top(); events.pop();
            now = e.time;
            e.action();
            result.points.push_back({now, depth(), market_trades});
            for (const auto& o : book) assert(o.qty >= 0);
        }
        assert(result.filled + result.canceled == 20);
        assert(result.known_filled == result.filled && local_leaves == 0);
        result.fills = fill_log.str();
        result.events = event_log.str();
        return result;
    }
};
static std::string sample(const Result& result, int step) {
    std::ostringstream out;
    for (int t = 0; t <= 150; t += step) {
        StatePoint state{0, 0, 0};
        for (const auto& p : result.points) if (p.time <= t) state = p;
        out << t << ':' << state.depth << ':' << state.trades << ';';
    }
    return out.str();
}
static void write(const std::filesystem::path& file, const std::string& text) {
    std::ofstream stream(file);
    if (!stream) throw std::runtime_error("cannot write " + file.string());
    stream << text;
}
int main(int argc, char** argv) {
    const std::filesystem::path out = argc > 1 ? argv[1] : "results";
    std::filesystem::create_directories(out);
    std::vector<Config> configurations;
    Config c;
    configurations.push_back(c);
    c.name = "touch"; c.touch = true; configurations.push_back(c);
    c = {}; c.name = "zero_network_delay";
    c.feed_delay = c.entry_delay = c.cancel_delay = c.report_delay = 0;
    configurations.push_back(c);
    c = {}; c.name = "late_entry_95"; c.entry_delay = 55; configurations.push_back(c);
    c = {}; c.name = "cancel_at_125"; c.cancel_delay = 5; configurations.push_back(c);
    c = {}; c.name = "tie_market_first"; c.cancel_delay = 10; configurations.push_back(c);
    c.name = "tie_cancel_first"; c.cancel_first = true; configurations.push_back(c);
    c = {}; c.name = "uniform_external"; c.uniform = true; configurations.push_back(c);
    c = {}; c.name = "base_mbp_back"; c.queue = QueueModel::CancelFromBack;
    configurations.push_back(c);
    c = {}; c.name = "tail_exact"; c.add_tail = true; configurations.push_back(c);
    c.name = "tail_mbp_back"; c.queue = QueueModel::CancelFromBack;
    configurations.push_back(c);
    c.name = "tail_mbp_random"; c.queue = QueueModel::RandomUnits;
    configurations.push_back(c);
    c = {}; c.name = "reactive_B_stays"; c.reactive_b = true; configurations.push_back(c);
    std::ostringstream summary;
    summary << "scenario,filled,canceled,first_fill_us,cancel_effect_us\n";
    Result baseline;
    for (const auto& cfg : configurations) {
        const auto first = Simulation(cfg).run();
        const auto second = Simulation(cfg).run();
        assert(first.fills == second.fills && first.events == second.events);
        write(out / (cfg.name + ".fills.csv"), first.fills);
        write(out / (cfg.name + ".events.csv"), first.events);
        summary << cfg.name << ',' << first.filled << ',' << first.canceled << ','
                << first.first_fill << ',' << first.cancel_time << '\n';
        if (cfg.name == "baseline") baseline = first;
        if (cfg.name == "touch") assert(first.filled == 20);
        if (cfg.name == "zero_network_delay" || cfg.name == "cancel_at_125" ||
            cfg.name == "tie_cancel_first") assert(first.filled == 5);
        if (cfg.name == "late_entry_95") assert(first.filled == 10);
        if (cfg.name == "tail_mbp_back" || cfg.name == "reactive_B_stays") assert(first.filled == 0);
        if (cfg.name == "base_mbp_back" || cfg.name == "tail_exact" ||
            cfg.name == "tie_market_first" || cfg.name == "uniform_external")
            assert(first.filled == 15);
    }
    assert(baseline.filled == 15 && baseline.first_fill == 90 && baseline.ack_time == 80);
    assert(baseline.fills == "exchange_us,known_us,order_id,side,price,qty,cumulative\n"
        "90,110,me,BUY,100,5,5\n130,150,me,BUY,100,10,15\n");
    write(out / "baseline.repeat.fills.csv", Simulation(Config{}).run().fills);
    write(out / "summary.csv", summary.str());
    std::cout << summary.str();

    // Three full histories with identical 0/150 snapshots, different private fills.
    Config low; low.b_cancel = 5;
    Config high; high.extra_a_cancel = 5;
    const auto low_result = Simulation(low).run(), high_result = Simulation(high).run();
    assert(low_result.filled == 0 && high_result.filled == 20);
    assert(sample(low_result, 150) == sample(baseline, 150));
    assert(sample(high_result, 150) == sample(baseline, 150));
    assert(sample(low_result, 50) != sample(baseline, 50));
    assert(sample(high_result, 50) != sample(baseline, 50));
    std::ostringstream snapshots;
    snapshots << "history,filled,samples_150us,samples_50us\n";
    snapshots << "low," << low_result.filled << ',' << sample(low_result, 150) << ',' << sample(low_result, 50) << '\n';
    snapshots << "baseline," << baseline.filled << ',' << sample(baseline, 150) << ',' << sample(baseline, 50) << '\n';
    snapshots << "high," << high_result.filled << ',' << sample(high_result, 150) << ',' << sample(high_result, 50) << '\n';
    write(out / "snapshots.csv", snapshots.str());
    std::cout << snapshots.str();

    std::ostringstream busy;
    busy << "schedule,arrival,start,finish,wait\n";
    for (const bool uniform : {false, true}) {
        int available = 0;
        const std::vector<int> arrivals = uniform ? std::vector<int>{100,110,120,130}
                                                  : std::vector<int>{100,101,102,130};
        for (const int arrival : arrivals) {
            const int start = std::max(arrival, available);
            available = start + 10;
            busy << (uniform ? "uniform" : "burst") << ',' << arrival << ','
                 << start << ',' << available << ',' << start - arrival << '\n';
        }
    }
    write(out / "busy.csv", busy.str());
    std::cout << busy.str() << "PASS: hand-checked fills, quantity conservation, local reports, snapshot ambiguity, deterministic ledgers\n";
}
