/* SPDX-License-Identifier: Apache-2.0 */
#ifndef MCB_LINK_H
#define MCB_LINK_H

#include <zephyr/kernel.h>

#include "mcb_proto.h"

int link_init(void);
/* Encode and queue a frame; -ENOSPC drops it whole (seq still advances). */
int link_send(uint8_t type, const uint8_t *payload, uint8_t len);
/* Queue raw bytes, e.g. the 0x00 that flushes the host's receiver at boot. */
int link_send_raw(const uint8_t *data, size_t len);
/* Wait for the next valid frame from the host. */
int link_recv(struct mcb_frame *f, k_timeout_t timeout);

#endif
