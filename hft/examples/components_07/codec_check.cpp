#include <array>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string_view>
#include <type_traits>
#include "event_v0/Event.h"
#include "event_v1/Event.h"

struct PodEvent {
    std::uint32_t instrumentId;
    std::uint64_t sourceSeq;
    std::int64_t price;
    std::int64_t quantity;
    std::uint64_t receivedNs;
};
static_assert(std::is_trivially_copyable_v<PodEvent>);

template<class Event>
std::size_t encode(std::array<char,256>& buffer) {
    Event e;
    e.wrapAndApplyHeader(buffer.data(), 0, buffer.size());
    e.instrumentId(42).sourceSeq(1001).price(1234500).quantity(3).receivedNs(999);
    if constexpr (Event::sbeSchemaVersion() == 1) e.flags(5);
    auto& levels = e.levelsCount(2);
    levels.next().price(1234500).quantity(3);
    levels.next().price(1234400).quantity(7);
    e.putSource("XNAS", 4);
    return 8 + e.encodedLength();
}

// This schema-specific envelope check runs before generated field accessors.
// It deliberately rejects unknown schema versions instead of guessing offsets.
void validate(const char* b, std::size_t n) {
    const auto u16 = [b,n](std::size_t p) -> std::size_t {
        if (p > n || n - p < 2) throw std::runtime_error("truncated u16");
        return static_cast<unsigned char>(b[p]) |
            (static_cast<std::size_t>(static_cast<unsigned char>(b[p+1])) << 8);
    };
    if (n < 8 || u16(2) != 1 || u16(4) != 7) throw std::runtime_error("bad header");
    const auto v = u16(6), block = u16(0);
    if (v > 1 || block != (v == 0 ? 36 : 40)) throw std::runtime_error("unsupported schema");
    auto p = 8 + block;
    if (p > n || n-p < 4) throw std::runtime_error("truncated group header");
    auto stride = u16(p), count = u16(p+2); p += 4;
    if (stride != 16 || count > (n-p)/stride) throw std::runtime_error("bad group bounds");
    p += count * stride;
    auto length = u16(p); p += 2;
    if (length > n-p || p + length != n) throw std::runtime_error("bad string bounds");
}

template<class Event>
void decode(std::array<char,256>& b, std::size_t n, std::uint64_t version, std::uint64_t block) {
    validate(b.data(), n);
    Event e;
    e.wrapForDecode(b.data(), 8, block, version, n);
    assert(e.instrumentId() == 42 && e.sourceSeq() == 1001 && e.price() == 1234500);
    if constexpr (Event::sbeSchemaVersion() == 1)
        assert(e.flags() == (version == 0 ? Event::flagsNullValue() : 5));
    auto& levels = e.levels();
    assert(levels.count() == 2);
    assert(levels.next().price() == 1234500);
    assert(levels.next().quantity() == 7);
    assert(e.getSourceAsString() == "XNAS");
}

int main() {
    std::array<char,256> v0{},v1{};
    auto n0=encode<event_v0::Event>(v0),n1=encode<event_v1::Event>(v1);
    assert(n0==86 && n1==90);
    decode<event_v0::Event>(v0,n0,0,36);
    decode<event_v1::Event>(v0,n0,0,36);
    decode<event_v0::Event>(v1,n1,1,40);
    decode<event_v1::Event>(v1,n1,1,40);
    for (std::size_t n=0;n<n1;++n) {
        bool rejected=false;
        try {validate(v1.data(),n);} catch(const std::runtime_error&) {rejected=true;}
        assert(rejected);
    }
    event_v1::Event wrong;
    wrong.wrapForDecode(v1.data(),8,40,1,n1);
    bool order_rejected=false;
    try {(void)wrong.getSourceAsString();} catch(const std::exception&) {order_rejected=true;}
    assert(order_rejected);
    event_v1::Event view;
    view.wrapForDecode(v1.data(),8,40,1,n1);
    auto& levels=view.levels(); levels.next();levels.next();
    auto source=view.getSourceAsStringView();
    assert(source=="XNAS");
    std::memcpy(v1.data()+n1-4,"TEST",4);
    assert(source=="TEST");
    std::memcpy(v1.data()+n1-4,"XNAS",4);
    std::cout << "POD size=" << sizeof(PodEvent) << " sourceSeq offset=" << offsetof(PodEvent,sourceSeq) << '\n';
    std::cout << "v0=" << n0 << " v1=" << n1 << " cross-decode=4/4 truncated-prefixes=90/90 order-check=pass view-alias=pass\n";
    std::cout << "v1 hex=";
    for(std::size_t i=0;i<n1;++i) std::cout << std::hex << std::setfill('0') << std::setw(2) << static_cast<unsigned>(static_cast<unsigned char>(v1[i]));
    std::cout << '\n';
}
