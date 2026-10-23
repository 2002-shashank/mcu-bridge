# SPDX-License-Identifier: GPL-2.0-only
# Local out-of-tree build (the image build uses br2-external/package/mcb-bridge):
#   make KDIR=<kernel build dir> ARCH=arm64 CROSS_COMPILE=<prefix> [W=1] [C=1]
KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
