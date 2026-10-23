################################################################################
#
# mcb-bridge
#
################################################################################

MCB_BRIDGE_VERSION = 1.0
MCB_BRIDGE_SITE = $(BR2_EXTERNAL_MCU_BRIDGE_PATH)/../driver
MCB_BRIDGE_SITE_METHOD = local
MCB_BRIDGE_LICENSE = GPL-2.0-only
MCB_BRIDGE_MODULE_MAKE_OPTS = MCB_PROTO_DIR=$(BR2_EXTERNAL_MCU_BRIDGE_PATH)/../protocol

$(eval $(kernel-module))
$(eval $(generic-package))
