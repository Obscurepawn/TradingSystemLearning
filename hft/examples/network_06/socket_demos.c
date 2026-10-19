#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <linux/errqueue.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>

static long check(long rc) {
    if (rc < 0) { perror("socket demo"); exit(EXIT_FAILURE); }
    return rc;
}

static void require(bool condition, const char *message) {
    if (!condition) { fprintf(stderr, "%s\n", message); exit(EXIT_FAILURE); }
}

/* 回环地址与系统分配端口，不依赖外部服务。 */
static int bound_socket(int type, struct sockaddr_in *address) {
    int fd = (int)check(socket(AF_INET, type, 0));
    *address = (struct sockaddr_in){.sin_family = AF_INET,
        .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    check(bind(fd, (struct sockaddr *)address, sizeof(*address)));
    socklen_t length = sizeof(*address);
    check(getsockname(fd, (struct sockaddr *)address, &length));
    return fd;
}

static int udp_loopback(void) {
    struct sockaddr_in address;
    int fd = bound_socket(SOCK_DGRAM, &address);
    check(connect(fd, (struct sockaddr *)&address, sizeof(address)));
    return fd;
}

static void tcp_pair(int pair[2]) {
    struct sockaddr_in address;
    int listener = bound_socket(SOCK_STREAM, &address);
    check(listen(listener, 1));
    pair[0] = (int)check(socket(AF_INET, SOCK_STREAM, 0));
    check(connect(pair[0], (struct sockaddr *)&address, sizeof(address)));
    pair[1] = (int)check(accept(listener, NULL, NULL));
    close(listener);
}

/* 仅用于这组小型阻塞 TCP 示例，处理短写和信号中断。 */
static void send_all(int fd, const char *data, size_t size, int flags) {
    while (size) {
        ssize_t n = send(fd, data, size, flags | MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR) continue;
        check(n);
        require(n > 0, "send made no progress");
        data += n;
        size -= (size_t)n;
    }
}

static void expect_tcp(int fd, const char *expected) {
    char buf[64];
    size_t size = strlen(expected), received = 0;
    while (received < size) {
        ssize_t n = recv(fd, buf + received, size - received, 0);
        if (n < 0 && errno == EINTR) continue;
        check(n);
        require(n > 0, "unexpected TCP EOF");
        received += (size_t)n;
    }
    require(memcmp(buf, expected, size) == 0, "unexpected TCP bytes");
}

static int get_int_option(int fd, int level, int name) {
    int value;
    socklen_t size = sizeof(value);
    check(getsockopt(fd, level, name, &value, &size));
    return value;
}

static void tcp_options(void) {
    int pair[2];
    tcp_pair(pair);
    int fd = pair[0], one = 1, zero = 0;
    check(setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one)));
    require(get_int_option(fd, IPPROTO_TCP, TCP_NODELAY) == 1, "NODELAY unset");
    send_all(fd, "BUY\n", 4, 0);
    expect_tcp(pair[1], "BUY\n");
    puts("TCP_NODELAY: enabled, received BUY");

    send_all(fd, "PRICE=", 6, MSG_MORE);
    send_all(fd, "123\n", 4, 0);
    expect_tcp(pair[1], "PRICE=123\n");
    puts("MSG_MORE: received PRICE=123");

    check(setsockopt(fd, IPPROTO_TCP, TCP_CORK, &one, sizeof(one)));
    send_all(fd, "CORK\n", 5, 0);
    check(setsockopt(fd, IPPROTO_TCP, TCP_CORK, &zero, sizeof(zero)));
    require(get_int_option(fd, IPPROTO_TCP, TCP_CORK) == 0, "CORK still set");
    expect_tcp(pair[1], "CORK\n");
    puts("TCP_CORK: cleared, received CORK");
    close(pair[0]); close(pair[1]);
}

static void busy_poll(void) {
    int fd = udp_loopback(), us = 50;
    if (setsockopt(fd, SOL_SOCKET, SO_BUSY_POLL, &us, sizeof(us)) < 0) {
        fprintf(stderr, "SO_BUSY_POLL unavailable: %s\n", strerror(errno));
    } else {
        printf("SO_BUSY_POLL: configured %d us; loopback does not test NIC polling\n",
               get_int_option(fd, SOL_SOCKET, SO_BUSY_POLL));
    }
    close(fd);
}

static void gather_and_batch(void) {
    int fd = udp_loopback();
    char header[] = "PX=", body[] = "123";
    struct iovec parts[] = {{header, sizeof(header) - 1}, {body, sizeof(body) - 1}};
    struct msghdr out = {.msg_iov = parts, .msg_iovlen = 2};
    require(check(sendmsg(fd, &out, 0)) == 6, "unexpected UDP send size");
    check(send(fd, "Q=2", 3, 0));

    char buf[4][128];
    struct iovec iov[4];
    struct mmsghdr msgs[4] = {0};
    for (int i = 0; i < 4; ++i) {
        iov[i] = (struct iovec){buf[i], sizeof(buf[i])};
        msgs[i].msg_hdr = (struct msghdr){.msg_iov = &iov[i], .msg_iovlen = 1};
    }
    int n = (int)check(recvmmsg(fd, msgs, 4, MSG_DONTWAIT, NULL));
    require(n == 2, "expected the two queued datagrams, not a full batch of four");
    for (int i = 0; i < n; ++i) {
        require(!(msgs[i].msg_hdr.msg_flags & MSG_TRUNC), "UDP message truncated");
        printf("recvmmsg[%d]: %.*s\n", i, (int)msgs[i].msg_len, buf[i]);
    }
    require(msgs[0].msg_len == 6 && memcmp(buf[0], "PX=123", 6) == 0,
            "sendmsg did not preserve gathered bytes");
    require(msgs[1].msg_len == 3 && memcmp(buf[1], "Q=2", 3) == 0,
            "second UDP payload mismatch");
    errno = 0;
    require(recvmmsg(fd, msgs, 4, MSG_DONTWAIT, NULL) == -1 && errno == EAGAIN,
            "empty nonblocking receive should return EAGAIN");
    close(fd);
}

static void reuse_receive_buffer(void) {
    int fd = udp_loopback(), bytes = 256 * 1024;
    check(setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &bytes, sizeof(bytes)));
    printf("SO_RCVBUF: requested %d, reported %d (includes kernel accounting)\n",
           bytes, get_int_option(fd, SOL_SOCKET, SO_RCVBUF));
    check(send(fd, "first", 5, 0));
    check(send(fd, "second", 6, 0));
    char buf[2048];
    for (int i = 0; i < 2; ++i) {
        ssize_t n = check(recv(fd, buf, sizeof(buf), 0));
        printf("reused buffer: %.*s\n", (int)n, buf);
    }
    close(fd);
}

enum { CAP = 8 };
struct Order { int price; };
struct Queue { struct Order items[CAP]; unsigned head, tail, count; };

/* 单线程队列：满时拒绝新提交，由调用方决定重试或放弃。 */
static bool try_push(struct Queue *q, struct Order order) {
    if (q->count == CAP) return false;
    q->items[q->tail] = order;
    q->tail = (q->tail + 1) % CAP;
    ++q->count;
    return true;
}

static void bounded_queue(void) {
    struct Queue q = {0};
    for (int i = 0; i < CAP; ++i)
        require(try_push(&q, (struct Order){.price = i}), "queue filled too soon");
    require(!try_push(&q, (struct Order){.price = 99}), "queue exceeded capacity");
    require(q.items[q.head].price == 0, "full queue overwrote an older order");
    q.head = (q.head + 1) % CAP; --q.count;
    require(try_push(&q, (struct Order){.price = 8}), "queue slot was not reusable");
    puts("bounded queue: rejected order 9; reclaimed slot reused");
}

/* 新建 IPv4 socket 上唯一一次 MSG_ZEROCOPY 发送的完成编号为 0。 */
static void wait_zerocopy_0(int fd) {
    struct pollfd p = {.fd = fd, .events = POLLERR};
    require(check(poll(&p, 1, 1000)) == 1, "zerocopy notification timed out");
    union { struct cmsghdr align; char bytes[128]; } control;
    struct msghdr msg = {.msg_control = control.bytes,
                        .msg_controllen = sizeof(control.bytes)};
    check(recvmsg(fd, &msg, MSG_ERRQUEUE));
    require(!(msg.msg_flags & MSG_CTRUNC), "completion control data truncated");
    for (struct cmsghdr *c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg, c)) {
        if (c->cmsg_level != SOL_IP || c->cmsg_type != IP_RECVERR ||
            c->cmsg_len < CMSG_LEN(sizeof(struct sock_extended_err))) continue;
        struct sock_extended_err e;
        memcpy(&e, CMSG_DATA(c), sizeof(e));
        if (e.ee_origin != SO_EE_ORIGIN_ZEROCOPY || e.ee_errno != 0) continue;
        require(e.ee_info == 0 && e.ee_data == 0, "unexpected completion IDs");
        printf("MSG_ZEROCOPY: completion 0, copied=%d\n",
               e.ee_code == SO_EE_CODE_ZEROCOPY_COPIED);
        return;
    }
    require(false, "no matching zerocopy completion");
}

static void zerocopy(void) {
    int fd = udp_loopback(), one = 1;
    check(setsockopt(fd, SOL_SOCKET, SO_ZEROCOPY, &one, sizeof(one)));
    char buf[32768] = {0}, received[32768];
    require(check(send(fd, buf, sizeof(buf), MSG_ZEROCOPY)) == sizeof(buf),
            "unexpected UDP send size");
    require(check(recv(fd, received, sizeof(received), 0)) == sizeof(buf),
            "unexpected UDP receive size");
    require(memcmp(buf, received, sizeof(buf)) == 0, "zerocopy payload mismatch");
    wait_zerocopy_0(fd);
    memset(buf, 'X', sizeof(buf)); /* 完成通知到达后才复用。 */
    close(fd);
}

int main(void) {
    alarm(10);
    tcp_options();
    busy_poll();
    gather_and_batch();
    reuse_receive_buffer();
    bounded_queue();
    zerocopy();
    puts("Socket demos completed; no latency measurement was performed.");
    return 0;
}
