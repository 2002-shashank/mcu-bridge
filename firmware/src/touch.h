/* SPDX-License-Identifier: Apache-2.0 */
#ifndef MCB_TOUCH_H
#define MCB_TOUCH_H

#include <stdbool.h>
#include <stdint.h>

struct touch_sample {
	bool touched;
	int32_t x;		/* raw 12-bit, -1 when not touched */
	int32_t y;		/* raw 12-bit, -1 when not touched */
	int32_t pressure;	/* raw Z1, 0 when not touched */
};

int touch_init(void);
int touch_read(struct touch_sample *s);

#endif
