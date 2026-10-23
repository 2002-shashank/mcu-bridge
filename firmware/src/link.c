/* SPDX-License-Identifier: Apache-2.0 */
/*
 * mcu-bridge link over UART0 (docs/protocol.md), interrupt driven.
 *
 * The ISR moves bytes between the UART FIFOs and two ring buffers. Each ring
 * has exactly one producer and one consumer, which Zephyr's ring buffer
 * supports without locking:
 *   rx_ring: ISR -> command thread (link_recv)
 *   tx_ring: senders -> ISR; tx_lock makes the senders (sampler and command
 *            threads) one producer and keeps seq order = queue order.
 * A sender never blocks on a full ring: the frame is dropped whole.
 */
#include <errno.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/ring_buffer.h>

#include "link.h"

static const struct device *const uart = DEVICE_DT_GET(DT_CHOSEN(mcb_uart));

RING_BUF_DECLARE(rx_ring, 256);
RING_BUF_DECLARE(tx_ring, 1024);
static K_SEM_DEFINE(rx_sem, 0, 1);
static K_MUTEX_DEFINE(tx_lock);

static uint32_t tx_seq;
static uint8_t boot_id;
static struct mcb_rx rx;	/* only touched by the link_recv() caller */

uint32_t mcb_crc32(const uint8_t *data, size_t len)
{
	return crc32_ieee(data, len);
}

static void uart_isr(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);

	if (!uart_irq_update(dev))
		return;

	if (uart_irq_rx_ready(dev)) {
		uint8_t buf[32];
		int n;

		while ((n = uart_fifo_read(dev, buf, sizeof(buf))) > 0) {
			/* bytes beyond the ring are lost, like a UART overrun */
			ring_buf_put(&rx_ring, buf, n);
		}
		k_sem_give(&rx_sem);
	}

	if (uart_irq_tx_ready(dev)) {
		uint8_t *p;
		uint32_t n = ring_buf_get_claim(&tx_ring, &p, 64);

		if (!n) {
			uart_irq_tx_disable(dev);
			/*
			 * A sender may have queued data and enabled TX between
			 * our empty check and the disable: re-check, or that
			 * frame would wait for the next send.
			 */
			if (!ring_buf_is_empty(&tx_ring))
				uart_irq_tx_enable(dev);
		} else {
			int sent = uart_fifo_fill(dev, p, n);

			ring_buf_get_finish(&tx_ring, sent > 0 ? sent : 0);
		}
	}
}

int link_init(void)
{
	int ret;

	if (!device_is_ready(uart))
		return -ENODEV;
	/* random, non-zero, fixed for this boot: lets the host detect reboots */
	do {
		boot_id = sys_rand8_get();
	} while (!boot_id);

	ret = uart_irq_callback_user_data_set(uart, uart_isr, NULL);
	if (ret)
		return ret;
	uart_irq_rx_enable(uart);
	return 0;
}

static int queue(const uint8_t *data, size_t len)
{
	if (ring_buf_space_get(&tx_ring) < len)
		return -ENOSPC;
	ring_buf_put(&tx_ring, data, len);
	uart_irq_tx_enable(uart);
	return 0;
}

int link_send_raw(const uint8_t *data, size_t len)
{
	int ret;

	k_mutex_lock(&tx_lock, K_FOREVER);
	ret = queue(data, len);
	k_mutex_unlock(&tx_lock);
	return ret;
}

int link_send(uint8_t type, const uint8_t *payload, uint8_t len)
{
	struct mcb_frame f = {
		.type = type,
		.len = len,
		.boot_id = boot_id,
	};
	uint8_t wire[MCB_MAX_WIRE];
	int n, ret;

	if (len > MCB_MAX_PAYLOAD)
		return -EINVAL;
	memcpy(f.payload, payload, len);

	k_mutex_lock(&tx_lock, K_FOREVER);
	f.seq = tx_seq++;	/* advances even if dropped: the host sees the gap */
	f.timestamp_us = (uint32_t)k_ticks_to_us_floor64(k_uptime_ticks());
	n = mcb_frame_encode(&f, wire);
	ret = n < 0 ? -EINVAL : queue(wire, n);
	k_mutex_unlock(&tx_lock);
	return ret;
}

int link_recv(struct mcb_frame *f, k_timeout_t timeout)
{
	uint8_t b;

	for (;;) {
		while (ring_buf_get(&rx_ring, &b, 1)) {
			if (mcb_rx_byte(&rx, b, f) == MCB_RX_FRAME)
				return 0;
			/* CRC/framing errors and overruns: frame ignored, no reply */
		}
		if (k_sem_take(&rx_sem, timeout))
			return -EAGAIN;
	}
}
