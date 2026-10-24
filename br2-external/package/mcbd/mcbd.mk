################################################################################
#
# mcbd
#
################################################################################

MCBD_VERSION = 1.0
MCBD_SITE = $(BR2_EXTERNAL_MCU_BRIDGE_PATH)/../daemon
MCBD_SITE_METHOD = local
MCBD_LICENSE = GPL-2.0-only

define MCBD_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) $(TARGET_CONFIGURE_OPTS) -C $(@D)
endef

define MCBD_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/mcbd $(TARGET_DIR)/usr/sbin/mcbd
endef

define MCBD_INSTALL_INIT_SYSV
	$(INSTALL) -D -m 0755 $(MCBD_PKGDIR)/S50mcbd $(TARGET_DIR)/etc/init.d/S50mcbd
endef

$(eval $(generic-package))
