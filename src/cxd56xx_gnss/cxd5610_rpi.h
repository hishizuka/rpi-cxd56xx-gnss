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

/*
 * Public API for CXD5610 GNSS access from Cython.
 *
 * Portions of this header and the corresponding implementation are adapted from
 * the Apache NuttX RTOS (Apache License 2.0), specifically the Sony CXD56xx GNSS driver.
 * See NOTICE for attribution and details.
 */
#ifndef CXD5610_RPI_H
#define CXD5610_RPI_H

#include <errno.h>
#include <fcntl.h>
#include <gpiod.h>
#include <linux/i2c-dev.h>
#include <math.h>
#include <pthread.h>
#include <poll.h>
#include <signal.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#ifndef CXD5610_I2C_DEV
#define CXD5610_I2C_DEV     "/dev/i2c-1"
#endif
#ifndef CXD5610_I2C_ADDR
#define CXD5610_I2C_ADDR    0x24
#endif
#ifndef CXD5610_INT_GPIO
#define CXD5610_INT_GPIO   17  /* Interrupt line offset within the selected GPIO chip */
#endif
#ifndef CXD5610_GPIO_CHIP_DEV
#define CXD5610_GPIO_CHIP_DEV "/dev/gpiochip4"
#endif

#define PACKET_SYNC         0x7f
#define PACKET_MAXLEN       512
#define PACKET_HEADERLEN    5
#define PACKET_CHECKSUMLEN  1
#define PACKET_LEN(n)       ((n) + PACKET_HEADERLEN + PACKET_CHECKSUMLEN)
#define PACKET_NR(n)        (((n) + (PACKET_MAXLEN - 1)) / PACKET_MAXLEN)

/* OPC codes */
#define OPC_SYS_STATE_CHANGE_INSTRUCTION  0x00
#define OPC_FWVER_REQ                     0x06
#define OPC_BACKUP_MANUAL                 0x10
#define OPC_PPS_OUTPUT                    0x15
#define OPC_GNSS_START                    0x30
#define OPC_GNSS_STOP                     0x31
#define OPC_GNSS_SAT_SETTING              0x32
#define OPC_BINARY_OUTPUT_SET             0x34
#define OPC_RECEIVER_POS_SET              0x35
#define OPC_UTC_TIME_SET                  0x36
#define OPC_OP_MODE_SET                   0x3d
#define OPC_OP_MODE_GET                   0x3e
#define OPC_TIME_NOTIFY                   0x80
#define OPC_RECEIVER_POS_NOTIFY           0x81
#define OPC_RECEIVER_VEL_NOTIFY           0x82
#define OPC_SAT_INFO_NOTIFY               0x83
#define OPC_ACCURACY_IDX_NOTIFY           0x89
#define OPC_DISASTER_CRISIS_NOTIFY        0x8b

/* System state payload values */
#define STATE_RESET     0x01
#define STATE_WAKEUP    0x02

/* Notify helpers */
#define IS_NOTIFY_INVALID(v)  (((v) & 0x80) == 0)
#define GET_PACKET_VERSION(v) ((v) & 0x7f)

#define NOTIFY_MAX_BUF  (PACKET_MAXLEN * 3) /* 1536 bytes as in the RTOS driver */
#define CXD5610_MAX_SV2_NUM 150 /* Align with CXD56_GNSS_MAX_SV2_NUM in NuttX */
#define CXD5610_DCREPORT_BYTES 32
#define CXD5610_DCREPORT_QUEUE_SIZE 64
#ifndef CXD5610_COMMAND_TIMEOUT_MS
#define CXD5610_COMMAND_TIMEOUT_MS 3000
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct cxd5610_ctx cxd5610_ctx;

enum cxd5610_session_state {
  CXD5610_STARTING,
  CXD5610_READY,
  CXD5610_CONFIGURING,
  CXD5610_RECOVERING,
  CXD5610_FAILED,
  CXD5610_STOPPED
};

enum cxd5610_error_stage {
  CXD5610_STAGE_NONE,
  CXD5610_STAGE_STARTUP,
  CXD5610_STAGE_RECOVERY,
  CXD5610_STAGE_RESTORE,
  CXD5610_STAGE_TRANSPORT,
  CXD5610_STAGE_WORKER
};

/* Failed sessions return this code; status retains the original positive errno. */
#define CXD5610_SESSION_ERROR (-ENOTRECOVERABLE)

struct cxd5610_session_status {
  int state;
  bool qzss_dcr_requested;
  int qzss_dcr_applied; /* -1 when not confirmed, otherwise 0 or 1. */
  int error_errno;
  int change_errno;
  int restore_errno;
  int error_stage;
};

struct cxd5610_data {
  double lat;
  double lon;
  double alt;
  double speed;
  double track;
  int mode;
  int status;
  double pdop;
  double hdop;
  double vdop;
  int used_sats;
  int total_sats;
  struct timespec timestamp;
};

struct cxd5610_dcreport {
  uint8_t satellite_id; /* QZQSM ID, e.g. 55 for PRN 183 */
  uint8_t data[CXD5610_DCREPORT_BYTES]; /* Unmodified, MSB-first L1S message */
  struct timespec received_at; /* Host CLOCK_REALTIME, independent of GNSS fix */
};

int cxd5610_create(cxd5610_ctx **out_ctx);
int cxd5610_create_with_dcr(cxd5610_ctx **out_ctx, bool enabled);
int cxd5610_get_session_status(cxd5610_ctx *ctx, struct cxd5610_session_status *out);
/* Finite wait: zero on ready, -ETIMEDOUT while still starting/recovering,
 * CXD5610_SESSION_ERROR on a terminal failure. Serialize with shutdown.
 */
int cxd5610_wait_ready(cxd5610_ctx *ctx, int timeout_ms);
/* Apply L1S reception and DC output on the worker; wait for both ACKs.
 * Return zero on success, negative errno on failure. Serialize with shutdown.
 */
int cxd5610_set_qzss_dcr_enabled(cxd5610_ctx *ctx, bool enabled);
int cxd5610_read(cxd5610_ctx *ctx, struct cxd5610_data *out, int timeout_ms);
int cxd5610_peek(cxd5610_ctx *ctx, struct cxd5610_data *out);
/* Drain up to capacity reports in arrival order; return count or negative errno.
 * dropped receives the cumulative number of reports lost to queue overflow.
 * A zero capacity queries dropped without consuming reports; out may be NULL.
 */
int cxd5610_read_dcreports(cxd5610_ctx *ctx, struct cxd5610_dcreport *out,
                          size_t capacity, uint64_t *dropped);
/* Both shutdown functions release the context, including on failure. */
int cxd5610_shutdown(cxd5610_ctx *ctx);
void cxd5610_close(cxd5610_ctx *ctx);

#ifdef __cplusplus
}
#endif

#endif /* CXD5610_RPI_H */
