/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Extracted from Pi Zero Bikecomputer on 2026-09-06.
 * Modified for standalone packaging and overridable hardware build settings.
 * See LICENSE, NOTICE and THIRD_PARTY.md.
 * The following upstream notice applies to the Apache NuttX portions.
 */
/****************************************************************************
 * boards/arm/cxd56xx/drivers/sensors/cxd5610_gnss.c
 *
 * Original upstream license: Apache-2.0
 *
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.  The
 * ASF licenses this file to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance with the
 * License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
 * WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
 * License for the specific language governing permissions and limitations
 * under the License.
 *
 ****************************************************************************/

#define _GNU_SOURCE

#include "cxd5610_rpi.h"

/*
 * Minimal Linux userspace port of the Sony CXD5610 GNSS I2C protocol.
 *
 * This implementation is informed by and includes portions adapted from
 * the Apache NuttX RTOS (Apache License 2.0), specifically the Sony CXD56xx
 * GNSS driver/protocol handling. See NOTICE for attribution and details.
 *
 * To mirror the structure of BHI3 (bhi3_s.c), this file is organized into:
 *   1) Wire-format helpers (checksum, endian, I/O)
 *   2) Notify parsers
 *   3) Session management (init/recover)
 *   4) Worker thread (interrupt wait + receive loop)
 *   5) Public API (create/read/close)
 */

struct cxd5610_ctx {
  int i2c_fd;
  struct gpiod_chip *chip;
  struct gpiod_line_request *irq_req;
  struct gpiod_edge_event_buffer *evbuf;
  uint8_t sndbuf[64];
  uint8_t rcvbuf[64];
  uint8_t notifybuf[NOTIFY_MAX_BUF];
  struct cxd5610_data last;
  struct cxd5610_dcreport dcreports[CXD5610_DCREPORT_QUEUE_SIZE];
  size_t dcreport_head;
  size_t dcreport_count;
  uint64_t dcreports_dropped;
  bool qzss_dcr_enabled;
  bool dcr_applied_known;
  enum cxd5610_session_state session_state;
  int session_errno;
  int change_errno;
  int restore_errno;
  enum cxd5610_error_stage error_stage;
  bool dcr_request_pending;
  bool dcr_request_done;
  bool dcr_request_enabled;
  int dcr_request_result;
  bool worker_alive;
  int err_streak;
  pthread_t thread;
  pthread_mutex_t lock;
  pthread_cond_t cond;
  bool lock_initialized;
  bool cond_initialized;
  bool running;
  bool closing;
  bool thread_started;
  bool irq_wait_started;
  bool irq_part_ready;
  bool rx_pending;
  bool rx_discard;
  uint8_t rx_opcode;
  uint8_t rx_checksum;
  uint16_t rx_length;
  size_t rx_offset;
  int transport_error;
  bool ready;
  bool data_valid;
  int last_opc;
  uint64_t sample_seq;
};

static int64_t monotonic_ms(void);

static int cond_wait_ms(pthread_cond_t *cond,
                        pthread_mutex_t *lock,
                        int timeout_ms)
{
  if (timeout_ms < 0)
    {
      pthread_cond_wait(cond, lock);
      return 0;
    }

  if (timeout_ms == 0)
    {
      return -ETIMEDOUT;
    }

  struct timespec ts;
#ifdef __APPLE__
  ts.tv_sec = 0;
  ts.tv_nsec = 0;
#else
  clock_gettime(CLOCK_MONOTONIC, &ts);
#endif
  ts.tv_sec += timeout_ms / 1000;
  ts.tv_nsec += (timeout_ms % 1000) * 1000000L;
  if (ts.tv_nsec >= 1000000000L)
    {
      ts.tv_sec += 1;
      ts.tv_nsec -= 1000000000L;
    }

#ifdef __APPLE__
  int wret = pthread_cond_timedwait_relative_np(cond, lock, &ts);
#else
  int wret = pthread_cond_timedwait(cond, lock, &ts);
#endif
  return -wret;
}

static int init_sync(struct cxd5610_ctx *ctx)
{
  int ret = pthread_mutex_init(&ctx->lock, NULL);
  if (ret != 0)
    return -ret;
  ctx->lock_initialized = true;
  pthread_condattr_t attr;
  ret = pthread_condattr_init(&attr);
  if (ret != 0)
    return -ret;
#ifndef __APPLE__
  ret = pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
#endif
  if (ret == 0)
    ret = pthread_cond_init(&ctx->cond, &attr);
  pthread_condattr_destroy(&attr);
  if (ret == 0)
    ctx->cond_initialized = true;
  return -ret;
}

static void cxd5610_join_and_destroy_sync(struct cxd5610_ctx *ctx)
{
  if (!ctx)
    {
      return;
    }

  if (ctx->lock_initialized)
    {
      pthread_mutex_lock(&ctx->lock);
      if (ctx->thread_started)
        {
          ctx->closing = true;
          ctx->running = false;
          pthread_cond_broadcast(&ctx->cond);
          pthread_mutex_unlock(&ctx->lock);
          pthread_join(ctx->thread, NULL);
          pthread_mutex_lock(&ctx->lock);
          ctx->thread_started = false;
        }
      pthread_mutex_unlock(&ctx->lock);
    }

  if (ctx->cond_initialized)
    {
      pthread_cond_destroy(&ctx->cond);
      ctx->cond_initialized = false;
    }
  if (ctx->lock_initialized)
    {
      pthread_mutex_destroy(&ctx->lock);
      ctx->lock_initialized = false;
    }
}

/* -------------------------------------------------------------------------- */
/* 1. Wire-format helpers                                                     */
/* -------------------------------------------------------------------------- */

/* Packed wire structures (little-endian fields) */
struct __attribute__((packed)) cmd_notify_time {
  uint8_t ver8;
  uint8_t type;
  uint8_t yearl;
  uint8_t yearu_mon;
  uint8_t day;
  uint8_t hour;
  uint8_t min;
  uint8_t sec;
  uint8_t sec100;
};

struct __attribute__((packed)) cmd_notify_pos {
  uint8_t  ver8;
  uint8_t  mode;
  int32_t  lat32;
  int32_t  lon32;
  int32_t  alt32;
  int16_t  geo16;
};

struct __attribute__((packed)) cmd_notify_vel {
  uint8_t  ver8;
  uint8_t  mode;
  uint16_t course16;
  uint16_t mag_course16;
  int16_t  vel16;
  int16_t  up_vel16;
};

struct __attribute__((packed)) cmd_notify_sat {
  uint8_t  ver8;
  uint8_t  mode;
  uint16_t numsv;
};

struct __attribute__((packed)) cmd_notify_satinfo {
  uint8_t  signal;
  uint8_t  svid;
  uint8_t  cn;
  uint8_t  elevation;
  uint16_t azimuth;
};

struct __attribute__((packed)) cmd_notify_acc {
  uint8_t  ver8;
  uint16_t h_uc16;
  uint16_t v_uc16;
  uint16_t h_speed_uc16;
  uint16_t v_speed_uc16;
  uint8_t  pdop8;
  uint8_t  hdop8;
  uint8_t  vdop8;
  uint16_t semimajor16;
  uint16_t semiminor16;
  uint8_t  orientation;
};

struct __attribute__((packed)) mt43_data {
  uint8_t svid;
  uint8_t data[CXD5610_DCREPORT_BYTES];
};

struct __attribute__((packed)) cmd_notify_dcreport {
  uint8_t ver8;
  uint8_t nr;
  struct mt43_data msg[3];
};

static volatile sig_atomic_t g_stop = 0;

static void reset_data(struct cxd5610_data *d)
{
  if (!d)
    {
      return;
    }
  d->lat = NAN;
  d->lon = NAN;
  d->alt = NAN;
  d->speed = NAN;
  d->track = NAN;
  d->mode = -1;
  d->status = -1;
  d->pdop = NAN;
  d->hdop = NAN;
  d->vdop = NAN;
  d->used_sats = -1;
  d->total_sats = -1;
  d->timestamp.tv_sec = 0;
  d->timestamp.tv_nsec = 0;
}

/* The caller holds lock when synchronization has been initialized. */
static void reset_session_data(struct cxd5610_ctx *ctx)
{
  ctx->ready = false;
  ctx->data_valid = false;
  ctx->dcr_applied_known = false;
  ctx->dcreport_head = ctx->dcreport_count = 0;
  reset_data(&ctx->last);
}

/* The caller holds lock. A terminal failure is sticky until close/recreation. */
static void fail_session_locked(struct cxd5610_ctx *ctx, int error,
                                enum cxd5610_error_stage stage)
{
  if (ctx->session_state == CXD5610_FAILED)
    return;
  ctx->session_state = CXD5610_FAILED;
  ctx->session_errno = error < 0 ? -error : EIO;
  ctx->error_stage = stage;
  ctx->running = false;
  reset_session_data(ctx);
  if (ctx->cond_initialized)
    pthread_cond_broadcast(&ctx->cond);
}

static int fail_session(struct cxd5610_ctx *ctx, int error,
                        enum cxd5610_error_stage stage)
{
  if (ctx->lock_initialized)
    pthread_mutex_lock(&ctx->lock);
  fail_session_locked(ctx, error, stage);
  if (ctx->lock_initialized)
    pthread_mutex_unlock(&ctx->lock);
  return CXD5610_SESSION_ERROR;
}

static void handle_sigint(int sig)
{
  (void)sig;
  g_stop = 1;
}

/* Small helpers */
static uint8_t cxd_checksum(const uint8_t *data, uint16_t len)
{
  uint8_t s = 0;
  for (uint16_t i = 0; i < len; i++)
    {
      s += data[i];
    }
  return s;
}

static uint16_t le16(const void *p)
{
  const uint8_t *b = (const uint8_t *)p;
  return (uint16_t)b[0] | ((uint16_t)b[1] << 8);
}

static int32_t le32(const void *p)
{
  const uint8_t *b = (const uint8_t *)p;
  return (int32_t)(b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24));
}

static int read_exact(int fd, uint8_t *buf, size_t len)
{
  ssize_t n;
  do
    {
      n = read(fd, buf, len);
    }
  while (n < 0 && errno == EINTR);
  /* Each I2C syscall is a transaction, not a resumable byte stream. */
  return n < 0 ? -errno : (size_t)n == len ? 0 : -EIO;
}

static int write_exact(int fd, const uint8_t *buf, size_t len)
{
  ssize_t n;
  do
    {
      n = write(fd, buf, len);
    }
  while (n < 0 && errno == EINTR);
  return n < 0 ? -errno : (size_t)n == len ? 0 : -EIO;
}

static int discard_irq_events(struct cxd5610_ctx *ctx)
{
  struct pollfd pfd = {
    .fd = gpiod_line_request_get_fd(ctx->irq_req),
    .events = POLLIN
  };
  int ret;
  while ((ret = poll(&pfd, 1, 0)) > 0)
    {
      if (gpiod_line_request_read_edge_events(ctx->irq_req, ctx->evbuf, 16) <= 0)
        {
          return -EIO;
        }
    }
  return ret < 0 ? -errno : 0;
}

static int wait_irq(struct cxd5610_ctx *ctx, int timeout_ms)
{
  if (ctx->irq_part_ready)
    return 0;

  /* Only the initial transfer may use a level asserted before line ownership. */
  if (!ctx->irq_wait_started)
    {
      int ret = discard_irq_events(ctx);
      if (ret < 0)
        return ret;
      int value = gpiod_line_request_get_value(ctx->irq_req, CXD5610_INT_GPIO);
      if (value < 0)
        return -errno;
      ctx->irq_wait_started = true;
      if (value == GPIOD_LINE_VALUE_ACTIVE)
        {
          ctx->irq_part_ready = true;
          return 0;
        }
    }

  struct pollfd pfd = {
    .fd = gpiod_line_request_get_fd(ctx->irq_req),
    .events = POLLIN
  };
  int pret = poll(&pfd, 1, timeout_ms);
  if (pret == 0)
    return -ETIMEDOUT;
  if (pret < 0)
    return -errno;
  if (!(pfd.revents & POLLIN))
    return -EIO;
  int n = gpiod_line_request_read_edge_events(ctx->irq_req, ctx->evbuf, 16);
  if (n <= 0)
    return n < 0 ? -errno : -EIO;
  ctx->irq_part_ready = true;
  return 0;
}

static int read_part(struct cxd5610_ctx *ctx, uint8_t *buf, size_t size,
                     int timeout_ms)
{
  int ret = wait_irq(ctx, timeout_ms);
  if (ret < 0)
    return ret;
  /* Consume old edges before the read can prepare the next part. */
  ret = discard_irq_events(ctx);
  if (ret < 0)
    return ret;
  ctx->irq_part_ready = false;
  ret = read_exact(ctx->i2c_fd, buf, size);
  if (ret < 0)
    ctx->transport_error = ret;
  return ret;
}

static int read_packet(struct cxd5610_ctx *ctx,
                       uint8_t *opc_out,
                       uint8_t **payload_out,
                       uint16_t *len_out,
                       int timeout_ms,
                       bool quiet)
{
  if (ctx->transport_error)
    return ctx->transport_error;

  int ret;
  int64_t deadline = timeout_ms > 0 ? monotonic_ms() + timeout_ms : 0;
  if (!ctx->rx_pending)
    {
      uint8_t header[PACKET_HEADERLEN];
      ret = read_part(ctx, header, sizeof(header), timeout_ms);
      if (ret < 0)
        return ret;
      if (header[0] != PACKET_SYNC || header[4] != cxd_checksum(header, 4))
        {
          /* Byte scanning would issue invalid I2C transaction lengths. */
          if (!quiet)
            fprintf(stderr, "[ERR] invalid I2C packet header\n");
          ctx->transport_error = -EBADMSG;
          return -EBADMSG;
        }
      ctx->rx_opcode = header[3];
      ctx->rx_length = le16(&header[1]);
      ctx->rx_offset = 0;
      ctx->rx_checksum = 0;
      size_t maxlen = ctx->rx_opcode < OPC_TIME_NOTIFY ?
                     sizeof(ctx->rcvbuf) : sizeof(ctx->notifybuf);
      ctx->rx_discard = (size_t)ctx->rx_length + 1 > maxlen;
      ctx->rx_pending = true;
    }

  uint8_t *buf = ctx->rx_opcode < OPC_TIME_NOTIFY ? ctx->rcvbuf : ctx->notifybuf;
  size_t total = ctx->rx_length ? (size_t)ctx->rx_length + 1 : 0;
  uint8_t expected_checksum = 0;
  while (ctx->rx_offset < total)
    {
      size_t size = total - ctx->rx_offset;
      if (size > PACKET_MAXLEN)
        size = PACKET_MAXLEN;
      uint8_t discard[PACKET_MAXLEN];
      uint8_t *part = ctx->rx_discard ? discard : buf + ctx->rx_offset;
      int remaining = timeout_ms > 0 ? (int)(deadline - monotonic_ms()) : timeout_ms;
      if (timeout_ms > 0 && remaining <= 0)
        return -ETIMEDOUT;
      ret = read_part(ctx, part, size, remaining);
      if (ret < 0)
        return ret;
      bool last = ctx->rx_offset + size == total;
      for (size_t i = 0; i < size - (last ? 1 : 0); i++)
        ctx->rx_checksum += part[i];
      if (last)
        expected_checksum = part[size - 1];
      ctx->rx_offset += size;
    }

  ctx->rx_pending = false;
  if (total && ctx->rx_checksum != expected_checksum)
    {
      if (!quiet)
        fprintf(stderr, "[ERR] payload checksum opc=0x%02x len=%u\n",
                ctx->rx_opcode, ctx->rx_length);
      return -EAGAIN;
    }
  if (ctx->rx_discard)
    return -EOVERFLOW;
  *opc_out = ctx->rx_opcode;
  *payload_out = ctx->rx_length ? buf : NULL;
  *len_out = ctx->rx_length;
  return 0;
}

/* Notify parsers */
static void handle_time_notify(struct cxd5610_ctx *ctx,
                               const uint8_t *p,
                               uint16_t len)
{
  if (len < sizeof(struct cmd_notify_time))
    {
      return;
    }

  const struct cmd_notify_time *t = (const struct cmd_notify_time *)p;
  if (IS_NOTIFY_INVALID(t->ver8))
    {
      return;
    }

  int year = ((t->yearu_mon >> 4) << 8) | t->yearl;
  int month = t->yearu_mon & 0x0f;

  struct tm tmv;
  memset(&tmv, 0, sizeof(tmv));
  tmv.tm_year = year - 1900;
  tmv.tm_mon = month - 1;
  tmv.tm_mday = t->day;
  tmv.tm_hour = t->hour;
  tmv.tm_min = t->min;
  tmv.tm_sec = t->sec;

  time_t epoch = timegm(&tmv);

  ctx->last.timestamp.tv_sec = epoch;
  ctx->last.timestamp.tv_nsec = (long)t->sec100 * 10L * 1000000L;
}

static void handle_pos_notify(struct cxd5610_ctx *ctx,
                              const uint8_t *p,
                              uint16_t len)
{
  const uint16_t MIN_POS_LEN = 14;
  if (len < MIN_POS_LEN)
    {
      return;
    }

  const struct cmd_notify_pos *pos = (const struct cmd_notify_pos *)p;
  if (IS_NOTIFY_INVALID(pos->ver8))
    {
      return;
    }

  ctx->last.lat = (double)le32(&pos->lat32) / 10000000.0;
  ctx->last.lon = (double)le32(&pos->lon32) / 10000000.0;
  ctx->last.alt = (double)le32(&pos->alt32) / 100.0;  /* [m] */
  ctx->last.status = pos->mode & 0x0f;
}

static void handle_vel_notify(struct cxd5610_ctx *ctx,
                              const uint8_t *p,
                              uint16_t len)
{
  if (len < sizeof(struct cmd_notify_vel))
    {
      return;
    }

  const struct cmd_notify_vel *vel = (const struct cmd_notify_vel *)p;
  if (IS_NOTIFY_INVALID(vel->ver8))
    {
      return;
    }

  ctx->last.speed = (double)(int16_t)le16(&vel->vel16) / 10.0 * 1000 / 3600; /* [km/h] -> [m/s] */
  ctx->last.track = (double)le16(&vel->course16) / 10.0; /* [deg] */
}

static void handle_sat_notify(struct cxd5610_ctx *ctx,
                              const uint8_t *p,
                              uint16_t len)
{
  if (len < sizeof(struct cmd_notify_sat))
    {
      return;
    }

  const struct cmd_notify_sat *sat = (const struct cmd_notify_sat *)p;
  if (IS_NOTIFY_INVALID(sat->ver8))
    {
      return;
    }

  uint16_t numsv = le16(&sat->numsv);
  if (numsv > CXD5610_MAX_SV2_NUM)
    {
      numsv = CXD5610_MAX_SV2_NUM; /* sanity cap aligned to upstream */
    }

  const struct cmd_notify_satinfo *info =
      (const struct cmd_notify_satinfo *)(p + sizeof(struct cmd_notify_sat));

  uint16_t expect_len = sizeof(struct cmd_notify_sat) + numsv * sizeof(*info);
  if (len < expect_len)
    {
      numsv = (len - sizeof(struct cmd_notify_sat)) / sizeof(*info);
    }

  /* TODO: deduplicate satellites by signal type and satellite ID. */
  uint16_t valid_sats = 0;
  for (uint16_t i = 0; i < numsv; i++)
    {
      if (info[i].signal & 0x20) /* Tracking */
        {
          valid_sats++;
        }
    }
  ctx->last.used_sats = (int)valid_sats;
  ctx->last.total_sats = (int)numsv;
  ctx->last.mode = sat->mode & 0x3;
}

static void handle_acc_notify(struct cxd5610_ctx *ctx,
                              const uint8_t *p,
                              uint16_t len)
{
  if (len < sizeof(struct cmd_notify_acc))
    {
      return;
    }

  const struct cmd_notify_acc *acc = (const struct cmd_notify_acc *)p;
  if (IS_NOTIFY_INVALID(acc->ver8))
    {
      return;
    }

  float pdop = (float)acc->pdop8 / 10.0f;
  float hdop = (float)acc->hdop8 / 10.0f;
  float vdop = (float)acc->vdop8 / 10.0f;

  /* Round DOP values to 3 decimal places before crossing the C/Python boundary */
  ctx->last.pdop = roundf(pdop * 1000.0f) / 1000.0f;
  ctx->last.hdop = roundf(hdop * 1000.0f) / 1000.0f;
  ctx->last.vdop = roundf(vdop * 1000.0f) / 1000.0f;
}

static void handle_dc_notify(struct cxd5610_ctx *ctx,
                             const uint8_t *p,
                             uint16_t len)
{
  const size_t header_size = offsetof(struct cmd_notify_dcreport, msg);
  if (len < header_size)
    {
      return;
    }

  const struct cmd_notify_dcreport *dc = (const struct cmd_notify_dcreport *)p;
  if (IS_NOTIFY_INVALID(dc->ver8))
    {
      return;
    }

  const uint8_t count = dc->nr;
  if (count == 0 || count > 3 ||
      len < header_size + count * sizeof(struct mt43_data))
    {
      return;
    }

  struct timespec received_at;
  clock_gettime(CLOCK_REALTIME, &received_at);
  /* Command exchanges can also dispatch notifications, outside the worker lock. */
  if (ctx->lock_initialized)
    pthread_mutex_lock(&ctx->lock);
  /* Ignore disabled or in-flight reports, including command-time notifications. */
  if (!ctx->qzss_dcr_enabled || !ctx->dcr_applied_known || !ctx->ready ||
      ctx->session_state == CXD5610_FAILED ||
      (ctx->dcr_request_pending && !ctx->dcr_request_done))
    {
      if (ctx->lock_initialized)
        pthread_mutex_unlock(&ctx->lock);
      return;
    }
  for (uint8_t i = 0; i < count; i++)
    {
      if (dc->msg[i].svid == 0)
        continue;
      if (ctx->dcreport_count == CXD5610_DCREPORT_QUEUE_SIZE)
        {
          ctx->dcreport_head = (ctx->dcreport_head + 1) % CXD5610_DCREPORT_QUEUE_SIZE;
          ctx->dcreport_count--;
          ctx->dcreports_dropped++;
        }
      size_t tail = (ctx->dcreport_head + ctx->dcreport_count) %
                    CXD5610_DCREPORT_QUEUE_SIZE;
      struct cxd5610_dcreport *report = &ctx->dcreports[tail];
      /* Sony's CXD5610 NMEA implementation uses svid directly as the QZQSM ID. */
      report->satellite_id = dc->msg[i].svid;
      memcpy(report->data, dc->msg[i].data, sizeof(report->data));
      report->received_at = received_at;
      ctx->dcreport_count++;
    }
  if (ctx->lock_initialized)
    pthread_mutex_unlock(&ctx->lock);
}

/* -------------------------------------------------------------------------- */
/* 2. Notify dispatcher                                                       */
/* -------------------------------------------------------------------------- */

static void handle_notify(struct cxd5610_ctx *ctx,
                          uint8_t opc,
                          const uint8_t *payload,
                          uint16_t len)
{
  if (opc == OPC_DISASTER_CRISIS_NOTIFY)
    {
      handle_dc_notify(ctx, payload, len);
      return;
    }
  /* Notifications can also arrive while the worker waits for a command ACK. */
  if (ctx->lock_initialized)
    pthread_mutex_lock(&ctx->lock);
  switch (opc)
    {
      case OPC_TIME_NOTIFY:
        handle_time_notify(ctx, payload, len);
        break;
      case OPC_RECEIVER_POS_NOTIFY:
        handle_pos_notify(ctx, payload, len);
        break;
      case OPC_RECEIVER_VEL_NOTIFY:
        handle_vel_notify(ctx, payload, len);
        break;
      case OPC_SAT_INFO_NOTIFY:
        handle_sat_notify(ctx, payload, len);
        break;
      case OPC_ACCURACY_IDX_NOTIFY:
        handle_acc_notify(ctx, payload, len);
        break;
      default:
        break;
    }
  if (ctx->lock_initialized)
    pthread_mutex_unlock(&ctx->lock);
}

/* Command helpers */
static int64_t monotonic_ms(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int finish_pending_packet(struct cxd5610_ctx *ctx, int64_t deadline)
{
  while (ctx->rx_pending)
    {
      int remaining = (int)(deadline - monotonic_ms());
      if (remaining <= 0)
        return -ETIMEDOUT;
      uint8_t opc, *payload;
      uint16_t len;
      int ret = read_packet(ctx, &opc, &payload, &len, remaining, false);
      if (ret == -EINTR)
        continue;
      if (ret < 0)
        return ret;
      if (opc >= OPC_TIME_NOTIFY)
        handle_notify(ctx, opc, payload, len);
    }
  return 0;
}

static int send_command(struct cxd5610_ctx *ctx,
                        uint8_t opc,
                        const void *opr,
                        size_t oprlen,
                        int timeout_ms)
{
  if (PACKET_LEN(oprlen) > sizeof(ctx->sndbuf) || timeout_ms <= 0)
    return -EINVAL;
  if (ctx->transport_error)
    return ctx->transport_error;
  int64_t deadline = monotonic_ms() + timeout_ms;
  /* Complete the pending notification before starting another exchange. */
  int ret = finish_pending_packet(ctx, deadline);
  if (ret < 0)
    return ret;

  uint8_t *pkt = ctx->sndbuf;
  size_t buflen = PACKET_HEADERLEN;
  pkt[0] = PACKET_SYNC;
  pkt[1] = (uint8_t)(oprlen & 0xff);
  pkt[2] = (uint8_t)(oprlen >> 8);
  pkt[3] = opc;
  pkt[4] = cxd_checksum(pkt, 4);
  if (opr && oprlen > 0)
    {
      memcpy(&pkt[5], opr, oprlen);
      pkt[5 + oprlen] = cxd_checksum(&pkt[5], oprlen);
      buflen += oprlen + 1;
    }
  ret = write_exact(ctx->i2c_fd, pkt, buflen);
  if (ret < 0)
    {
      ctx->transport_error = ret;
      return ret;
    }

  /* Cleanup must still receive STOP's ACK after SIGINT/SIGTERM. */
  for (;;)
    {
      int remaining = (int)(deadline - monotonic_ms());
      if (remaining <= 0)
        return -ETIMEDOUT;
      uint8_t ropc, *payload;
      uint16_t rlen;
      ret = read_packet(ctx, &ropc, &payload, &rlen, remaining, false);
      if (ret == -EINTR)
        continue;
      if (ret < 0)
        return ret;
      if (ropc == opc)
        return rlen > 0 ? (int8_t)payload[0] : -EBADMSG;
      if (ropc == 0xff)
        return -EPROTO;
      if (ropc >= OPC_TIME_NOTIFY)
        handle_notify(ctx, ropc, payload, rlen);
    }
}

/* -------------------------------------------------------------------------- */
/* 3. Session management (init/cleanup/recover)                              */
/* -------------------------------------------------------------------------- */

static int init_i2c(struct cxd5610_ctx *ctx)
{
  ctx->i2c_fd = open(CXD5610_I2C_DEV, O_RDWR);
  if (ctx->i2c_fd < 0)
    {
      return -errno;
    }

  if (ioctl(ctx->i2c_fd, I2C_SLAVE, CXD5610_I2C_ADDR) < 0)
    {
      int err = -errno;
      close(ctx->i2c_fd);
      return err;
    }

  return 0;
}

static int init_gpio(struct cxd5610_ctx *ctx)
{
  ctx->chip = gpiod_chip_open(CXD5610_GPIO_CHIP_DEV);
  if (!ctx->chip)
    {
      return -errno;
    }

  struct gpiod_line_settings *settings = gpiod_line_settings_new();
  if (!settings)
    {
      return -ENOMEM;
    }
  gpiod_line_settings_set_direction(settings, GPIOD_LINE_DIRECTION_INPUT);
  gpiod_line_settings_set_edge_detection(settings, GPIOD_LINE_EDGE_RISING);

  struct gpiod_line_config *lcfg = gpiod_line_config_new();
  if (!lcfg)
    {
      gpiod_line_settings_free(settings);
      return -ENOMEM;
    }

  unsigned int offset = CXD5610_INT_GPIO;
  int ret = gpiod_line_config_add_line_settings(lcfg, &offset, 1, settings);
  gpiod_line_settings_free(settings);
  if (ret < 0)
    {
      gpiod_line_config_free(lcfg);
      return -errno;
    }

  struct gpiod_request_config *rcfg = gpiod_request_config_new();
  if (!rcfg)
    {
      gpiod_line_config_free(lcfg);
      return -ENOMEM;
    }
  gpiod_request_config_set_consumer(rcfg, "cxd5610_gnss");

  ctx->irq_req = gpiod_chip_request_lines(ctx->chip, rcfg, lcfg);
  gpiod_request_config_free(rcfg);
  gpiod_line_config_free(lcfg);

  if (!ctx->irq_req)
    {
      return -errno;
    }

  ctx->evbuf = gpiod_edge_event_buffer_new(16);
  if (!ctx->evbuf)
    {
      return -ENOMEM;
    }

  return 0;
}

static void cleanup(struct cxd5610_ctx *ctx)
{
  if (!ctx)
    {
      return;
    }
  if (ctx->evbuf)
    {
      gpiod_edge_event_buffer_free(ctx->evbuf);
    }
  if (ctx->irq_req)
    {
      gpiod_line_request_release(ctx->irq_req);
    }
  if (ctx->chip)
    {
      gpiod_chip_close(ctx->chip);
    }
  if (ctx->i2c_fd >= 0)
    {
      close(ctx->i2c_fd);
    }
}

static int reopen_i2c(struct cxd5610_ctx *ctx)
{
  if (ctx->i2c_fd >= 0)
    {
      close(ctx->i2c_fd);
    }
  ctx->i2c_fd = -1;
  return init_i2c(ctx);
}

static int configure_dcreport(struct cxd5610_ctx *ctx, bool enabled)
{
  /* Sony GNSS_USE_QZSS_L1S is bit 5; retain QZSS L1CA/L5 and other signals. */
  uint8_t sat_setting[] = {enabled ? 0xef : 0xcf, 0x3f};
  int ret = send_command(ctx, OPC_GNSS_SAT_SETTING, sat_setting,
                         sizeof(sat_setting), CXD5610_COMMAND_TIMEOUT_MS);
  if (ret < 0)
    {
      return ret;
    }

  if (ret != 0)
    return -EPROTO;

  uint8_t notify_list[] = {
    OPC_TIME_NOTIFY,
    OPC_RECEIVER_POS_NOTIFY,
    OPC_RECEIVER_VEL_NOTIFY,
    OPC_SAT_INFO_NOTIFY,
    OPC_ACCURACY_IDX_NOTIFY,
    OPC_DISASTER_CRISIS_NOTIFY
  };
  ret = send_command(ctx, OPC_BINARY_OUTPUT_SET, notify_list,
                     sizeof(notify_list) - (enabled ? 0 : 1), CXD5610_COMMAND_TIMEOUT_MS);
  return ret > 0 ? -EPROTO : ret;
}

static int configure_session(struct cxd5610_ctx *ctx)
{
  if (ctx->lock_initialized)
    pthread_mutex_lock(&ctx->lock);
  reset_session_data(ctx);
  bool enabled = ctx->qzss_dcr_enabled;
  enum cxd5610_error_stage stage = ctx->session_state == CXD5610_RECOVERING ?
                                  CXD5610_STAGE_RECOVERY : CXD5610_STAGE_STARTUP;
  if (ctx->lock_initialized)
    pthread_mutex_unlock(&ctx->lock);

  /* Stop GNSS first (no payload). */
  int ret = send_command(ctx, OPC_GNSS_STOP, NULL, 0, CXD5610_COMMAND_TIMEOUT_MS);
  if (ret == -ETIMEDOUT || ret == -EAGAIN)
    ret = 0; /* Preserve quiet-start behavior for initial configuration. */
  if (ret != 0)
    return fail_session(ctx, ret > 0 ? -EPROTO : ret, stage);
  ret = configure_dcreport(ctx, enabled);
  if (ret < 0)
    return fail_session(ctx, ret, stage);

  /* Start GNSS */
  uint8_t start_param = 3;
  ret = send_command(ctx, OPC_GNSS_START, &start_param, sizeof(start_param),
                     CXD5610_COMMAND_TIMEOUT_MS);
  if (ret != 0)
    return fail_session(ctx, ret > 0 ? -EPROTO : ret, stage);
  if (ctx->lock_initialized)
    pthread_mutex_lock(&ctx->lock);
  ctx->session_state = CXD5610_READY;
  ctx->ready = true;
  ctx->dcr_applied_known = true;
  if (ctx->cond_initialized)
    pthread_cond_broadcast(&ctx->cond);
  if (ctx->lock_initialized)
    pthread_mutex_unlock(&ctx->lock);
  return 0;
}

static int apply_dcreport_request(struct cxd5610_ctx *ctx)
{
  pthread_mutex_lock(&ctx->lock);
  if (!ctx->dcr_request_pending || ctx->dcr_request_done)
    {
      pthread_mutex_unlock(&ctx->lock);
      return 0;
    }
  bool enabled = ctx->dcr_request_enabled;
  bool previous = ctx->qzss_dcr_enabled;
  ctx->session_state = CXD5610_CONFIGURING;
  ctx->dcr_applied_known = false;
  pthread_mutex_unlock(&ctx->lock);

  /* Only this worker owns the I2C exchange. Positioning stays running. */
  int ret = configure_dcreport(ctx, enabled);
  int rollback = ret < 0 ? configure_dcreport(ctx, previous) : 0;

  pthread_mutex_lock(&ctx->lock);
  if (ret == 0)
    {
      ctx->qzss_dcr_enabled = enabled;
      /* A new enable period must not replay messages from the previous period. */
      ctx->dcreport_head = ctx->dcreport_count = 0;
    }
  ctx->dcr_request_result = ret;
  ctx->dcr_request_done = true;
  ctx->change_errno = ret < 0 ? -ret : 0;
  ctx->restore_errno = rollback < 0 ? -rollback : 0;
  if (rollback < 0)
    {
      fail_session_locked(ctx, rollback, CXD5610_STAGE_RESTORE);
    }
  else
    {
      ctx->session_state = CXD5610_READY;
      ctx->dcr_applied_known = true;
    }
  pthread_cond_broadcast(&ctx->cond);
  pthread_mutex_unlock(&ctx->lock);
  return rollback;
}

static int recover_session(struct cxd5610_ctx *ctx)
{
  pthread_mutex_lock(&ctx->lock);
  if (ctx->session_state == CXD5610_FAILED)
    {
      pthread_mutex_unlock(&ctx->lock);
      return CXD5610_SESSION_ERROR;
    }
  ctx->session_state = CXD5610_RECOVERING;
  ctx->ready = ctx->data_valid = ctx->dcr_applied_known = false;
  ctx->dcreport_head = ctx->dcreport_count = 0;
  pthread_cond_broadcast(&ctx->cond);
  pthread_mutex_unlock(&ctx->lock);
  int ret = reopen_i2c(ctx);
  if (ret < 0)
    return fail_session(ctx, ret, CXD5610_STAGE_RECOVERY);
  ret = configure_session(ctx);
  ctx->err_streak = 0;
  return ret;
}

/* -------------------------------------------------------------------------- */
/* 4. Worker thread                                                           */
/* -------------------------------------------------------------------------- */

static bool cxd5610_should_continue(struct cxd5610_ctx *ctx)
{
  if (!ctx)
    {
      return false;
    }
  if (ctx->lock_initialized)
    {
      pthread_mutex_lock(&ctx->lock);
      bool running = ctx->running;
      pthread_mutex_unlock(&ctx->lock);
      return running;
    }
  return false;
}

static void *cxd5610_worker(void *arg)
{
  struct cxd5610_ctx *ctx = (struct cxd5610_ctx *)arg;
  int ret = configure_session(ctx);

  pthread_mutex_lock(&ctx->lock);
  ctx->data_valid = false;
  ctx->last_opc = ret;
  pthread_mutex_unlock(&ctx->lock);

  if (ret < 0)
    {
      goto done;
    }

  bool first_loop = true;

  while (cxd5610_should_continue(ctx))
    {
      if (apply_dcreport_request(ctx) < 0)
        break;
      uint8_t opc;
      uint8_t *payload;
      uint16_t len;

      ret = read_packet(ctx, &opc, &payload, &len, 200, first_loop);
      if (ret < 0)
        {
          if (!cxd5610_should_continue(ctx))
            {
              break;
            }

          /* Keep the packet phase across idle waits and interrupted reads. */
          if (ret == -ETIMEDOUT || ret == -EINTR)
            continue;
          if (ctx->transport_error)
            {
              fail_session(ctx, ret, CXD5610_STAGE_TRANSPORT);
              fprintf(stderr, "[CXD5610] transport failed: %s\n", strerror(-ret));
              break;
            }
          if (ret == -EAGAIN)
            {
              if (first_loop)
                {
                  first_loop = false;
                  ctx->err_streak = 0;
                  continue;
                }

              fprintf(stderr, "[CXD5610] read warning (%s). Reopening I2C...\n",
                      strerror(-ret));
              int reopened = reopen_i2c(ctx);
              if (reopened < 0)
                {
                  fail_session(ctx, reopened, CXD5610_STAGE_RECOVERY);
                  break;
                }
            }
          else
            {
              fprintf(stderr, "[CXD5610] read error: %s\n", strerror(-ret));
            }
          if (++ctx->err_streak >= 5)
            {
              fprintf(stderr, "[CXD5610] consecutive errors, reinitializing session...\n");
              if (recover_session(ctx) < 0)
                break;
            }
          continue;
        }

      first_loop = false;
      ctx->err_streak = 0;

      /* DC reports have their own queue and must not publish a position sample. */
      if (opc == OPC_DISASTER_CRISIS_NOTIFY)
        {
          handle_dc_notify(ctx, payload, len);
          continue;
        }

      pthread_mutex_lock(&ctx->lock);
      ctx->last_opc = (int)opc;
      if (opc < OPC_TIME_NOTIFY)
        {
          /* Command reply: keep last opcode/status for diagnostics. */
          pthread_mutex_unlock(&ctx->lock);
          continue;
        }

      pthread_mutex_unlock(&ctx->lock);
      handle_notify(ctx, opc, payload, len);
      pthread_mutex_lock(&ctx->lock);
      ctx->data_valid = true;
      ctx->sample_seq++;
      pthread_cond_signal(&ctx->cond);
      pthread_mutex_unlock(&ctx->lock);
    }

done:
  pthread_mutex_lock(&ctx->lock);
  if (!ctx->closing && ctx->session_state != CXD5610_FAILED)
    fail_session_locked(ctx, -EIO, CXD5610_STAGE_WORKER);
  if (ctx->closing && ctx->session_state != CXD5610_FAILED)
    ctx->session_state = CXD5610_STOPPED;
  ctx->worker_alive = false;
  ctx->running = false;
  pthread_cond_broadcast(&ctx->cond);
  pthread_mutex_unlock(&ctx->lock);
  return NULL;
}

/* -------------------------------------------------------------------------- */
/* 5. Public API (create/read/close)                                          */
/* -------------------------------------------------------------------------- */

int cxd5610_create(struct cxd5610_ctx **out_ctx)
{
  return cxd5610_create_with_dcr(out_ctx, true);
}

int cxd5610_create_with_dcr(struct cxd5610_ctx **out_ctx, bool enabled)
{
  if (!out_ctx)
    {
      return -EINVAL;
    }

  struct cxd5610_ctx *ctx = calloc(1, sizeof(struct cxd5610_ctx));
  if (!ctx)
    {
      return -ENOMEM;
    }

  ctx->i2c_fd = -1;
  ctx->qzss_dcr_enabled = enabled;
  reset_data(&ctx->last);
  ctx->last_opc = -1;

  int ret = init_i2c(ctx);
  if (ret < 0)
    goto fail;

  ret = init_gpio(ctx);
  if (ret < 0)
    goto fail;

  ret = init_sync(ctx);
  if (ret < 0)
    goto fail;

  ctx->running = true;
  ctx->worker_alive = true;
  ret = -pthread_create(&ctx->thread, NULL, cxd5610_worker, ctx);
  if (ret != 0)
    {
      ctx->running = false;
      goto fail;
    }

  ctx->thread_started = true;
  *out_ctx = ctx;
  return 0;

fail:
  cxd5610_close(ctx);
  *out_ctx = NULL;
  return ret;
}

int cxd5610_set_qzss_dcr_enabled(struct cxd5610_ctx *ctx, bool enabled)
{
  if (!ctx)
    return -EINVAL;
  if (!ctx->lock_initialized || !ctx->cond_initialized)
    return -ENOTSUP;
  pthread_mutex_lock(&ctx->lock);
  if (ctx->session_state == CXD5610_FAILED)
    {
      pthread_mutex_unlock(&ctx->lock);
      return CXD5610_SESSION_ERROR;
    }
  if (!ctx->running || !ctx->worker_alive)
    {
      int ret = ctx->last_opc < 0 ? ctx->last_opc : -EIO;
      pthread_mutex_unlock(&ctx->lock);
      return ret;
    }
  if (ctx->dcr_request_pending)
    {
      pthread_mutex_unlock(&ctx->lock);
      return -EBUSY;
    }
  ctx->dcr_request_pending = true;
  ctx->dcr_request_done = false;
  ctx->dcr_request_enabled = enabled;
  pthread_cond_broadcast(&ctx->cond);
  while (!ctx->dcr_request_done && ctx->running && ctx->worker_alive)
    pthread_cond_wait(&ctx->cond, &ctx->lock);
  int ret = ctx->session_state == CXD5610_FAILED ? CXD5610_SESSION_ERROR :
            ctx->dcr_request_done ? ctx->dcr_request_result :
            (ctx->last_opc < 0 ? ctx->last_opc : -ECANCELED);
  ctx->dcr_request_pending = false;
  pthread_mutex_unlock(&ctx->lock);
  return ret;
}

int cxd5610_get_session_status(struct cxd5610_ctx *ctx,
                                struct cxd5610_session_status *out)
{
  if (!ctx || !out)
    return -EINVAL;
  if (!ctx->lock_initialized)
    return -ENOTSUP;
  pthread_mutex_lock(&ctx->lock);
  out->state = ctx->session_state;
  out->qzss_dcr_requested = ctx->dcr_request_pending && !ctx->dcr_request_done ?
                           ctx->dcr_request_enabled : ctx->qzss_dcr_enabled;
  out->qzss_dcr_applied = ctx->dcr_applied_known ? ctx->qzss_dcr_enabled : -1;
  out->error_errno = ctx->session_errno;
  out->change_errno = ctx->change_errno;
  out->restore_errno = ctx->restore_errno;
  out->error_stage = ctx->error_stage;
  pthread_mutex_unlock(&ctx->lock);
  return 0;
}

int cxd5610_wait_ready(struct cxd5610_ctx *ctx, int timeout_ms)
{
  if (!ctx || timeout_ms < 0)
    return -EINVAL;
  if (!ctx->lock_initialized || !ctx->cond_initialized)
    return -ENOTSUP;
  int64_t deadline = monotonic_ms() + timeout_ms;
  pthread_mutex_lock(&ctx->lock);
  int ret = 0;
  while (ctx->session_state != CXD5610_READY)
    {
      if (ctx->session_state == CXD5610_FAILED)
        {
          ret = CXD5610_SESSION_ERROR;
          break;
        }
      if (!ctx->running || !ctx->worker_alive)
        {
          ret = -ECANCELED;
          break;
        }
      int remaining = (int)(deadline - monotonic_ms());
      if (remaining <= 0)
        {
          ret = -ETIMEDOUT;
          break;
        }
      ret = cond_wait_ms(&ctx->cond, &ctx->lock, remaining);
      if (ret < 0 && ret != -ETIMEDOUT)
        break;
      ret = 0;
    }
  pthread_mutex_unlock(&ctx->lock);
  return ret;
}

int cxd5610_read(struct cxd5610_ctx *ctx, struct cxd5610_data *out, int timeout_ms)
{
  if (!ctx)
    {
      return -EINVAL;
    }

  if (ctx->thread_started && ctx->lock_initialized)
    {
      int ret = 0;
      pthread_mutex_lock(&ctx->lock);
      uint64_t start_seq = ctx->sample_seq;
      int64_t deadline = timeout_ms < 0 ? 0 : monotonic_ms() + timeout_ms;

      while (ctx->running && (!ctx->data_valid || ctx->sample_seq == start_seq))
        {
          if (timeout_ms == 0)
            {
              ret = -EAGAIN;
              break;
            }

          int remaining = timeout_ms < 0 ? -1 : (int)(deadline - monotonic_ms());
          ret = remaining == 0 || (timeout_ms > 0 && remaining < 0) ?
                -ETIMEDOUT : cond_wait_ms(&ctx->cond, &ctx->lock, remaining);
          if (ret < 0)
            break;
        }

      if (ctx->session_state == CXD5610_FAILED)
        {
          reset_data(out);
          ret = CXD5610_SESSION_ERROR;
        }
      else if (ret == 0 && (!ctx->running || !ctx->data_valid))
        {
          ret = -EIO;
        }

      if (ret == 0 && out)
        {
          *out = ctx->last;
        }

      int opc = (ret == 0) ? ctx->last_opc : ret;
      pthread_mutex_unlock(&ctx->lock);
      return opc;
    }

  /* Fallback: blocking read without worker thread */
  uint8_t opc;
  uint8_t *payload;
  uint16_t len;

  int ret = read_packet(ctx, &opc, &payload, &len, timeout_ms, false);
  if (ret < 0)
    {
      return ret;
    }

  if (opc >= OPC_TIME_NOTIFY)
    {
      handle_notify(ctx, opc, payload, len);
    }

  if (out)
    {
      *out = ctx->last;
    }

  return (int)opc;
}

int cxd5610_peek(struct cxd5610_ctx *ctx, struct cxd5610_data *out)
{
  if (!ctx || !out)
    {
      return -EINVAL;
    }

  if (ctx->thread_started && ctx->lock_initialized)
    {
      int ret = 0;
      pthread_mutex_lock(&ctx->lock);
      if (ctx->session_state == CXD5610_FAILED)
        {
          reset_data(out);
          ret = CXD5610_SESSION_ERROR;
        }
      else if (!ctx->data_valid)
        {
          ret = -EAGAIN;
        }
      else
        {
          *out = ctx->last;
        }
      pthread_mutex_unlock(&ctx->lock);
      return ret;
    }

  return -ENOTSUP;
}

int cxd5610_read_dcreports(struct cxd5610_ctx *ctx,
                          struct cxd5610_dcreport *out,
                          size_t capacity, uint64_t *dropped)
{
  if (!ctx || (capacity > 0 && !out))
    return -EINVAL;
  if (!ctx->lock_initialized)
    return -ENOTSUP;

  pthread_mutex_lock(&ctx->lock);
  if (capacity > 0 && ctx->session_state == CXD5610_FAILED)
    {
      pthread_mutex_unlock(&ctx->lock);
      return CXD5610_SESSION_ERROR;
    }
  size_t count = capacity < ctx->dcreport_count ? capacity : ctx->dcreport_count;
  for (size_t i = 0; i < count; i++)
    {
      out[i] = ctx->dcreports[ctx->dcreport_head];
      ctx->dcreport_head = (ctx->dcreport_head + 1) % CXD5610_DCREPORT_QUEUE_SIZE;
    }
  ctx->dcreport_count -= count;
  if (dropped)
    *dropped = ctx->dcreports_dropped;
  pthread_mutex_unlock(&ctx->lock);
  return (int)count;
}

int cxd5610_shutdown(struct cxd5610_ctx *ctx)
{
  if (!ctx)
    return 0;
  cxd5610_join_and_destroy_sync(ctx);
  int ret = 0;
  /* Initialization can fail before either GPIO resource exists. */
  if (ctx->i2c_fd >= 0 && ctx->irq_req && ctx->evbuf)
    ret = send_command(ctx, OPC_GNSS_STOP, NULL, 0, 5000);
  cleanup(ctx);
  free(ctx);
  return ret;
}

void cxd5610_close(struct cxd5610_ctx *ctx)
{
  int ret = cxd5610_shutdown(ctx);
  if (ret < 0)
    fprintf(stderr, "[CXD5610] stop during close failed: %s\n", strerror(-ret));
}


#ifndef CXD5610_NO_MAIN
int main(void)
{
  cxd5610_ctx *ctx = NULL;
  signal(SIGINT, handle_sigint);
  signal(SIGTERM, handle_sigint);

  int ret = cxd5610_create(&ctx);
  if (ret < 0 || !ctx)
    {
      fprintf(stderr, "[CLI ] Failed to init CXD5610: %s\n", strerror(-ret));
      return EXIT_FAILURE;
    }

  printf("[CLI ] CXD5610 initialized. Printing latest data every 1s (Ctrl+C to exit)...\n");

  while (!g_stop)
    {
      struct cxd5610_data d;
      ret = cxd5610_peek(ctx, &d);
      if (ret == -EAGAIN)
        {
          printf("[DATA] waiting for first fix...\n");
        }
      else if (ret < 0)
        {
          fprintf(stderr, "[CLI ] read error: %s\n", strerror(-ret));
        }
      else
        {
          printf("[DATA] lat=%.7f lon=%.7f alt=%.0f[m] speed=%.1f[m/s] track=%.0f[deg] fix=%d sats=%d/%d\n",
                 d.lat, d.lon, d.alt, d.speed, d.track,
                 d.mode, d.used_sats, d.total_sats);
        }

      sleep(1);
    }

  ret = cxd5610_shutdown(ctx);
  if (ret < 0)
    {
      fprintf(stderr, "[CLI ] stop failed: %s\n", strerror(-ret));
      return EXIT_FAILURE;
    }
  return EXIT_SUCCESS;
}

#endif /* CXD5610_NO_MAIN */
