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
static struct cxd5610_ctx *stop_when_idle;
static uint8_t configured_notify[32];
static size_t configured_notify_count;
static uint8_t configured_satellites[2];
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
    {
      fake_ms += timeout;
      if (stop_when_idle)
        {
          stop_when_idle->closing = true;
          stop_when_idle->running = false;
        }
    }
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
  const uint8_t *request = source;
  if (request[3] == OPC_BINARY_OUTPUT_SET)
    {
      configured_notify_count = request[1];
      assert(configured_notify_count <= sizeof(configured_notify));
      memcpy(configured_notify, request + 5, configured_notify_count);
    }
  if (request[3] == OPC_GNSS_SAT_SETTING)
    memcpy(configured_satellites, request + 5, sizeof(configured_satellites));
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
  stop_when_idle = NULL;
  configured_notify_count = 0;
  stall_offset = 0;
  g_stop = 0;
  ready = level = already_high;
  memset(opcodes, 0, sizeof(opcodes));
  memset(bodies, 0, sizeof(bodies));
  for (int i = 0; i < 32; i++)
    lengths[i] = 1;
  struct cxd5610_ctx ctx = {.i2c_fd = 3, .qzss_dcr_enabled = true};
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

static void init_report_lock(struct cxd5610_ctx *ctx)
{
  assert(pthread_mutex_init(&ctx->lock, NULL) == 0);
  ctx->lock_initialized = true;
  ctx->session_state = CXD5610_READY;
  ctx->ready = ctx->dcr_applied_known = true;
}

static void position_notifications(void)
{
  struct cxd5610_ctx ctx = {0};
  reset_data(&ctx.last);
  struct cmd_notify_pos pos = {
    .ver8 = 0x81, .mode = 0x32,
    .lat32 = 355001234, .lon32 = 1395001234, .alt32 = 12345, .geo16 = 4000
  };
  /* Both legacy and extended positions expose the same altitude. */
  handle_notify(&ctx, OPC_RECEIVER_POS_NOTIFY, (const uint8_t *)&pos, 14);
  assert(fabs(ctx.last.lat - 35.5001234) < 1e-7);
  assert(fabs(ctx.last.lon - 139.5001234) < 1e-7);
  assert(fabs(ctx.last.alt - 123.45) < 1e-7 && ctx.last.status == 2);
  handle_notify(&ctx, OPC_RECEIVER_POS_NOTIFY, (const uint8_t *)&pos, sizeof(pos));
  assert(fabs(ctx.last.alt - 123.45) < 1e-7);

  struct cmd_notify_vel vel = {
    .ver8 = 0x80, .course16 = 1234, .mag_course16 = 2345,
    .vel16 = 360, .up_vel16 = 180
  };
  handle_notify(&ctx, OPC_RECEIVER_VEL_NOTIFY, (const uint8_t *)&vel, sizeof(vel));
  assert(fabs(ctx.last.speed - 10) < 1e-7);
  assert(fabs(ctx.last.track - 123.4) < 1e-7);

  struct __attribute__((packed)) {
    struct cmd_notify_sat header;
    struct cmd_notify_satinfo info[3];
  } sats = {
    .header = {.ver8 = 0x80, .mode = 3, .numsv = 3},
    .info = {{.signal = 0x21}, {.signal = 0xc1}, {.signal = 0xe1}}
  };
  handle_notify(&ctx, OPC_SAT_INFO_NOTIFY, (const uint8_t *)&sats, sizeof(sats));
  assert(ctx.last.used_sats == 2 && ctx.last.total_sats == 3 && ctx.last.mode == 3);

  struct cmd_notify_acc acc = {.ver8 = 0x80, .pdop8 = 13, .hdop8 = 9, .vdop8 = 17};
  handle_notify(&ctx, OPC_ACCURACY_IDX_NOTIFY, (const uint8_t *)&acc, sizeof(acc));
  assert(fabs(ctx.last.pdop - 1.3) < 1e-6);
  assert(fabs(ctx.last.hdop - 0.9) < 1e-6);
  assert(fabs(ctx.last.vdop - 1.7) < 1e-6);
}

static struct cmd_notify_dcreport report_frame(uint8_t count)
{
  struct cmd_notify_dcreport frame = {.ver8 = 0x80, .nr = count};
  for (size_t i = 0; i < 3; i++)
    {
      frame.msg[i].svid = 55 + i;
      for (size_t j = 0; j < CXD5610_DCREPORT_BYTES; j++)
        frame.msg[i].data[j] = (uint8_t)(i * 32 + j);
    }
  return frame;
}

static void dcreport_queue(void)
{
  struct cxd5610_ctx ctx = receiver(0, 0);
  init_report_lock(&ctx);
  struct cxd5610_dcreport reports[4];
  uint64_t dropped = 99;
  assert(cxd5610_read_dcreports(&ctx, reports, 4, &dropped) == 0);
  assert(dropped == 0);
  struct cmd_notify_dcreport frame = report_frame(1);
  fake_ms = 1234567;
  /* A single-message notification may omit the two unused wire slots. */
  handle_notify(&ctx, OPC_DISASTER_CRISIS_NOTIFY, (uint8_t *)&frame, 35);
  frame.nr = 3;
  handle_notify(&ctx, OPC_DISASTER_CRISIS_NOTIFY, (uint8_t *)&frame, sizeof(frame));
  memset(&frame, 0, sizeof(frame));
  assert(cxd5610_read_dcreports(&ctx, reports, 4, &dropped) == 4);
  assert(reports[0].satellite_id == 55);
  assert(reports[1].satellite_id == 55); /* Repeats are left to the application. */
  assert(reports[2].satellite_id == 56 && reports[3].satellite_id == 57);
  assert(reports[0].received_at.tv_sec == 1234);
  assert(reports[0].received_at.tv_nsec == 567000000);
  for (size_t i = 0; i < 32; i++)
    {
      assert(reports[0].data[i] == i);
      assert(reports[3].data[i] == 64 + i);
    }
  assert(cxd5610_read_dcreports(&ctx, reports, 4, NULL) == 0);
  assert(!ctx.data_valid && ctx.sample_seq == 0);
  pthread_mutex_destroy(&ctx.lock);
}

static void dcreport_invalid(void)
{
  struct cxd5610_ctx ctx = receiver(0, 0);
  init_report_lock(&ctx);
  struct cmd_notify_dcreport frame = report_frame(3);
  /* Reject a truncated batch as a whole, even if its first message is complete. */
  handle_dc_notify(&ctx, (uint8_t *)&frame, sizeof(frame) - 1);
  handle_dc_notify(&ctx, (uint8_t *)&frame, 1);
  frame.ver8 = 0;
  handle_dc_notify(&ctx, (uint8_t *)&frame, sizeof(frame));
  frame.ver8 = 0x80;
  frame.nr = 4;
  handle_dc_notify(&ctx, (uint8_t *)&frame, sizeof(frame));
  frame.nr = 0;
  handle_dc_notify(&ctx, (uint8_t *)&frame, sizeof(frame));
  /* Actual CXD5610 firmware sends a two-byte empty notification each epoch. */
  const uint8_t empty[] = {0x80, 0x00};
  handle_dc_notify(&ctx, empty, sizeof(empty));
  assert(ctx.dcreport_count == 0);
  frame.nr = 2;
  frame.msg[0].svid = 0;
  handle_dc_notify(&ctx, (uint8_t *)&frame, sizeof(frame));
  struct cxd5610_dcreport report;
  assert(cxd5610_read_dcreports(&ctx, &report, 1, NULL) == 1);
  assert(report.satellite_id == 56);
  assert(cxd5610_read_dcreports(NULL, &report, 1, NULL) == -EINVAL);
  assert(cxd5610_read_dcreports(&ctx, NULL, 1, NULL) == -EINVAL);
  pthread_mutex_destroy(&ctx.lock);
  ctx.lock_initialized = false;
  assert(cxd5610_read_dcreports(&ctx, &report, 1, NULL) == -ENOTSUP);
}

static void dcreport_overflow(void)
{
  struct cxd5610_ctx ctx = receiver(0, 0);
  init_report_lock(&ctx);
  struct cmd_notify_dcreport frame = report_frame(1);
  for (int i = 0; i < CXD5610_DCREPORT_QUEUE_SIZE + 3; i++)
    {
      frame.msg[0].data[0] = i;
      handle_dc_notify(&ctx, (uint8_t *)&frame, sizeof(frame));
    }
  uint64_t dropped = 0;
  assert(cxd5610_read_dcreports(&ctx, NULL, 0, &dropped) == 0);
  assert(dropped == 3 && ctx.dcreport_count == CXD5610_DCREPORT_QUEUE_SIZE);
  struct cxd5610_dcreport reports[CXD5610_DCREPORT_QUEUE_SIZE];
  assert(cxd5610_read_dcreports(&ctx, reports, 2, &dropped) == 2);
  assert(reports[0].data[0] == 3 && reports[1].data[0] == 4);
  assert(cxd5610_read_dcreports(&ctx, reports, CXD5610_DCREPORT_QUEUE_SIZE,
                               &dropped) == CXD5610_DCREPORT_QUEUE_SIZE - 2);
  for (int i = 0; i < CXD5610_DCREPORT_QUEUE_SIZE - 2; i++)
    assert(reports[i].data[0] == i + 5);
  handle_dc_notify(&ctx, (uint8_t *)&frame, sizeof(frame));
  assert(cxd5610_read_dcreports(&ctx, reports, 1, &dropped) == 1);
  assert(dropped == 3);
  pthread_mutex_destroy(&ctx.lock);
}

static void dcreport_during_command(void)
{
  struct cxd5610_ctx ctx = receiver(2, 0);
  init_report_lock(&ctx);
  struct cmd_notify_dcreport frame = report_frame(1);
  opcodes[0] = OPC_DISASTER_CRISIS_NOTIFY;
  lengths[0] = 35;
  memcpy(bodies[0], &frame, lengths[0]);
  bodies[0][lengths[0]] = cxd_checksum(bodies[0], lengths[0]);
  opcodes[1] = OPC_GNSS_STOP;
  assert(send_command(&ctx, OPC_GNSS_STOP, NULL, 0, 50) == 0);
  struct cxd5610_dcreport report;
  assert(cxd5610_read_dcreports(&ctx, &report, 1, NULL) == 1);
  assert(report.satellite_id == 55 && report.data[31] == 31);
  pthread_mutex_destroy(&ctx.lock);
}

static void dcreport_worker(void)
{
  struct cxd5610_ctx ctx = receiver(5, 0);
  init_report_lock(&ctx);
  assert(pthread_cond_init(&ctx.cond, NULL) == 0);
  ctx.running = true;
  opcodes[0] = OPC_GNSS_STOP;
  opcodes[1] = OPC_GNSS_SAT_SETTING;
  opcodes[2] = OPC_BINARY_OUTPUT_SET;
  opcodes[3] = OPC_GNSS_START;
  opcodes[4] = OPC_DISASTER_CRISIS_NOTIFY;
  struct cmd_notify_dcreport frame = report_frame(1);
  lengths[4] = 35;
  memcpy(bodies[4], &frame, lengths[4]);
  bodies[4][lengths[4]] = cxd_checksum(bodies[4], lengths[4]);
  stop_when_idle = &ctx;
  cxd5610_worker(&ctx);
  assert(configured_notify_count == 6);
  assert(configured_notify[5] == OPC_DISASTER_CRISIS_NOTIFY);
  assert(ctx.dcreport_count == 1);
  assert(!ctx.data_valid && ctx.sample_seq == 0);
  pthread_cond_destroy(&ctx.cond);
  pthread_mutex_destroy(&ctx.lock);
}

struct dcr_change {
  struct cxd5610_ctx *ctx;
  bool enabled;
  int result;
};

static void *request_dcr_change(void *argument)
{
  struct dcr_change *change = argument;
  change->result = cxd5610_set_qzss_dcr_enabled(change->ctx, change->enabled);
  return NULL;
}

static void run_dcr_change(struct cxd5610_ctx *ctx, bool enabled, int expected)
{
  struct dcr_change change = {.ctx = ctx, .enabled = enabled};
  pthread_t client;
  assert(pthread_create(&client, NULL, request_dcr_change, &change) == 0);
  pthread_mutex_lock(&ctx->lock);
  while (!ctx->dcr_request_pending)
    pthread_cond_wait(&ctx->cond, &ctx->lock);
  pthread_mutex_unlock(&ctx->lock);
  assert(cxd5610_set_qzss_dcr_enabled(ctx, enabled) == -EBUSY);
  apply_dcreport_request(ctx);
  assert(pthread_join(client, NULL) == 0);
  assert(change.result == expected);
}

static void init_dcr_change(struct cxd5610_ctx *ctx)
{
  init_report_lock(ctx);
  assert(pthread_cond_init(&ctx->cond, NULL) == 0);
  ctx->cond_initialized = true;
  ctx->running = ctx->worker_alive = true;
}

static void dcreport_runtime(void)
{
  struct cxd5610_ctx ctx = receiver(4, 0);
  init_dcr_change(&ctx);
  opcodes[0] = opcodes[2] = OPC_GNSS_SAT_SETTING;
  opcodes[1] = opcodes[3] = OPC_BINARY_OUTPUT_SET;
  struct cmd_notify_dcreport frame = report_frame(1);
  handle_dc_notify(&ctx, (uint8_t *)&frame, sizeof(frame));
  assert(ctx.dcreport_count == 1);
  ctx.dcreports_dropped = 7;
  run_dcr_change(&ctx, false, 0);
  assert(!ctx.qzss_dcr_enabled && ctx.dcreport_count == 0);
  assert(configured_satellites[0] == 0xcf && configured_satellites[1] == 0x3f);
  assert(configured_notify_count == 5 && configured_notify[4] == OPC_ACCURACY_IDX_NOTIFY);
  handle_dc_notify(&ctx, (uint8_t *)&frame, sizeof(frame));
  assert(ctx.dcreport_count == 0);
  run_dcr_change(&ctx, true, 0);
  assert(ctx.qzss_dcr_enabled && ctx.dcreport_count == 0);
  assert(configured_satellites[0] == 0xef && configured_satellites[1] == 0x3f);
  assert(configured_notify_count == 6 && configured_notify[5] == OPC_DISASTER_CRISIS_NOTIFY);
  assert(write_calls == 4 && ctx.dcreports_dropped == 7);
  handle_dc_notify(&ctx, (uint8_t *)&frame, sizeof(frame));
  assert(ctx.dcreport_count == 1);
  ctx.running = false;
  assert(cxd5610_set_qzss_dcr_enabled(&ctx, false) < 0);
  pthread_cond_destroy(&ctx.cond);
  pthread_mutex_destroy(&ctx.lock);
}

static void dcreport_change_failure(void)
{
  struct cxd5610_ctx ctx = receiver(4, 0);
  init_dcr_change(&ctx);
  opcodes[0] = opcodes[2] = OPC_GNSS_SAT_SETTING;
  opcodes[1] = opcodes[3] = OPC_BINARY_OUTPUT_SET;
  bodies[1][0] = bodies[1][1] = (uint8_t)-EINVAL;
  run_dcr_change(&ctx, false, -EINVAL);
  assert(ctx.qzss_dcr_enabled && ctx.running);
  assert(configured_satellites[0] == 0xef && configured_notify_count == 6);
  pthread_cond_destroy(&ctx.cond);
  pthread_mutex_destroy(&ctx.lock);
}

static void dcreport_change_timeout(void)
{
  struct cxd5610_ctx ctx = receiver(0, 0);
  init_dcr_change(&ctx);
  run_dcr_change(&ctx, false, CXD5610_SESSION_ERROR);
  assert(ctx.qzss_dcr_enabled && !ctx.running && !ctx.data_valid);
  struct cxd5610_session_status status;
  assert(cxd5610_get_session_status(&ctx, &status) == 0);
  assert(status.state == CXD5610_FAILED && status.qzss_dcr_applied == -1);
  assert(status.change_errno == ETIMEDOUT && status.restore_errno == ETIMEDOUT);
  struct cxd5610_dcreport report;
  assert(cxd5610_read_dcreports(&ctx, &report, 1, NULL) == CXD5610_SESSION_ERROR);
  assert(write_calls == 2); /* Failed change and failed restoration. */
  pthread_cond_destroy(&ctx.cond);
  pthread_mutex_destroy(&ctx.lock);
}

static void dcreport_disabled_session(void)
{
  struct cxd5610_ctx ctx = receiver(4, 0);
  init_report_lock(&ctx);
  ctx.qzss_dcr_enabled = false;
  opcodes[0] = OPC_GNSS_STOP;
  opcodes[1] = OPC_GNSS_SAT_SETTING;
  opcodes[2] = OPC_BINARY_OUTPUT_SET;
  opcodes[3] = OPC_GNSS_START;
  assert(configure_session(&ctx) == 0);
  assert(configured_satellites[0] == 0xcf && configured_notify_count == 5);
  assert(!ctx.qzss_dcr_enabled);
  pthread_mutex_destroy(&ctx.lock);
}

static void dcreport_change_notification(void)
{
  struct cxd5610_ctx ctx = receiver(3, 0);
  init_dcr_change(&ctx);
  ctx.qzss_dcr_enabled = false;
  opcodes[0] = OPC_GNSS_SAT_SETTING;
  opcodes[1] = OPC_DISASTER_CRISIS_NOTIFY;
  opcodes[2] = OPC_BINARY_OUTPUT_SET;
  struct cmd_notify_dcreport frame = report_frame(1);
  lengths[1] = 35;
  memcpy(bodies[1], &frame, lengths[1]);
  bodies[1][lengths[1]] = cxd_checksum(bodies[1], lengths[1]);
  run_dcr_change(&ctx, true, 0);
  assert(ctx.qzss_dcr_enabled && ctx.dcreport_count == 0);
  handle_dc_notify(&ctx, (uint8_t *)&frame, sizeof(frame));
  assert(ctx.dcreport_count == 1);
  pthread_cond_destroy(&ctx.cond);
  pthread_mutex_destroy(&ctx.lock);
}

static void dcreport_failed_worker(void)
{
  struct cxd5610_ctx ctx = receiver(1, 0);
  init_dcr_change(&ctx);
  opcodes[0] = OPC_GNSS_STOP;
  bodies[0][0] = bodies[0][1] = (uint8_t)-EINVAL;
  struct dcr_change change = {.ctx = &ctx, .enabled = false};
  pthread_t client;
  assert(pthread_create(&client, NULL, request_dcr_change, &change) == 0);
  pthread_mutex_lock(&ctx.lock);
  while (!ctx.dcr_request_pending)
    pthread_cond_wait(&ctx.cond, &ctx.lock);
  pthread_mutex_unlock(&ctx.lock);
  cxd5610_worker(&ctx);
  assert(pthread_join(client, NULL) == 0);
  assert(change.result == CXD5610_SESSION_ERROR && !ctx.dcr_request_pending);
  assert(!ctx.worker_alive && !ctx.running);
  pthread_cond_destroy(&ctx.cond);
  pthread_mutex_destroy(&ctx.lock);
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
    {"position-notifications", position_notifications},
    {"dcreport-queue", dcreport_queue}, {"dcreport-invalid", dcreport_invalid},
    {"dcreport-overflow", dcreport_overflow},
    {"dcreport-during-command", dcreport_during_command},
    {"dcreport-worker", dcreport_worker},
    {"dcreport-runtime", dcreport_runtime},
    {"dcreport-change-failure", dcreport_change_failure},
    {"dcreport-change-timeout", dcreport_change_timeout},
    {"dcreport-disabled-session", dcreport_disabled_session},
    {"dcreport-change-notification", dcreport_change_notification},
    {"dcreport-failed-worker", dcreport_failed_worker},
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
