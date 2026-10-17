/* SPDX-License-Identifier: Apache-2.0 */
/* Reproduce packet interruption and shutdown races without opening hardware. */
#define CXD5610_NO_MAIN 1
#define read simulated_read
#define write simulated_write
#define close simulated_close
#define poll simulated_poll
#define clock_gettime simulated_clock_gettime
#define gpiod_line_request_get_fd simulated_gpio_fd
#define gpiod_line_request_get_value simulated_gpio_value
#define gpiod_line_request_read_edge_events simulated_gpio_events
#ifndef CXD5610_TRANSPORT_SOURCE
#define CXD5610_TRANSPORT_SOURCE "../src/cxd56xx_gnss/cxd5610_rpi.c"
#endif
#include CXD5610_TRANSPORT_SOURCE
#include <assert.h>

static int response_count, response_index, part, offset, ready, pending, level;
static int read_calls, write_calls, stall_body, interrupt_body;
static int short_read, short_write, bad_header, io_ms, stall_offset;
static int64_t fake_ms;
static uint8_t opcodes[32], bodies[32][4096];
static uint16_t lengths[32];

int simulated_clock_gettime(clockid_t clock, struct timespec *value)
{
  (void)clock;
  value->tv_sec = fake_ms / 1000;
  value->tv_nsec = (fake_ms % 1000) * 1000000;
  return 0;
}

int simulated_gpio_fd(struct gpiod_line_request *request)
{
  (void)request;
  return 7;
}

enum gpiod_line_value simulated_gpio_value(struct gpiod_line_request *request,
                                          unsigned int line)
{
  (void)request;
  (void)line;
  return level ? GPIOD_LINE_VALUE_ACTIVE : GPIOD_LINE_VALUE_INACTIVE;
}

int simulated_gpio_events(struct gpiod_line_request *request,
                           struct gpiod_edge_event_buffer *buffer, size_t count)
{
  (void)request;
  (void)buffer;
  (void)count;
  assert(pending);
  pending = 0;
  return 1;
}

int simulated_poll(struct pollfd *fds, nfds_t count, int timeout)
{
  assert(count == 1 && fds[0].fd == 7);
  fds[0].revents = 0;
  if (pending)
    {
      fds[0].revents = POLLIN;
      return 1;
    }
  if (timeout != 0 && part && interrupt_body)
    {
      interrupt_body = 0;
      errno = EINTR;
      return -1;
    }
  if (timeout != 0 && part && stall_body && offset == stall_offset)
    {
      stall_body = 0;
      fake_ms += timeout;
      return 0;
    }
  if (timeout != 0 && response_index < response_count)
    {
      ready = pending = level = 1;
      fds[0].revents = POLLIN;
      return 1;
    }
  if (timeout > 0)
    fake_ms += timeout;
  return 0;
}

ssize_t simulated_read(int fd, void *destination, size_t size)
{
  (void)fd;
  assert(ready && response_index < response_count);
  uint8_t *data = destination;
  read_calls++;
  fake_ms += io_ms;
  if (short_read)
    {
      short_read = 0;
      memset(data, 0, size);
      return (ssize_t)size - 1;
    }
  if (!part)
    {
      assert(size == 5);
      uint16_t len = lengths[response_index];
      uint8_t header[] = {0x7f, len & 255, len >> 8, opcodes[response_index], 0};
      header[4] = cxd_checksum(header, 4);
      memcpy(data, header, sizeof(header));
      if (bad_header)
        data[0] = 0;
      part = 1;
      offset = 0;
    }
  else
    {
      int remaining = lengths[response_index] + 1 - offset;
      assert(size == (size_t)(remaining > 512 ? 512 : remaining));
      memcpy(data, bodies[response_index] + offset, size);
      offset += size;
      if (offset == lengths[response_index] + 1)
        {
          response_index++;
          part = 0;
          level = 0;
        }
    }
  ready = pending = 0;
  return (ssize_t)size;
}

ssize_t simulated_write(int fd, const void *source, size_t size)
{
  (void)fd;
  /* Reject a new request until the pending operand has been consumed. */
  assert(!part);
  assert(size >= 5 && ((const uint8_t *)source)[0] == 0x7f);
  write_calls++;
  if (short_write)
    {
      short_write = 0;
      return (ssize_t)size - 1;
    }
  return (ssize_t)size;
}

int simulated_close(int fd)
{
  (void)fd;
  return 0;
}

static struct cxd5610_ctx receiver(int count, int already_high)
{
  response_count = count;
  response_index = part = offset = pending = read_calls = write_calls = 0;
  stall_body = interrupt_body = short_read = short_write = bad_header = io_ms = 0;
  fake_ms = 0;
  stall_offset = 0;
  g_stop = 0;
  ready = level = already_high;
  memset(opcodes, 0, sizeof(opcodes));
  memset(bodies, 0, sizeof(bodies));
  for (int i = 0; i < 32; i++)
    lengths[i] = 1;
  struct cxd5610_ctx ctx = {.i2c_fd = 3};
  return ctx;
}

static void packet_timeout(void)
{
  struct cxd5610_ctx ctx = receiver(1, 1);
  opcodes[0] = OPC_GNSS_STOP;
  stall_body = 1;
  uint8_t opc, *payload;
  uint16_t size;
  assert(read_packet(&ctx, &opc, &payload, &size, 50, true) == -ETIMEDOUT);
  assert(read_calls == 1 && part == 1);
  assert(read_packet(&ctx, &opc, &payload, &size, 50, true) == 0);
  assert(opc == OPC_GNSS_STOP && size == 1 && payload[0] == 0);
}

static void packet_interrupt(void)
{
  struct cxd5610_ctx ctx = receiver(1, 1);
  opcodes[0] = OPC_GNSS_STOP;
  interrupt_body = 1;
  uint8_t opc, *payload;
  uint16_t size;
  assert(read_packet(&ctx, &opc, &payload, &size, 50, true) == -EINTR);
  assert(read_packet(&ctx, &opc, &payload, &size, 50, true) == 0);
  assert(opc == OPC_GNSS_STOP && read_calls == 2);
}

static void chunk_timeout(void)
{
  struct cxd5610_ctx ctx = receiver(1, 1);
  opcodes[0] = OPC_SAT_INFO_NOTIFY;
  lengths[0] = 900;
  for (int i = 0; i < 900; i++)
    bodies[0][i] = i % 251;
  bodies[0][900] = cxd_checksum(bodies[0], 900);
  stall_body = 1;
  stall_offset = 512;
  uint8_t opc, *payload;
  uint16_t size;
  assert(read_packet(&ctx, &opc, &payload, &size, 50, true) == -ETIMEDOUT);
  assert(offset == 512);
  assert(read_packet(&ctx, &opc, &payload, &size, 50, true) == 0);
  assert(size == 900 && memcmp(payload, bodies[0], 900) == 0);
}

static void matched_ack(void)
{
  struct cxd5610_ctx ctx = receiver(2, 1);
  opcodes[0] = OPC_GNSS_STOP;
  opcodes[1] = OPC_GNSS_SAT_SETTING;
  bodies[1][0] = bodies[1][1] = 0xea;
  uint8_t signals[] = {0xef, 0x3f};
  assert(send_command(&ctx, OPC_GNSS_SAT_SETTING, signals, 2, 50) == -EINVAL);
  assert(response_index == 2);
}

static void pending_before_stop(void)
{
  struct cxd5610_ctx ctx = receiver(2, 1);
  opcodes[0] = OPC_ACCURACY_IDX_NOTIFY;
  opcodes[1] = OPC_GNSS_STOP;
  stall_body = 1;
  uint8_t opc, *payload;
  uint16_t size;
  assert(read_packet(&ctx, &opc, &payload, &size, 50, true) == -ETIMEDOUT);
  assert(send_command(&ctx, OPC_GNSS_STOP, NULL, 0, 50) == 0);
  assert(response_index == 2 && write_calls == 1);
}

static void signal_cleanup(void)
{
  struct cxd5610_ctx ctx = receiver(1, 0);
  opcodes[0] = OPC_GNSS_STOP;
  g_stop = 1;
  assert(send_command(&ctx, OPC_GNSS_STOP, NULL, 0, 50) == 0);
  assert(write_calls == 1 && read_calls == 2);
}

static void command_deadline(void)
{
  struct cxd5610_ctx ctx = receiver(16, 0);
  for (int i = 0; i < 16; i++)
    opcodes[i] = OPC_TIME_NOTIFY;
  io_ms = 1;
  assert(send_command(&ctx, OPC_GNSS_STOP, NULL, 0, 5) == -ETIMEDOUT);
  assert(fake_ms <= 6 && read_calls <= 6);
}

static void oversized_packet(void)
{
  struct cxd5610_ctx ctx = receiver(2, 1);
  opcodes[0] = OPC_SAT_INFO_NOTIFY;
  lengths[0] = 2000;
  opcodes[1] = OPC_GNSS_STOP;
  uint8_t opc, *payload;
  uint16_t size;
  assert(read_packet(&ctx, &opc, &payload, &size, 50, true) == -EOVERFLOW);
  assert(read_packet(&ctx, &opc, &payload, &size, 50, true) == 0);
  assert(opc == OPC_GNSS_STOP);
}

static void invalid_header(void)
{
  struct cxd5610_ctx ctx = receiver(1, 1);
  bad_header = 1;
  uint8_t opc, *payload;
  uint16_t size;
  assert(read_packet(&ctx, &opc, &payload, &size, 50, true) == -EBADMSG);
  assert(read_calls == 1);
}

static void short_transactions(void)
{
  struct cxd5610_ctx ctx = receiver(1, 1);
  short_read = 1;
  uint8_t opc, *payload;
  uint16_t size;
  assert(read_packet(&ctx, &opc, &payload, &size, 50, true) == -EIO);
  assert(read_packet(&ctx, &opc, &payload, &size, 50, true) == -EIO);
  assert(read_calls == 1);
  receiver(0, 0);
  short_write = 1;
  uint8_t request[] = {0x7f, 0, 0, 0x31, 0xb0};
  assert(write_exact(3, request, sizeof(request)) == -EIO);
  assert(write_calls == 1);
}

static void partial_initialization(void)
{
  receiver(0, 0);
  struct cxd5610_ctx *ctx = calloc(1, sizeof(*ctx));
  assert(ctx);
  ctx->i2c_fd = 3;
  cxd5610_close(ctx);
  assert(write_calls == 0 && read_calls == 0);
}

int main(int argc, char **argv)
{
  struct { const char *name; void (*run)(void); } tests[] = {
    {"packet-timeout", packet_timeout}, {"packet-interrupt", packet_interrupt},
    {"chunk-timeout", chunk_timeout}, {"matched-ack", matched_ack},
    {"pending-before-stop", pending_before_stop}, {"signal-cleanup", signal_cleanup},
    {"command-deadline", command_deadline}, {"oversized-packet", oversized_packet},
    {"invalid-header", invalid_header}, {"short-transactions", short_transactions},
    {"partial-initialization", partial_initialization},
  };
  int count = 0;
  for (size_t i = 0; i < sizeof(tests) / sizeof(tests[0]); i++)
    if (argc == 1 || strcmp(argv[1], tests[i].name) == 0)
      {
        tests[i].run();
        printf("PASS %s\n", tests[i].name);
        count++;
      }
  assert(count);
  return 0;
}
