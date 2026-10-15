#pragma once
#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>
#include <type_traits>
#include <utility>

namespace stopbit {
// FAST-style mandatory unsigned integer, shortest representation only.
enum class Status { done, need_more, invalid };
struct UIntState {
    std::uint64_t value = 0;
    unsigned count = 0;
};
struct Result {
    Status status;
    std::size_t consumed; // Bytes consumed from THIS span, including an invalid byte.
};

constexpr std::size_t encoded_size(std::uint64_t value) noexcept {
    return std::max<std::size_t>(1, (std::bit_width(value) + 6) / 7);
}

// Caller guarantees enough writable bytes. Returns bytes written.
inline std::size_t encode_unchecked(std::uint64_t value,
                                    std::uint8_t* out) noexcept {
    if (value < 128) {
        out[0] = static_cast<std::uint8_t>(value | 0x80);
        return 1;
    }
    const auto size = encoded_size(value);
    out[size - 1] = static_cast<std::uint8_t>((value & 0x7f) | 0x80);
    for (auto i = size - 1; i != 0; ) {
        value >>= 7;
        out[--i] = static_cast<std::uint8_t>(value & 0x7f);
    }
    return size;
}

// Zero means insufficient capacity; output remains unchanged.
inline std::size_t encode(std::uint64_t value,
                          std::span<std::uint8_t> out) noexcept {
    if (out.size() < encoded_size(value)) return 0;
    return encode_unchecked(value, out.data());
}

namespace detail {
// Used only when at least 10 input bytes are accessible. I is a byte index.
// Compile-time expansion removes loop bookkeeping and per-byte bounds checks.
template<unsigned I>
inline Result unrolled(const std::uint8_t* p, UIntState& s) noexcept {
    const auto b = p[I];
    if constexpr (I == 9) {
        if (s.value > (UINT64_MAX >> 7) || (b & 0x80) == 0)
            return {Status::invalid, I + 1};
    }
    s.value = (s.value << 7) | (b & 0x7f);
    if (b & 0x80) {
        s.count = 0;
        return {Status::done, I + 1};
    }
    if constexpr (I < 9) return unrolled<I + 1>(p, s);
    return {Status::invalid, I + 1};
}
} // namespace detail

// s must be initialized, or retained from need_more; discard it after invalid.
// On done, s.value is the decoded value and s.count == 0 (ready for next field).
inline Result decode(std::span<const std::uint8_t> input,
                     UIntState& s) noexcept {
    if (input.empty()) return {Status::need_more, 0};
    std::size_t i = 0;
    if (s.count == 0) {
        const auto b = input[i++];
        s.value = b & 0x7f;
        if (b & 0x80) return {Status::done, 1}; // Single-byte fast path.
        if (b == 0) return {Status::invalid, 1}; // Redundant leading zero group.
        s.count = 1;
        if (input.size() == 1) return {Status::need_more, 1};
        const auto second = input[i++];
        s.value = (s.value << 7) | (second & 0x7f);
        if (second & 0x80) {
            s.count = 0;
            return {Status::done, 2};
        }
        s.count = 2;
        if (input.size() >= 10)
            return detail::unrolled<2>(input.data(), s);
    }
    // Short input or a field split across receives: resume without rescanning.
    while (i < input.size()) {
        const auto b = input[i++];
        if (s.count == 9 &&
            (s.value > (UINT64_MAX >> 7) || (b & 0x80) == 0))
            return {Status::invalid, i};
        s.value = (s.value << 7) | (b & 0x7f);
        ++s.count;
        if (b & 0x80) {
            s.count = 0;
            return {Status::done, i};
        }
    }
    return {Status::need_more, i};
}

// Teaching wire schema: exactly four mandatory unsigned fields, in this order.
// Not a real exchange message, nor the native memory layout of this struct.
struct Quote {
    std::uint64_t instrument_id;
    std::uint64_t sequence;
    std::uint64_t price_ticks;
    std::uint64_t quantity;
    bool operator==(const Quote&) const = default;
};

inline std::size_t encode_quote(const Quote& q,
                                std::span<std::uint8_t> out) noexcept {
    const std::array values{q.instrument_id, q.sequence, q.price_ticks, q.quantity};
    std::size_t size = 0;
    for (auto v : values) size += encoded_size(v);
    if (out.size() < size) return 0;
    auto* p = out.data();
    for (auto v : values) p += encode_unchecked(v, p);
    return size;
}

class QuoteStream {
    Quote current_{};
    UIntState integer_{};
    unsigned field_ = 0;
    bool failed_ = false;
public:
    bool at_message_boundary() const noexcept {
        return !failed_ && field_ == 0 && integer_.count == 0;
    }

    // Emit must synchronously accept the quote, must not throw or re-enter.
    // Copy it if needed later: current_ is reused for subsequent messages.
    // done means this input ended at a message boundary, not connection EOF.
    template<class Emit>
    Result feed(std::span<const std::uint8_t> input, Emit&& emit) noexcept {
        static_assert(std::is_nothrow_invocable_v<Emit&, const Quote&>);
        if (failed_) return {Status::invalid, 0};
        std::size_t used = 0;
        while (used < input.size()) {
            const auto r = decode(input.subspan(used), integer_);
            used += r.consumed;
            if (r.status == Status::invalid) {
                failed_ = true;
                return {Status::invalid, used};
            }
            if (r.status == Status::need_more) return {Status::need_more, used};
            switch (field_) {
                case 0: current_.instrument_id = integer_.value; break;
                case 1: current_.sequence = integer_.value; break;
                case 2: current_.price_ticks = integer_.value; break;
                case 3: current_.quantity = integer_.value; break;
            }
            if (++field_ == 4) {
                emit(std::as_const(current_));
                field_ = 0;
            }
        }
        return {at_message_boundary() ? Status::done : Status::need_more, used};
    }
};
} // namespace stopbit
