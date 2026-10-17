/* SPDX-License-Identifier: Apache-2.0 */
/* Experimental RTCM capture using the existing driver's I2C/IRQ transport. */
#define _GNU_SOURCE
#define CXD5610_NO_MAIN 1
#include "../src/cxd56xx_gnss/cxd5610_rpi.h"

/* This translation unit alone accepts the protocol's full notification size. */
#undef NOTIFY_MAX_BUF
#define NOTIFY_MAX_BUF 4096
#include "../src/cxd56xx_gnss/cxd5610_rpi.c"

static FILE *capture;
static double origin;

static double monotonic_seconds(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return ts.tv_sec + ts.tv_nsec / 1e9;
}

static int save_packet(uint8_t opc, const uint8_t *data, uint16_t len)
{
  uint8_t header[] = {PACKET_SYNC, len & 0xff, len >> 8, opc, 0};
  header[4] = cxd_checksum(header, 4);
  if (fwrite(header, 1, sizeof(header), capture) != sizeof(header))
    return -EIO;
  if (len)
    {
      uint8_t checksum = cxd_checksum(data, len);
      if (fwrite(data, 1, len, capture) != len ||
          fwrite(&checksum, 1, 1, capture) != 1)
        return -EIO;
    }
  return fflush(capture) == 0 ? 0 : -EIO;
}

static int command(struct cxd5610_ctx *ctx, uint8_t opc,
                   const uint8_t *data, uint16_t len)
{
  double deadline = monotonic_seconds() + 5;
  /* A timed-out or interrupted capture may still owe a notification body. */
  while (ctx->rx_pending)
    {
      int remaining = (int)((deadline - monotonic_seconds()) * 1000);
      if (remaining <= 0)
        return -ETIMEDOUT;
      uint8_t received_opc, *payload;
      uint16_t received_len;
      int ret = read_packet(ctx, &received_opc, &payload, &received_len, remaining, true);
      if (ret == -EINTR)
        continue;
      if (ret < 0)
        return ret;
      ret = save_packet(received_opc, payload, received_len);
      if (ret < 0)
        return ret;
    }
  if (ctx->transport_error)
    return ctx->transport_error;
  uint8_t packet[64] = {PACKET_SYNC, len & 0xff, len >> 8, opc, 0};
  if (len + 6 > sizeof(packet))
    return -EINVAL;
  packet[4] = cxd_checksum(packet, 4);
  size_t size = 5;
  if (len)
    {
      memcpy(packet + 5, data, len);
      packet[5 + len] = cxd_checksum(data, len);
      size += len + 1;
    }
  int ret = write_exact(ctx->i2c_fd, packet, size);
  if (ret < 0)
    return ret;
  while (monotonic_seconds() < deadline)
    {
      uint8_t received_opc;
      uint8_t *payload;
      uint16_t received_len;
      int remaining = (int)((deadline - monotonic_seconds()) * 1000);
      if (remaining <= 0)
        break;
      ret = read_packet(ctx, &received_opc, &payload, &received_len, remaining, true);
      if (ret == -EINTR || ret == -ETIMEDOUT || ret == -EAGAIN)
        continue;
      if (ret < 0)
        return ret;
      ret = save_packet(received_opc, payload, received_len);
      if (ret < 0)
        return ret;
      if (received_opc == opc || received_opc == 0xff)
        {
          /* Firmware replies contain text; other commands return an error byte. */
          ret = received_opc == 0xff ? -EPROTO :
                received_len == 0 ? -EBADMSG :
                opc == OPC_FWVER_REQ ? 0 :
                payload[0] >= 128 ? (int8_t)payload[0] : -(int)payload[0];
          printf("{\"event\":\"command\",\"elapsed_s\":%.6f,\"opc\":%u,"
                 "\"response_opc\":%u,\"result\":%d,\"request_hex\":\"",
                 monotonic_seconds() - origin, opc, received_opc, ret);
          for (unsigned int i = 0; i < len; i++)
            printf("%02x", data[i]);
          printf("\",\"payload_hex\":\"");
          for (unsigned int i = 0; i < received_len; i++)
            printf("%02x", payload[i]);
          printf("\"}\n");
          fflush(stdout);
          return ret;
        }
    }
  fprintf(stderr, "Command 0x%02x timed out\n", opc);
  return -ETIMEDOUT;
}

int main(int argc, char **argv)
{
  if (argc < 3 || argc > 5)
    {
      fprintf(stderr, "Usage: %s OUTPUT_FILE DURATION_SECONDS [RTCM_MASK [GNSS_MASK]]\n", argv[0]);
      return 2;
    }
  char *end;
  double duration = strtod(argv[2], &end);
  if (*end || !isfinite(duration) || duration < 0)
    return 2;
  unsigned long mask = 0x07f22222UL;
  if (argc >= 4)
    {
      errno = 0;
      mask = strtoul(argv[3], &end, 0);
      if (argv[3][0] == '-' || end == argv[3] || *end || errno || mask > UINT32_MAX)
        return 2;
    }
  unsigned long gnss_mask = 0;
  if (argc == 5)
    {
      errno = 0;
      gnss_mask = strtoul(argv[4], &end, 0);
      if (argv[4][0] == '-' || end == argv[4] || *end || errno ||
          !gnss_mask || (gnss_mask & ~0x3fefUL))
        return 2;
    }
  capture = fopen(argv[1], "wbx");
  if (!capture)
    {
      perror("capture");
      return 1;
    }
  setvbuf(stdout, NULL, _IOLBF, 0);
  signal(SIGINT, handle_sigint);
  signal(SIGTERM, handle_sigint);
  origin = monotonic_seconds();
  struct cxd5610_ctx ctx = {.i2c_fd = -1};
  reset_data(&ctx.last);
  int ret = init_i2c(&ctx);
  bool connected = false;
  bool configured = false;
  int errors = 0;
  unsigned int counts[256] = {0};
  uint8_t normal_notify[] = {0x80, 0x81, 0x82, 0x83, 0x89};
  uint8_t rtcm_notify[] = {0x80, 0x81, 0x82, 0x83, 0x84, 0x89};
  /* The default selects all documented MSM7 signals and ephemerides. */
  uint8_t types[] = {mask & 0xff, (mask >> 8) & 0xff,
                     (mask >> 16) & 0xff, (mask >> 24) & 0xff};
  uint8_t disabled[] = {0, 0, 0, 0};
  uint8_t start_mode = 3;
  if (ret == 0)
    ret = init_gpio(&ctx);
  if (ret < 0)
    goto done;
  connected = true;
  ret = command(&ctx, OPC_FWVER_REQ, NULL, 0);
  if (ret < 0)
    goto done;
  ret = command(&ctx, OPC_GNSS_STOP, NULL, 0);
  if (ret < 0)
    goto done;
  /* No backup/write/reset commands are sent by this probe. */
  configured = true;
  if (gnss_mask)
    {
      uint8_t selection[] = {gnss_mask & 0xff, gnss_mask >> 8};
      ret = command(&ctx, OPC_GNSS_SAT_SETTING, selection, sizeof(selection));
      if (ret < 0)
        goto done;
    }
  ret = command(&ctx, 0x45, types, sizeof(types));
  if (ret < 0)
    goto done;
  ret = command(&ctx, OPC_BINARY_OUTPUT_SET, rtcm_notify, sizeof(rtcm_notify));
  if (ret == -EINVAL)
    {
      /* Distinguish an unsupported notification from a notification-count limit. */
      uint8_t minimal_notify[] = {0x80, 0x84};
      ret = command(&ctx, OPC_BINARY_OUTPUT_SET, minimal_notify, sizeof(minimal_notify));
      if (ret == -EINVAL)
        {
          uint8_t rtcm_only = 0x84;
          ret = command(&ctx, OPC_BINARY_OUTPUT_SET, &rtcm_only, 1);
        }
    }
  if (ret < 0)
    goto done;
  ret = command(&ctx, OPC_GNSS_START, &start_mode, 1);
  if (ret < 0)
    goto done;

  double started = monotonic_seconds();
  while (!g_stop && (duration == 0 || monotonic_seconds() - started < duration))
    {
      uint8_t opc;
      uint8_t *payload;
      uint16_t len;
      int received = read_packet(&ctx, &opc, &payload, &len, 250, true);
      if (received == -ETIMEDOUT || received == -EAGAIN)
        continue;
      if (received < 0)
        {
          if (received == -EINTR && g_stop)
            break;
          fprintf(stderr, "Read error: %s\n", strerror(-received));
          errors++;
          ret = received;
          break;
        }
      counts[opc]++;
      ret = save_packet(opc, payload, len);
      if (ret < 0)
        break;
    }
  printf("{\"event\":\"capture\",\"duration_s\":%.6f,\"rtcm_notifications\":%u,"
         "\"read_errors\":%d,\"interrupted\":%s}\n",
         monotonic_seconds() - started, counts[0x84], errors, g_stop ? "true" : "false");

done:
  if (ret < 0)
    fprintf(stderr, "Probe failed: %s (%d)\n", strerror(-ret), ret);
  /* Restore the notification set used by the normal driver, then release IRQ. */
  if (connected && configured)
    {
      int stop_ret = command(&ctx, OPC_GNSS_STOP, NULL, 0);
      int mask_ret = command(&ctx, 0x45, disabled, sizeof(disabled));
      int notify_ret = command(&ctx, OPC_BINARY_OUTPUT_SET, normal_notify, sizeof(normal_notify));
      printf("{\"event\":\"cleanup\",\"stop\":%d,\"disable_rtcm\":%d,\"normal_notify\":%d}\n",
             stop_ret, mask_ret, notify_ret);
      if (stop_ret || mask_ret || notify_ret)
        ret = -EIO;
    }
  cleanup(&ctx);
  if (fclose(capture) != 0)
    ret = -EIO;
  if (ret < 0)
    return 1;
  return g_stop ? 130 : 0;
}
