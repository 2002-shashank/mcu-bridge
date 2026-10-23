/* SPDX-License-Identifier: Apache-2.0 */
/*
 * mcu-bridge firmware for the Sunton ESP32-3248S035R.
 *
 * main thread:    boot sequence, then DATA (touch sample) at rate_hz on
 *                 absolute deadlines, so the rate does not drift by the
 *                 sampling time
 * command thread: answers host commands: PING -> PONG, SET_RATE -> ACK,
 *                 anything else -> ACK "unsupported"
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>

#include "link.h"
#include "touch.h"

#define FW_VERSION	0x00010000	/* 1.0.0 (the simulator reports 0.1.0) */
#define DEFAULT_RATE_HZ	50

static atomic_t rate_hz = ATOMIC_INIT(DEFAULT_RATE_HZ);
static k_tid_t sampler;

static void send_hello(void)
{
	uint8_t p[MCB_HELLO_LEN] = { MCB_VERSION, 0 };

	mcb_put_le16(p + 2, (uint16_t)atomic_get(&rate_hz));
	mcb_put_le32(p + 4, FW_VERSION);
	link_send(MCB_TYPE_HELLO, p, sizeof(p));
}

static void send_ack(const struct mcb_frame *req, uint8_t status)
{
	uint8_t p[MCB_ACK_LEN];

	mcb_put_le32(p, req->seq);
	p[4] = req->type;
	p[5] = status;
	link_send(MCB_TYPE_ACK, p, sizeof(p));
}

static void handle(const struct mcb_frame *f)
{
	uint8_t p[4 + MCB_PING_MAX_TOKEN];
	uint8_t tok;
	uint16_t hz;

	switch (f->type) {
	case MCB_TYPE_PING:
		tok = MIN(f->len, MCB_PING_MAX_TOKEN);
		mcb_put_le32(p, f->seq);
		memcpy(p + 4, f->payload, tok);
		link_send(MCB_TYPE_PONG, p, 4 + tok);
		break;
	case MCB_TYPE_SET_RATE:
		hz = f->len == 2 ? mcb_get_le16(f->payload) : 0;
		if (hz < MCB_RATE_MIN_HZ || hz > MCB_RATE_MAX_HZ) {
			send_ack(f, MCB_STATUS_RANGE);
			break;
		}
		atomic_set(&rate_hz, hz);
		k_wakeup(sampler);	/* apply now, not after the old period */
		send_ack(f, MCB_STATUS_OK);
		break;
	default:
		send_ack(f, MCB_STATUS_UNSUPPORTED);
		break;
	}
}

static void command_thread(void *a, void *b, void *c)
{
	struct mcb_frame f;

	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);
	for (;;) {
		if (!link_recv(&f, K_FOREVER))
			handle(&f);
	}
}

K_THREAD_DEFINE(cmd_tid, 2048, command_thread, NULL, NULL, NULL, 5, 0, K_TICKS_FOREVER);

int main(void)
{
	static const uint8_t flush;	/* 0x00: ends the ROM boot text on the host side */
	struct touch_sample s;
	uint8_t p[4 * 4];
	int64_t next;
	int last_rate = 0;

	sampler = k_current_get();
	if (link_init() || touch_init())
		return 0;	/* no console to report on; the host sees silence */

	link_send_raw(&flush, 1);
	send_hello();
	k_thread_start(cmd_tid);

	next = k_uptime_ticks();
	for (;;) {
		int rate = atomic_get(&rate_hz);

		if (rate != last_rate) {	/* rate changed: restart the schedule */
			next = k_uptime_ticks();
			last_rate = rate;
		}
		if (touch_read(&s) == 0) {
			mcb_put_le32(p + 0, s.touched);
			mcb_put_le32(p + 4, (uint32_t)s.x);
			mcb_put_le32(p + 8, (uint32_t)s.y);
			mcb_put_le32(p + 12, (uint32_t)s.pressure);
			link_send(MCB_TYPE_DATA, p, sizeof(p));
		}
		next += k_us_to_ticks_ceil64(USEC_PER_SEC / rate);
		if (next < k_uptime_ticks())	/* fell behind: don't burst to catch up */
			next = k_uptime_ticks();
		k_sleep(K_TIMEOUT_ABS_TICKS(next));
	}
}
