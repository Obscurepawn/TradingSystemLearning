#include "stop_bit.hpp"
#include <cassert>
#include <iostream>
#include <random>
#include <vector>

using Bytes = std::vector<std::uint8_t>;
using namespace stopbit;

// Independent reference: base-128 division, temporary buffer, then reversal.
Bytes reference_encode(std::uint64_t v) {
    Bytes bytes;
    do { bytes.push_back(static_cast<std::uint8_t>(v % 128)); v /= 128; } while (v);
    std::reverse(bytes.begin(), bytes.end());
    bytes.back() |= 128;
    return bytes;
}
Result reference_decode(std::span<const std::uint8_t> bytes, std::uint64_t& value) {
    __uint128_t wide = 0;
    for (std::size_t i = 0; i < bytes.size(); ++i) {
        auto b = bytes[i];
        if (i == 0 && b == 0) return {Status::invalid, 1};
        wide = wide * 128 + (b % 128);
        if (wide > UINT64_MAX || (i == 9 && b < 128))
            return {Status::invalid, i + 1};
        if (b >= 128) { value = static_cast<std::uint64_t>(wide); return {Status::done, i+1}; }
    }
    return {Status::need_more, bytes.size()};
}
void check_bytes(const Bytes& bytes) {
    std::uint64_t expected = 0;
    const auto ref = reference_decode(bytes, expected);
    UIntState state;
    const auto r = decode(bytes, state);
    assert(r.status == ref.status && r.consumed == ref.consumed);
    if (r.status == Status::done) assert(state.value == expected);
    // Compare all two-chunk boundaries, including empty chunks.
    for (std::size_t cut = 0; cut <= bytes.size(); ++cut) {
        UIntState split;
        auto a = decode(std::span{bytes}.first(cut), split);
        auto result = a;
        if (a.status == Status::need_more) {
            result = decode(std::span{bytes}.subspan(cut), split);
            result.consumed += a.consumed;
        }
        assert(result.status == ref.status && result.consumed == ref.consumed);
        if (result.status == Status::done) assert(split.value == expected);
    }
}
void check_value(std::uint64_t value) {
    const auto expected = reference_encode(value);
    Bytes buffer(expected.size()); // Exact allocation: ASan catches tail overreads.
    assert(encode(value, buffer) == expected.size() && buffer == expected);
    Bytes short_buffer(expected.size()-1, 0x55);
    const auto original = short_buffer;
    assert(encode(value, short_buffer) == 0 && short_buffer == original);
    check_bytes(buffer);
    // Force the >=10-byte unrolled path, without changing the first integer.
    buffer.resize(16, 0xff);
    check_bytes(buffer);
}
int main() {
    assert(reference_encode(0) == Bytes{0x80});
    assert(reference_encode(127) == Bytes{0xff});
    assert(reference_encode(128) == (Bytes{0x01,0x80}));
    assert(reference_encode(300) == (Bytes{0x02,0xac}));
    assert(reference_encode(UINT64_MAX) == (Bytes{1,127,127,127,127,127,127,127,127,255}));
    for (unsigned i = 0; i < 64; ++i) {
        const auto v = std::uint64_t{1} << i;
        check_value(v-1); check_value(v); if (v != UINT64_MAX) check_value(v+1);
    }
    check_value(UINT64_MAX);
    check_bytes({});
    for (unsigned a = 0; a < 256; ++a) {
        check_bytes(Bytes{static_cast<std::uint8_t>(a)});
        for (unsigned b = 0; b < 256; ++b)
            check_bytes(Bytes{static_cast<std::uint8_t>(a),static_cast<std::uint8_t>(b)});
    }
    std::mt19937_64 random(42);
    for (unsigned i = 0; i < 100000; ++i) {
        check_value(random());
        Bytes malformed(random() % 13);
        for (auto& b : malformed) b = static_cast<std::uint8_t>(random());
        check_bytes(malformed);
    }
    // Explicit 10th-byte overflow and missing stop; both full/fragmented paths.
    check_bytes(Bytes{2,0,0,0,0,0,0,0,0,128});
    check_bytes(Bytes{1,0,0,0,0,0,0,0,0,0});
    check_bytes(Bytes{1,0,0,0,0,0,0,0,0});

    const Quote q{1,128,2,3};
    std::array<std::uint8_t,40> encoded{};
    const auto size = encode_quote(q, encoded);
    assert(size == 5);
    Bytes wire(encoded.begin(), encoded.begin()+size);
    wire.insert(wire.end(), encoded.begin(), encoded.begin()+size);
    for (unsigned mask = 0; mask < (1u << (wire.size()-1)); ++mask) {
        QuoteStream parser;
        unsigned count = 0;
        auto emit = [&](const Quote& got) noexcept { assert(got == q); ++count; };
        std::size_t start = 0;
        for (std::size_t end = 1; end <= wire.size(); ++end) {
            if (end == wire.size() || (mask & (1u << (end-1)))) {
                const auto r = parser.feed(std::span{wire}.subspan(start,end-start), emit);
                assert(r.status != Status::invalid && r.consumed == end-start);
                parser.feed({}, emit);
                start = end;
            }
        }
        assert(count == 2 && parser.at_message_boundary());
    }
    for (std::size_t n = 0; n <= wire.size(); ++n) {
        QuoteStream parser;
        parser.feed(std::span{wire}.first(n), [](const Quote&) noexcept {});
        assert(parser.at_message_boundary() == (n == 0 || n == 5 || n == 10));
    }
    // Long fields, each possible split, heterogeneous messages, persistent failure.
    const Quote maximum{UINT64_MAX,UINT64_MAX,UINT64_MAX,UINT64_MAX};
    assert(encode_quote(maximum, encoded) == 40);
    wire.assign(encoded.begin(), encoded.end());
    encode_quote(q, encoded);
    wire.insert(wire.end(), encoded.begin(), encoded.begin()+size);
    for (std::size_t cut = 0; cut <= wire.size(); ++cut) {
        QuoteStream parser;
        unsigned count = 0;
        auto emit = [&](const Quote& got) noexcept { assert(got == (count++ == 0 ? maximum : q)); };
        parser.feed(std::span{wire}.first(cut), emit);
        assert(parser.feed(std::span{wire}.subspan(cut), emit).status == Status::done);
        assert(count == 2 && parser.at_message_boundary());
    }
    // Every long integer split into individual bytes.
    {
        QuoteStream one_byte;
        unsigned emitted = 0;
        for (auto byte : wire) {
            const auto r = one_byte.feed(std::span{&byte, 1}, [&](const Quote& got) noexcept {
                assert(got == (emitted++ == 0 ? maximum : q));
            });
            assert(r.status != Status::invalid && r.consumed == 1);
        }
        assert(emitted == 2 && one_byte.at_message_boundary());
    }
    // Independent expected structs, random values and random receive boundaries.
    {
        std::vector<Quote> expected;
        Bytes random_wire;
        for (unsigned i = 0; i < 10000; ++i) {
            expected.push_back(Quote{random(),random(),random(),random()});
            const auto n = encode_quote(expected.back(), encoded);
            random_wire.insert(random_wire.end(), encoded.begin(), encoded.begin()+n);
        }
        QuoteStream random_stream;
        std::size_t delivered = 0;
        for (std::size_t pos = 0; pos < random_wire.size();) {
            const auto n = std::min<std::size_t>(1+random()%64,random_wire.size()-pos);
            const auto r = random_stream.feed(std::span{random_wire}.subspan(pos,n),
                [&](const Quote& got) noexcept {
                    assert(delivered < expected.size() && got == expected[delivered]);
                    ++delivered;
                });
            assert(r.status != Status::invalid && r.consumed == n);
            pos += n;
        }
        assert(delivered == expected.size() && random_stream.at_message_boundary());
    }
    wire.push_back(0);
    QuoteStream parser;
    unsigned count = 0;
    auto emit = [&](const Quote&) noexcept { ++count; };
    auto r = parser.feed(wire, emit);
    assert(r.status == Status::invalid && r.consumed == wire.size() && count == 2);
    assert(!parser.at_message_boundary());
    assert(parser.feed(wire, emit).status == Status::invalid && count == 2);
    std::array<std::uint8_t,3> tiny{1,2,3};
    assert(encode_quote(q, tiny) == 0 && (tiny == std::array<std::uint8_t,3>{1,2,3}));
    std::cout << "PASS: boundary/golden vectors, 100k random values + malformed inputs,\n"
                 "all 1/2-byte sequences, split points, 512 stream partitions, 10k random structs, EOF and sticky errors\n";
}
