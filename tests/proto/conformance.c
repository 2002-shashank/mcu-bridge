// SPDX-License-Identifier: GPL-2.0-only OR MIT
/*
 * Host harness exercising protocol/mcb_proto.h, compared against the Python
 * implementation by tests/proto/test_proto.py.
 *
 *   conformance crc      stdin bytes -> "%08x\n"
 *   conformance encode   lines "type boot_id seq ts payloadhex|-" -> wire hex per line
 *   conformance decode   stdin byte stream -> one line per receiver event:
 *                        FRAME type boot_id seq ts payloadhex|- lost reset
 *                        CRC | FRAMING | OVERRUN
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "mcb_proto.h"

uint32_t mcb_crc32(const uint8_t *data, size_t len)
{
	uint32_t crc = 0xffffffff;

	while (len--) {
		crc ^= *data++;
		for (int k = 0; k < 8; k++)
			crc = (crc >> 1) ^ (0xedb88320 & -(crc & 1));
	}
	return ~crc;
}

static size_t read_all(uint8_t **out)
{
	size_t cap = 4096, n = 0, r;
	uint8_t *buf = malloc(cap);

	while ((r = fread(buf + n, 1, cap - n, stdin)) > 0) {
		n += r;
		if (n == cap)
			buf = realloc(buf, cap *= 2);
	}
	*out = buf;
	return n;
}

static void print_hex(const uint8_t *p, size_t n)
{
	if (!n)
		putchar('-');
	for (size_t i = 0; i < n; i++)
		printf("%02x", p[i]);
}

static int do_encode(void)
{
	char line[512], hex[256];
	unsigned int type, boot_id;
	unsigned long seq, ts;

	while (fgets(line, sizeof(line), stdin)) {
		struct mcb_frame f = { 0 };
		uint8_t wire[MCB_MAX_WIRE];
		int n;

		if (sscanf(line, "%u %u %lu %lu %255s", &type, &boot_id, &seq, &ts, hex) != 5)
			return 1;
		f.type = type;
		f.boot_id = boot_id;
		f.seq = seq;
		f.timestamp_us = ts;
		if (strcmp(hex, "-")) {
			size_t len = strlen(hex) / 2;

			if (len > MCB_MAX_PAYLOAD)
				return 1;
			for (size_t i = 0; i < len; i++)
				sscanf(hex + 2 * i, "%2hhx", &f.payload[i]);
			f.len = len;
		}
		n = mcb_frame_encode(&f, wire);
		if (n < 0)
			return 1;
		print_hex(wire, n);
		putchar('\n');
	}
	return 0;
}

static int do_decode(void)
{
	static const char *const names[] = {
		[MCB_RX_CRC_ERR] = "CRC",
		[MCB_RX_FRAMING_ERR] = "FRAMING",
		[MCB_RX_OVERRUN] = "OVERRUN",
	};
	struct mcb_rx rx = { 0 };
	struct mcb_seq seq = { 0 };
	struct mcb_frame f;
	uint8_t *in;
	size_t n = read_all(&in);

	for (size_t i = 0; i < n; i++) {
		enum mcb_rx_status st = mcb_rx_byte(&rx, in[i], &f);
		bool reset;
		uint32_t lost;

		if (st == MCB_RX_NONE)
			continue;
		if (st != MCB_RX_FRAME) {
			printf("%s\n", names[st]);
			continue;
		}
		lost = mcb_seq_update(&seq, &f, &reset);
		printf("FRAME %u %u %u %u ", f.type, f.boot_id, f.seq, f.timestamp_us);
		print_hex(f.payload, f.len);
		printf(" %u %d\n", lost, reset);
	}
	free(in);
	return 0;
}

int main(int argc, char **argv)
{
	if (argc == 2 && !strcmp(argv[1], "crc")) {
		uint8_t *in;
		size_t n = read_all(&in);

		printf("%08x\n", mcb_crc32(in, n));
		free(in);
		return 0;
	}
	if (argc == 2 && !strcmp(argv[1], "encode"))
		return do_encode();
	if (argc == 2 && !strcmp(argv[1], "decode"))
		return do_decode();
	fprintf(stderr, "usage: %s crc|encode|decode\n", argv[0]);
	return 2;
}
