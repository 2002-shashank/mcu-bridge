/* SPDX-License-Identifier: Apache-2.0 */
/*
 * XPT2046 resistive-touch controller over raw SPI.
 *
 * One conversion = a command byte (start, 3-bit channel, 12-bit mode,
 * single-ended/differential, power-down bits) followed by two bytes that
 * clock out the 12-bit result, left-aligned after one busy bit.
 * Touch channels run differential (ratiometric against the panel), so they do
 * not depend on the reference voltage. The chip's temperature channels do,
 * and on this board they could not be calibrated, so they are not used.
 */
#include <errno.h>
#include <zephyr/drivers/spi.h>

#include "touch.h"

/* Differential, 12-bit, PD=01 (ADC stays on between conversions of a burst) */
#define XPT_Z1		0xB1
#define XPT_X		0xD1
#define XPT_Y		0x91
/* Same Y command with PD=00: power down afterwards, PENIRQ enabled */
#define XPT_PWRDN	0x90

/* Idle Z1 reads 0, firm presses 400..900 on this panel */
#define TOUCH_THRESHOLD	100

static const struct spi_dt_spec xpt = SPI_DT_SPEC_GET(DT_NODELABEL(xpt),
	SPI_OP_MODE_MASTER | SPI_WORD_SET(8) | SPI_TRANSFER_MSB);

static int xpt_read(uint8_t cmd, int32_t *val)
{
	uint8_t tx[3] = { cmd, 0, 0 };
	uint8_t rx[3];
	const struct spi_buf tb = { .buf = tx, .len = sizeof(tx) };
	const struct spi_buf rb = { .buf = rx, .len = sizeof(rx) };
	const struct spi_buf_set ts = { .buffers = &tb, .count = 1 };
	const struct spi_buf_set rs = { .buffers = &rb, .count = 1 };
	int ret = spi_transceive_dt(&xpt, &ts, &rs);

	if (ret)
		return ret;
	*val = ((rx[1] << 8) | rx[2]) >> 3;
	return 0;
}

static int32_t median3(int32_t a, int32_t b, int32_t c)
{
	if ((a <= b && b <= c) || (c <= b && b <= a))
		return b;
	if ((b <= a && a <= c) || (c <= a && a <= b))
		return a;
	return c;
}

/* Panels are noisy as contact is made: take the median of three reads */
static int xpt_read_median(uint8_t cmd, int32_t *val)
{
	int32_t v[3];
	int ret;

	for (int i = 0; i < 3; i++) {
		ret = xpt_read(cmd, &v[i]);
		if (ret)
			return ret;
	}
	*val = median3(v[0], v[1], v[2]);
	return 0;
}

int touch_init(void)
{
	return spi_is_ready_dt(&xpt) ? 0 : -ENODEV;
}

int touch_read(struct touch_sample *s)
{
	int32_t z1, x, y, unused;
	int ret;

	ret = xpt_read(XPT_Z1, &z1);
	if (!ret)
		ret = xpt_read_median(XPT_X, &x);
	if (!ret)
		ret = xpt_read_median(XPT_Y, &y);
	if (!ret)
		ret = xpt_read(XPT_PWRDN, &unused);	/* result discarded */
	if (ret)
		return ret;

	s->touched = z1 > TOUCH_THRESHOLD;
	s->x = s->touched ? x : -1;
	s->y = s->touched ? y : -1;
	s->pressure = s->touched ? z1 : 0;
	return 0;
}
