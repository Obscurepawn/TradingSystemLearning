#include "stop_bit.hpp"
#include <chrono>
#include <iomanip>
#include <iostream>
#include <random>
#include <string_view>
#include <vector>

using namespace stopbit;
using Clock = std::chrono::steady_clock;
// GCC/Clang benchmark barriers; codec itself is portable C++20.
template<class T> inline void observe(const T& value) {
    asm volatile("" : : "g"(&value) : "memory");
}
std::size_t baseline_encode(std::uint64_t v, std::uint8_t* out) {
    std::uint8_t reverse[10];
    std::size_t n = 0;
    do { reverse[n++] = static_cast<std::uint8_t>(v & 127); v >>= 7; } while (v);
    for (std::size_t i = 0; i < n; ++i) out[i] = reverse[n-1-i];
    out[n-1] |= 128;
    return n;
}
Result baseline_decode(std::span<const std::uint8_t> input, UIntState& s) {
    s = {};
    for (std::size_t i = 0; i < input.size(); ++i) {
        auto b = input[i];
        if (i == 0 && b == 0) return {Status::invalid,1};
        if (i == 9 && (s.value > (UINT64_MAX >> 7) || (b & 128) == 0))
            return {Status::invalid,i+1};
        s.value = (s.value << 7) | (b & 127);
        if (b & 128) return {Status::done,i+1};
    }
    return {Status::need_more,input.size()};
}
template<class Run> double median_ns(std::size_t items, Run run) {
    constexpr int batches = 16;
    observe(run()); // Warmup.
    std::array<double,7> samples{};
    for (auto& ns : samples) {
        const auto begin = Clock::now();
        for (int batch = 0; batch < batches; ++batch) observe(run());
        const auto end = Clock::now();
        ns = std::chrono::duration<double,std::nano>(end-begin).count() / (items*batches);
    }
    std::sort(samples.begin(),samples.end());
    return samples[3];
}
int main() {
    constexpr std::size_t n = 65536;
    std::mt19937_64 rng(42);
    std::cout << std::fixed << std::setprecision(2);
    for (std::string_view kind : {"u7", "u14", "mixed", "u64"}) {
        std::vector<std::uint64_t> values(n);
        for (auto& v : values) {
            v = rng();
            if (kind == "u7") v &= 127;
            if (kind == "u14") v &= 16383;
            if (kind == "mixed") {
                const auto pick = rng()%100;
                v &= pick < 80 ? 127ULL : pick < 95 ? 16383ULL : UINT64_MAX;
            }
        }
        std::vector<std::uint8_t> wire(n*10), output(n*10);
        std::size_t bytes = 0;
        for (auto v : values) bytes += encode_unchecked(v,wire.data()+bytes);
        wire.resize(bytes);
        auto enc = [&]<bool optimized>() {
            observe(values.data());
            std::size_t pos = 0;
            for (auto v : values) {
                if constexpr (optimized) pos += encode_unchecked(v,output.data()+pos);
                else pos += baseline_encode(v,output.data()+pos);
            }
            observe(output.data());
            return pos;
        };
        auto dec = [&]<bool optimized>() {
            observe(wire.data());
            std::uint64_t sum = 0;
            std::size_t pos = 0;
            while (pos < wire.size()) {
                UIntState state;
                Result r;
                if constexpr (optimized) r = decode(std::span{wire}.subspan(pos),state);
                else r = baseline_decode(std::span{wire}.subspan(pos),state);
                if (r.status != Status::done) std::abort();
                sum += state.value;
                pos += r.consumed;
            }
            return sum;
        };
        std::cout << kind << " bytes/value=" << double(bytes)/n
                  << " encode baseline=" << median_ns(n,[&]{return enc.template operator()<false>();})
                  << " optimized=" << median_ns(n,[&]{return enc.template operator()<true>();})
                  << " decode baseline=" << median_ns(n,[&]{return dec.template operator()<false>();})
                  << " optimized=" << median_ns(n,[&]{return dec.template operator()<true>();}) << " ns/value\n";
    }
    // Synthetic independent fields; a software parsing workload, not recorded exchange data.
    std::vector<std::uint8_t> wire(n*40);
    std::size_t bytes = 0;
    for (std::size_t i=0;i<n;++i) {
        Quote q{rng()%4096, i, 100000+rng()%1000, rng()%1000};
        bytes += encode_quote(q,std::span{wire}.subspan(bytes));
    }
    wire.resize(bytes);
    for (std::size_t chunk : {std::size_t{1},std::size_t{64},std::size_t{4096},wire.size()}) {
        auto run = [&] {
            observe(wire.data());
            QuoteStream parser;
            std::uint64_t sum = 0;
            auto emit = [&](const Quote& q) noexcept {
                sum += q.instrument_id + q.sequence + q.price_ticks + q.quantity;
            };
            for (std::size_t pos=0;pos<wire.size();) {
                const auto size = std::min(chunk,wire.size()-pos);
                if (parser.feed(std::span{wire}.subspan(pos,size),emit).status == Status::invalid)
                    std::abort();
                pos += size;
            }
            return sum;
        };
        std::cout << "stream chunk=" << chunk << " bytes, " << median_ns(n,run)
                  << " ns/quote, bytes/quote=" << double(bytes)/n << '\n';
    }
}
