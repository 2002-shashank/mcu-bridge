# Built by Buildroot (br2-external/package/mcbd), which passes CC and CFLAGS on
# the command line; the warning flags live in their own variable so they survive.
CFLAGS ?= -O2
WARN := -Wall -Wextra -Wconversion -Wshadow -Werror

mcbd: mcbd.c
	$(CC) $(CFLAGS) $(WARN) $(LDFLAGS) -o $@ $<

clean:
	rm -f mcbd

.PHONY: clean
