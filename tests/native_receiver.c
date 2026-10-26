/* SPDX-License-Identifier: Apache-2.0 */
/* Fake only hardware ownership and wire I/O; run the production worker and API. */
#define CXD5610_NO_MAIN 1
#define CXD5610_COMMAND_TIMEOUT_MS 400
#define read fixture_read
#define write fixture_write
#define poll fixture_poll
#define open fixture_open
#define ioctl fixture_ioctl
#define close fixture_close_fd
#define gpiod_line_request_get_fd fixture_gpio_fd
#define gpiod_line_request_get_value fixture_gpio_value
#define gpiod_line_request_read_edge_events fixture_gpio_events
#define cxd5610_create_with_dcr hardware_create_with_dcr
#define cxd5610_shutdown hardware_shutdown
#define cxd5610_close hardware_close
#include "../src/cxd56xx_gnss/cxd5610_rpi.c"
#undef cxd5610_create_with_dcr
#undef cxd5610_shutdown
#undef cxd5610_close
#include <assert.h>

struct fixture_packet {
  uint8_t opcode;
  uint8_t data[128];
  size_t length;
  bool corrupt;
};
static pthread_mutex_t fixture_lock = PTHREAD_MUTEX_INITIALIZER;
static struct fixture_packet packets[64];
static int head, count, mode, output_attempts, stops, starts, last_mask;
static bool body, edge, io_failed, active;

static void enqueue(uint8_t opcode, const void *data, size_t length, bool corrupt)
{
  assert(count < 64 && length < sizeof(packets[0].data));
  struct fixture_packet *packet = &packets[(head + count++) % 64];
  packet->opcode = opcode;
  packet->length = length;
  packet->corrupt = corrupt;
  if (length) memcpy(packet->data, data, length);
  edge = true;
}

int fixture_open(const char *path, int flags, ...)
{
  (void)path; (void)flags;
  return 3;
}
int fixture_ioctl(int fd, unsigned long request, ...)
{
  (void)fd; (void)request;
  return 0;
}
int fixture_close_fd(int fd) { (void)fd; return 0; }
int fixture_gpio_fd(struct gpiod_line_request *request) { (void)request; return 7; }
enum gpiod_line_value fixture_gpio_value(struct gpiod_line_request *request, unsigned line)
{
  (void)request; (void)line;
  pthread_mutex_lock(&fixture_lock);
  bool ready = count > 0 || io_failed;
  pthread_mutex_unlock(&fixture_lock);
  return ready ? GPIOD_LINE_VALUE_ACTIVE : GPIOD_LINE_VALUE_INACTIVE;
}
int fixture_gpio_events(struct gpiod_line_request *request,
                         struct gpiod_edge_event_buffer *buffer, size_t size)
{
  (void)request; (void)buffer; (void)size;
  pthread_mutex_lock(&fixture_lock);
  edge = false;
  pthread_mutex_unlock(&fixture_lock);
  return 1;
}
int fixture_poll(struct pollfd *fds, nfds_t number, int timeout)
{
  (void)number;
  int64_t deadline = monotonic_ms() + timeout;
  for (;;)
    {
      pthread_mutex_lock(&fixture_lock);
      bool ready = edge;
      pthread_mutex_unlock(&fixture_lock);
      fds[0].revents = ready ? POLLIN : 0;
      if (ready) return 1;
      if (timeout == 0 || monotonic_ms() >= deadline) return 0;
      usleep(1000);
    }
}
ssize_t fixture_read(int fd, void *destination, size_t size)
{
  (void)fd;
  pthread_mutex_lock(&fixture_lock);
  if (io_failed)
    {
      pthread_mutex_unlock(&fixture_lock);
      errno = EIO;
      return -1;
    }
  assert(count > 0);
  struct fixture_packet *packet = &packets[head];
  if (!body)
    {
      assert(size == 5);
      uint8_t header[] = {0x7f, packet->length, 0, packet->opcode, 0};
      header[4] = cxd_checksum(header, 4);
      memcpy(destination, header, size);
      body = true;
    }
  else
    {
      assert(size == packet->length + 1);
      memcpy(destination, packet->data, packet->length);
      ((uint8_t *)destination)[packet->length] = cxd_checksum(packet->data, packet->length);
      if (packet->corrupt) ((uint8_t *)destination)[packet->length] ^= 1;
      body = false;
    }
  if (!body) { head = (head + 1) % 64; count--; }
  edge = body || count > 0;
  pthread_mutex_unlock(&fixture_lock);
  return (ssize_t)size;
}
ssize_t fixture_write(int fd, const void *source, size_t size)
{
  (void)fd;
  const uint8_t *request = source;
  uint8_t opcode = request[3];
  pthread_mutex_lock(&fixture_lock);
  bool slow = mode == 6 && opcode == OPC_GNSS_SAT_SETTING;
  if (slow) mode = 0;
  pthread_mutex_unlock(&fixture_lock);
  if (slow) usleep(150000);
  pthread_mutex_lock(&fixture_lock);
  if (opcode == OPC_GNSS_STOP) stops++;
  if (opcode == OPC_GNSS_START) starts++;
  if (opcode == OPC_GNSS_SAT_SETTING) last_mask = request[5];
  bool drop = (mode == 3 && opcode == OPC_GNSS_SAT_SETTING) ||
              (mode == 4 && opcode == OPC_BINARY_OUTPUT_SET) ||
              (mode == 5 && opcode == OPC_GNSS_START);
  int8_t ack = 0;
  if ((mode == 1 || mode == 2 || mode == 7) && opcode == OPC_BINARY_OUTPUT_SET)
    {
      output_attempts++;
      if (output_attempts == 1) ack = -EINVAL;
      else if (mode == 2) ack = -EIO;
      else if (mode == 7) ack = -EINVAL;
    }
  if (!drop) enqueue(opcode, &ack, 1, false);
  pthread_mutex_unlock(&fixture_lock);
  return (ssize_t)size;
}

#define FIXTURE_API __attribute__((visibility("default")))
FIXTURE_API void cxd5610_test_mode(int value)
{
  pthread_mutex_lock(&fixture_lock);
  mode = value;
  output_attempts = 0;
  pthread_mutex_unlock(&fixture_lock);
}
FIXTURE_API int cxd5610_test_starts(void)
{
  pthread_mutex_lock(&fixture_lock);
  int result = starts;
  pthread_mutex_unlock(&fixture_lock);
  return result;
}
FIXTURE_API int cxd5610_test_mask(void)
{
  pthread_mutex_lock(&fixture_lock);
  int result = last_mask;
  pthread_mutex_unlock(&fixture_lock);
  return result;
}
FIXTURE_API void cxd5610_test_corrupt(void)
{
  pthread_mutex_lock(&fixture_lock);
  const uint8_t payload = 0;
  for (int i = 0; i < 6; i++) enqueue(OPC_ACCURACY_IDX_NOTIFY, &payload, 1, true);
  pthread_mutex_unlock(&fixture_lock);
}
FIXTURE_API void cxd5610_test_io_failure(void)
{
  pthread_mutex_lock(&fixture_lock);
  io_failed = edge = true;
  pthread_mutex_unlock(&fixture_lock);
}
FIXTURE_API void cxd5610_test_report(void)
{
  struct cmd_notify_dcreport report = {.ver8 = 0x80, .nr = 1};
  report.msg[0].svid = 55;
  for (int i = 0; i < 32; i++) report.msg[0].data[i] = i;
  pthread_mutex_lock(&fixture_lock);
  enqueue(OPC_DISASTER_CRISIS_NOTIFY, &report, 35, false);
  pthread_mutex_unlock(&fixture_lock);
}

FIXTURE_API void cxd5610_test_payload(const uint8_t *data, size_t length)
{
  pthread_mutex_lock(&fixture_lock);
  enqueue(OPC_DISASTER_CRISIS_NOTIFY, data, length, false);
  pthread_mutex_unlock(&fixture_lock);
}

int cxd5610_create_with_dcr(struct cxd5610_ctx **out, bool enabled)
{
  pthread_mutex_lock(&fixture_lock);
  if (active) { pthread_mutex_unlock(&fixture_lock); return -EBUSY; }
  active = true;
  head = count = mode = output_attempts = stops = starts = last_mask = 0;
  body = edge = io_failed = false;
  const char *initial = getenv("CXD56XX_TEST_MODE");
  if (initial) mode = atoi(initial);
  pthread_mutex_unlock(&fixture_lock);
  struct cxd5610_ctx *ctx = calloc(1, sizeof(*ctx));
  assert(ctx && init_sync(ctx) == 0);
  ctx->i2c_fd = 3;
  ctx->qzss_dcr_enabled = enabled;
  ctx->running = ctx->worker_alive = true;
  reset_data(&ctx->last);
  assert(pthread_create(&ctx->thread, NULL, cxd5610_worker, ctx) == 0);
  ctx->thread_started = true;
  *out = ctx;
  return 0;
}
int cxd5610_shutdown(struct cxd5610_ctx *ctx)
{
  if (!ctx) return 0;
  cxd5610_join_and_destroy_sync(ctx);
  int ret = send_command(ctx, OPC_GNSS_STOP, NULL, 0, 200);
  free(ctx);
  pthread_mutex_lock(&fixture_lock);
  active = false;
  pthread_mutex_unlock(&fixture_lock);
  return ret;
}
void cxd5610_close(struct cxd5610_ctx *ctx) { cxd5610_shutdown(ctx); }
