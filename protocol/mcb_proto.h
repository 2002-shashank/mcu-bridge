/* SPDX-License-Identifier: GPL-2.0-only OR MIT */
/*
 * mcu-bridge wire protocol, shared by the Linux driver, the Zephyr firmware
 * and the host conformance test. See docs/protocol.md.
 *
 * Wire format: COBS(frame) followed by a 0x00 delimiter. Decoded frame:
 *
 *   0  u8   version (MCB_VERSION)
 *   1  u8   type    (enum mcb_type)
 *   2  u8   len     payload length, 0..MCB_MAX_PAYLOAD
 *   3  u8   boot_id sender's random 1..255, fixed for one boot
 *   4  le32 seq     per-direction sequence number
 *   8  le32 timestamp_us, sender's monotonic clock (wraps every ~71.6 min)
 *  12  len  payload
 *  12+len   le32 crc32 (IEEE 802.3) over bytes 0 .. 11+len
 *
 * Multi-byte fields are little-endian and always accessed through the
 * mcb_get/put helpers; frames are never cast to packed structs.
 *
 * Each user provides mcb_crc32(), returning the standard CRC-32 (check value
 * 0xcbf43926 for "123456789"): ~crc32_le(~0, p, n) in the kernel,
 * crc32_ieee(p, n) in Zephyr.
 */
#ifndef MCB_PROTO_H
#define MCB_PROTO_H

#ifdef __KERNEL__
#include <linux/string.h>
#include <linux/types.h>
#else
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#endif

#define MCB_VERSION		1
#define MCB_HDR_LEN		12
#define MCB_CRC_LEN		4
#define MCB_MAX_PAYLOAD		64
#define MCB_MIN_FRAME		(MCB_HDR_LEN + MCB_CRC_LEN)
#define MCB_MAX_FRAME		(MCB_MIN_FRAME + MCB_MAX_PAYLOAD)
/* COBS adds one code byte per started 254-byte block */
#define MCB_MAX_COBS		(MCB_MAX_FRAME + MCB_MAX_FRAME / 254 + 1)
/* encoded frame plus the 0x00 delimiter */
#define MCB_MAX_WIRE		(MCB_MAX_COBS + 1)

enum mcb_type {
	MCB_TYPE_DATA		= 0x01,	/* MCU -> host: n x le32 signed channel values */
	MCB_TYPE_HELLO		= 0x02,	/* MCU -> host: first frame after boot */
	MCB_TYPE_PING		= 0x10,	/* host -> MCU: 0..8 byte token */
	MCB_TYPE_PONG		= 0x11,	/* MCU -> host: le32 req_seq + echoed token */
	MCB_TYPE_SET_RATE	= 0x12,	/* host -> MCU: le16 rate_hz */
	MCB_TYPE_ACK		= 0x13,	/* MCU -> host: le32 req_seq, u8 req_type, u8 status */
};

/* HELLO payload: u8 proto_version, u8 reserved, le16 rate_hz, le32 fw_version */
#define MCB_HELLO_LEN		8
#define MCB_PING_MAX_TOKEN	8
#define MCB_ACK_LEN		6
#define MCB_DATA_MAX_CHANNELS	(MCB_MAX_PAYLOAD / 4)
#define MCB_RATE_MIN_HZ		1
#define MCB_RATE_MAX_HZ		200

enum mcb_status {
	MCB_STATUS_OK		= 0,
	MCB_STATUS_RANGE	= 1,	/* argument out of range */
	MCB_STATUS_UNSUPPORTED	= 2,	/* unknown command type */
};

/* In-memory frame; the wire layout is produced by mcb_frame_encode() */
struct mcb_frame {
	uint8_t type;
	uint8_t len;
	uint8_t boot_id;
	uint32_t seq;
	uint32_t timestamp_us;
	uint8_t payload[MCB_MAX_PAYLOAD];
};

uint32_t mcb_crc32(const uint8_t *data, size_t len);

static inline uint16_t mcb_get_le16(const uint8_t *p)
{
	return (uint16_t)(p[0] | p[1] << 8);
}

static inline uint32_t mcb_get_le32(const uint8_t *p)
{
	return (uint32_t)p[0] | (uint32_t)p[1] << 8 |
	       (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static inline void mcb_put_le16(uint8_t *p, uint16_t v)
{
	p[0] = (uint8_t)v;
	p[1] = (uint8_t)(v >> 8);
}

static inline void mcb_put_le32(uint8_t *p, uint32_t v)
{
	p[0] = (uint8_t)v;
	p[1] = (uint8_t)(v >> 8);
	p[2] = (uint8_t)(v >> 16);
	p[3] = (uint8_t)(v >> 24);
}

/*
 * COBS-encode @n bytes from @in into @out (room for n + n / 254 + 1 bytes).
 * The result contains no 0x00; the delimiter is not appended.
 */
static inline size_t mcb_cobs_encode(const uint8_t *in, size_t n, uint8_t *out)
{
	size_t code_at = 0, o = 1;
	uint8_t code = 1;
	size_t i;

	for (i = 0; i < n; i++) {
		if (in[i]) {
			out[o++] = in[i];
			code++;
		}
		if (!in[i] || code == 0xff) {
			out[code_at] = code;
			code = 1;
			/* a full block ending the input needs no trailing empty block */
			if (in[i] && i + 1 == n)
				return o;
			code_at = o++;
		}
	}
	out[code_at] = code;
	return o;
}

/* Decode @n COBS bytes (no delimiter). Returns the decoded length or -1. */
static inline int mcb_cobs_decode(const uint8_t *in, size_t n, uint8_t *out, size_t cap)
{
	size_t i = 0, o = 0;

	while (i < n) {
		uint8_t code = in[i++];

		if (!code || (size_t)code - 1 > n - i || (size_t)code - 1 > cap - o)
			return -1;
		memcpy(out + o, in + i, code - 1);
		i += code - 1;
		o += code - 1;
		if (code != 0xff && i < n) {
			if (o == cap)
				return -1;
			out[o++] = 0;
		}
	}
	return (int)o;
}

/* Encode @f into @wire (MCB_MAX_WIRE bytes). Returns bytes written or -1. */
static inline int mcb_frame_encode(const struct mcb_frame *f, uint8_t *wire)
{
	uint8_t raw[MCB_MAX_FRAME];
	size_t n;

	if (f->len > MCB_MAX_PAYLOAD)
		return -1;
	raw[0] = MCB_VERSION;
	raw[1] = f->type;
	raw[2] = f->len;
	raw[3] = f->boot_id;
	mcb_put_le32(raw + 4, f->seq);
	mcb_put_le32(raw + 8, f->timestamp_us);
	memcpy(raw + MCB_HDR_LEN, f->payload, f->len);
	n = MCB_HDR_LEN + f->len;
	mcb_put_le32(raw + n, mcb_crc32(raw, n));
	n = mcb_cobs_encode(raw, n + MCB_CRC_LEN, wire);
	wire[n++] = 0;
	return (int)n;
}

enum mcb_rx_status {
	MCB_RX_NONE,		/* need more bytes */
	MCB_RX_FRAME,		/* valid frame stored */
	MCB_RX_CRC_ERR,		/* frame corrupted */
	MCB_RX_FRAMING_ERR,	/* bad COBS, too short, or intact but malformed */
	MCB_RX_OVERRUN,		/* no delimiter within MCB_MAX_COBS bytes */
};

/*
 * Classify a COBS frame (delimiter stripped). The CRC is checked against the
 * decoded size before version/len are trusted, so corrupted header, payload
 * or CRC bytes count as CRC_ERR. FRAMING_ERR means the frame is structurally
 * broken: corrupted COBS overhead bytes, or a malformed frame from the sender.
 */
static inline enum mcb_rx_status mcb_frame_decode(const uint8_t *cobs, size_t n,
						  struct mcb_frame *f)
{
	uint8_t raw[MCB_MAX_FRAME];
	int len = mcb_cobs_decode(cobs, n, raw, sizeof(raw));
	size_t body;

	if (len < MCB_MIN_FRAME)
		return MCB_RX_FRAMING_ERR;
	body = len - MCB_CRC_LEN;
	if (mcb_get_le32(raw + body) != mcb_crc32(raw, body))
		return MCB_RX_CRC_ERR;
	if (raw[0] != MCB_VERSION || (size_t)raw[2] != body - MCB_HDR_LEN)
		return MCB_RX_FRAMING_ERR;

	f->type = raw[1];
	f->len = raw[2];
	f->boot_id = raw[3];
	f->seq = mcb_get_le32(raw + 4);
	f->timestamp_us = mcb_get_le32(raw + 8);
	memcpy(f->payload, raw + MCB_HDR_LEN, f->len);
	return MCB_RX_FRAME;
}

/* Byte-stream receiver. Zero-initialize before use. */
struct mcb_rx {
	uint8_t buf[MCB_MAX_COBS];
	size_t len;
	bool discarding;	/* after an overrun, skip to the next delimiter */
};

/*
 * Feed one received byte. Empty frames (repeated 0x00) are ignored, so a
 * sender may emit 0x00 at any time to flush a receiver's partial frame.
 */
static inline enum mcb_rx_status mcb_rx_byte(struct mcb_rx *rx, uint8_t b,
					     struct mcb_frame *f)
{
	size_t n;

	if (b) {
		if (rx->discarding)
			return MCB_RX_NONE;
		if (rx->len == sizeof(rx->buf)) {
			rx->discarding = true;
			rx->len = 0;
			return MCB_RX_OVERRUN;
		}
		rx->buf[rx->len++] = b;
		return MCB_RX_NONE;
	}
	if (rx->discarding) {
		rx->discarding = false;
		return MCB_RX_NONE;
	}
	n = rx->len;
	rx->len = 0;
	if (!n)
		return MCB_RX_NONE;
	return mcb_frame_decode(rx->buf, n, f);
}

/* Per-direction sequence tracking. Zero-initialize before use. */
struct mcb_seq {
	bool synced;
	uint8_t boot_id;
	uint32_t next;
};

/*
 * Account for a valid frame. Returns how many frames were lost before it.
 * Sets *reset when the sender restarted: its boot_id changed (works even
 * when HELLO was lost, e.g. during USB enumeration after a replug), a HELLO,
 * or seq moving backwards (same boot_id by chance). The signed difference
 * keeps the gap correct across the 2^32 wrap.
 */
static inline uint32_t mcb_seq_update(struct mcb_seq *s, const struct mcb_frame *f,
				      bool *reset)
{
	int32_t d = (int32_t)(f->seq - s->next);
	uint32_t lost = 0;

	*reset = s->synced && (f->boot_id != s->boot_id || d < 0);
	*reset |= f->type == MCB_TYPE_HELLO;
	if (s->synced && !*reset)
		lost = (uint32_t)d;
	s->synced = true;
	s->boot_id = f->boot_id;
	s->next = f->seq + 1;
	return lost;
}

#endif /* MCB_PROTO_H */
