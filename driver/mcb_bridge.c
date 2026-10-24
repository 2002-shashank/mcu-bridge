// SPDX-License-Identifier: GPL-2.0-only
/*
 * mcu-bridge: serdev driver for an ESP32 streaming CRC-32 framed samples over
 * a UART (docs/protocol.md). Exposes the latest sample, link counters and
 * PING/SET_RATE commands through sysfs.
 *
 * sysfs holds the latest sample only; readers poll() the "sample" attribute and
 * get one consistent snapshot including its CLOCK_MONOTONIC receive time, so
 * user space decides how old is too old. "link_up" is notified on both edges:
 * a delayed work item, pushed back by every valid frame, fires after
 * MCB_LINK_TIMEOUT of silence (sysfs_notify() can sleep, so not a timer).
 *
 * Locking: receive_buf() runs from the tty flip-buffer work item (process
 * context) and is serialized by the tty port, so the frame parser (rx, seq)
 * is only touched there. @lock protects everything sysfs reads (counters,
 * latest sample, pending command); nothing takes it from interrupt context,
 * hence plain spin_lock(). @cmd_lock serializes commands, which sleep while
 * waiting for the response; take it before @lock.
 */
#include <linux/completion.h>
#include <linux/crc32.h>
#include <linux/jiffies.h>
#include <linux/ktime.h>
#include <linux/math64.h>
#include <linux/mod_devicetable.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/property.h>
#include <linux/random.h>
#include <linux/serdev.h>
#include <linux/spinlock.h>
#include <linux/sysfs.h>
#include <linux/workqueue.h>

#include "mcb_proto.h"

#define MCB_DEFAULT_BAUD	115200
#define MCB_TX_TIMEOUT		msecs_to_jiffies(100)
#define MCB_CMD_TIMEOUT		msecs_to_jiffies(500)
#define MCB_LINK_TIMEOUT	(3 * HZ)

struct mcb_stats {
	u64 rx_frames;
	u64 crc_errors;
	u64 framing_errors;
	u64 overruns;
	u64 dropped_frames;
	u64 resets;
};

struct mcb_dev {
	struct serdev_device *serdev;

	/* receive path only (serialized by the tty port) */
	struct mcb_rx rx;
	struct mcb_seq seq;
	bool rx_synced;			/* a delimiter seen since the port opened */

	spinlock_t lock;		/* fields below up to cmd_lock */
	struct mcb_stats stats;
	s32 channels[MCB_DATA_MAX_CHANNELS];
	unsigned int nchannels;
	u32 sample_seq;
	u32 sample_ts;
	u8 sample_boot_id;
	u64 sample_rx_ns;		/* ktime_get_ns() at receipt */
	u64 sample_count;		/* DATA frames accepted since probe */
	u8 peer_boot_id;
	u32 fw_version;
	u16 rate_hz;			/* 0: unknown */
	unsigned long last_rx;		/* jiffies of the last valid frame */
	bool link_up;			/* cleared by link_work */
	/* command in flight, matched by req_seq */
	bool cmd_pending;
	u32 cmd_seq;
	u8 cmd_type;
	u16 cmd_rate;			/* SET_RATE argument, applied on ACK */
	u8 cmd_status;
	ktime_t cmd_sent;
	s64 ping_rtt_ns;		/* -1: none yet */

	struct mutex cmd_lock;		/* one command in flight; fields below */
	struct completion cmd_done;
	u32 tx_seq;
	u8 boot_id;
	u64 ping_token;

	struct delayed_work link_work;	/* fires after MCB_LINK_TIMEOUT of silence */
};

u32 mcb_crc32(const u8 *data, size_t len)
{
	return ~crc32_le(~0, data, len);
}

static u32 mcb_now_us(void)
{
	return (u32)ktime_to_us(ktime_get());
}

/* Caller holds mcb->lock. */
static void mcb_handle_response(struct mcb_dev *mcb, const struct mcb_frame *f)
{
	u32 req_seq;

	if (f->len < 4)
		return;
	req_seq = mcb_get_le32(f->payload);
	if (!mcb->cmd_pending || req_seq != mcb->cmd_seq)
		return;		/* stale: its command already timed out */

	if (f->type == MCB_TYPE_PONG) {
		mcb->ping_rtt_ns = ktime_to_ns(ktime_sub(ktime_get(), mcb->cmd_sent));
		mcb->cmd_status = MCB_STATUS_OK;
	} else if (f->len >= MCB_ACK_LEN && f->payload[4] == mcb->cmd_type) {
		mcb->cmd_status = f->payload[5];
		if (mcb->cmd_type == MCB_TYPE_SET_RATE && mcb->cmd_status == MCB_STATUS_OK)
			mcb->rate_hz = mcb->cmd_rate;
	} else {
		return;
	}
	mcb->cmd_pending = false;
	complete(&mcb->cmd_done);
}

static void mcb_link_work(struct work_struct *work)
{
	struct mcb_dev *mcb = container_of(to_delayed_work(work), struct mcb_dev, link_work);
	bool down;

	spin_lock(&mcb->lock);
	/* a frame may have arrived while we were queued; it re-armed us */
	down = mcb->link_up && !time_before(jiffies, mcb->last_rx + MCB_LINK_TIMEOUT);
	if (down)
		mcb->link_up = false;
	spin_unlock(&mcb->lock);

	if (down)
		sysfs_notify(&mcb->serdev->dev.kobj, NULL, "link_up");
}

static void mcb_cancel_link_work(void *data)
{
	struct mcb_dev *mcb = data;

	cancel_delayed_work_sync(&mcb->link_work);
}

static void mcb_handle_frame(struct mcb_dev *mcb, const struct mcb_frame *f)
{
	bool reset, came_up, notify = false;
	unsigned int i;
	u32 lost;

	lost = mcb_seq_update(&mcb->seq, f, &reset);

	spin_lock(&mcb->lock);
	mcb->stats.rx_frames++;
	mcb->stats.dropped_frames += lost;
	if (reset) {
		mcb->stats.resets++;
		mcb->rate_hz = 0;	/* firmware default unknown until HELLO */
	}
	mcb->peer_boot_id = f->boot_id;
	mcb->last_rx = jiffies;
	came_up = !mcb->link_up;
	mcb->link_up = true;

	switch (f->type) {
	case MCB_TYPE_DATA:
		if (!f->len || f->len % 4)
			break;
		mcb->nchannels = f->len / 4;
		for (i = 0; i < mcb->nchannels; i++)
			mcb->channels[i] = (s32)mcb_get_le32(f->payload + 4 * i);
		mcb->sample_seq = f->seq;
		mcb->sample_ts = f->timestamp_us;
		mcb->sample_boot_id = f->boot_id;
		mcb->sample_rx_ns = ktime_get_ns();
		mcb->sample_count++;
		notify = true;
		break;
	case MCB_TYPE_HELLO:
		if (f->len >= MCB_HELLO_LEN) {
			mcb->rate_hz = mcb_get_le16(f->payload + 2);
			mcb->fw_version = mcb_get_le32(f->payload + 4);
		}
		break;
	case MCB_TYPE_PONG:
	case MCB_TYPE_ACK:
		mcb_handle_response(mcb, f);
		break;
	}
	spin_unlock(&mcb->lock);

	mod_delayed_work(system_wq, &mcb->link_work, MCB_LINK_TIMEOUT);
	if (came_up)
		sysfs_notify(&mcb->serdev->dev.kobj, NULL, "link_up");
	if (notify) {
		sysfs_notify(&mcb->serdev->dev.kobj, NULL, "channels");
		sysfs_notify(&mcb->serdev->dev.kobj, NULL, "sample");
	}
}

static size_t mcb_receive_buf(struct serdev_device *serdev, const u8 *buf, size_t count)
{
	struct mcb_dev *mcb = serdev_device_get_drvdata(serdev);
	struct mcb_frame f;
	size_t i = 0;

	/*
	 * Opened mid-stream (probe, rebind): the first bytes are usually the tail
	 * of a frame. Hunt for the first delimiter instead of counting that as a
	 * framing error; at most one frame is lost, before seq tracking starts.
	 */
	if (!mcb->rx_synced) {
		const u8 *d = memchr(buf, 0, count);

		if (!d)
			return count;
		i = d - buf + 1;
		mcb->rx_synced = true;
	}

	for (; i < count; i++) {
		enum mcb_rx_status st = mcb_rx_byte(&mcb->rx, buf[i], &f);

		switch (st) {
		case MCB_RX_NONE:
			break;
		case MCB_RX_FRAME:
			mcb_handle_frame(mcb, &f);
			break;
		default:
			spin_lock(&mcb->lock);
			if (st == MCB_RX_CRC_ERR)
				mcb->stats.crc_errors++;
			else if (st == MCB_RX_FRAMING_ERR)
				mcb->stats.framing_errors++;
			else
				mcb->stats.overruns++;
			spin_unlock(&mcb->lock);
			dev_dbg(&serdev->dev, "rx error %d\n", st);
			break;
		}
	}
	return count;
}

static const struct serdev_device_ops mcb_serdev_ops = {
	.receive_buf = mcb_receive_buf,
	.write_wakeup = serdev_device_write_wakeup,
};

/*
 * Send a command and wait for its PONG/ACK. Returns the response status or a
 * negative errno. Caller holds cmd_lock.
 */
static int mcb_command(struct mcb_dev *mcb, u8 type, const u8 *payload, u8 plen)
{
	struct mcb_frame f = {
		.type = type,
		.len = plen,
		.boot_id = mcb->boot_id,
		.seq = mcb->tx_seq++,
		.timestamp_us = mcb_now_us(),
	};
	u8 wire[MCB_MAX_WIRE + 1];
	long left;
	int n, len;

	lockdep_assert_held(&mcb->cmd_lock);
	memcpy(f.payload, payload, plen);
	/* leading 0x00 flushes any partial frame in the ESP32's receiver */
	wire[0] = 0;
	n = mcb_frame_encode(&f, wire + 1);
	if (n < 0)
		return -EINVAL;

	reinit_completion(&mcb->cmd_done);
	spin_lock(&mcb->lock);
	mcb->cmd_pending = true;
	mcb->cmd_seq = f.seq;
	mcb->cmd_type = type;
	mcb->cmd_sent = ktime_get();
	spin_unlock(&mcb->lock);

	len = n + 1;
	n = serdev_device_write(mcb->serdev, wire, len, MCB_TX_TIMEOUT);
	if (n != len) {
		n = n < 0 ? n : -EIO;
		goto cancel;
	}

	left = wait_for_completion_interruptible_timeout(&mcb->cmd_done, MCB_CMD_TIMEOUT);
	if (left > 0)
		return mcb->cmd_status;
	n = left ? left : -ETIMEDOUT;
cancel:
	spin_lock(&mcb->lock);
	mcb->cmd_pending = false;
	spin_unlock(&mcb->lock);
	return n;
}

static int mcb_status_to_errno(int status)
{
	switch (status) {
	case MCB_STATUS_OK:
		return 0;
	case MCB_STATUS_RANGE:
		return -ERANGE;
	case MCB_STATUS_UNSUPPORTED:
		return -EOPNOTSUPP;
	default:
		return status < 0 ? status : -EPROTO;
	}
}

/* sysfs ------------------------------------------------------------------ */

#define MCB_STAT_ATTR(name)						\
static ssize_t name##_show(struct device *dev,				\
			   struct device_attribute *attr, char *buf)	\
{									\
	struct mcb_dev *mcb = dev_get_drvdata(dev);			\
	u64 v;								\
									\
	spin_lock(&mcb->lock);					\
	v = mcb->stats.name;						\
	spin_unlock(&mcb->lock);					\
	return sysfs_emit(buf, "%llu\n", v);				\
}									\
static DEVICE_ATTR_RO(name)

MCB_STAT_ATTR(rx_frames);
MCB_STAT_ATTR(crc_errors);
MCB_STAT_ATTR(framing_errors);
MCB_STAT_ATTR(overruns);
MCB_STAT_ATTR(dropped_frames);
MCB_STAT_ATTR(resets);

static ssize_t channels_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct mcb_dev *mcb = dev_get_drvdata(dev);
	s32 ch[MCB_DATA_MAX_CHANNELS];
	unsigned int i, n;
	ssize_t len = 0;

	/* snapshot under the lock, format outside it */
	spin_lock(&mcb->lock);
	n = mcb->nchannels;
	memcpy(ch, mcb->channels, sizeof(ch));
	spin_unlock(&mcb->lock);

	if (!n)
		return -ENODATA;
	for (i = 0; i < n; i++)
		len += sysfs_emit_at(buf, len, "%d%c", ch[i], i + 1 < n ? ' ' : '\n');
	return len;
}
static DEVICE_ATTR_RO(channels);

/* "boot_id seq timestamp_us rx_ns count v0 v1 ...", one consistent snapshot */
static ssize_t sample_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct mcb_dev *mcb = dev_get_drvdata(dev);
	s32 ch[MCB_DATA_MAX_CHANNELS];
	u32 seq, ts;
	u64 rx_ns, count;
	unsigned int i, n;
	ssize_t len;
	u8 boot_id;

	spin_lock(&mcb->lock);
	n = mcb->nchannels;
	memcpy(ch, mcb->channels, sizeof(ch));
	seq = mcb->sample_seq;
	ts = mcb->sample_ts;
	boot_id = mcb->sample_boot_id;
	rx_ns = mcb->sample_rx_ns;
	count = mcb->sample_count;
	spin_unlock(&mcb->lock);

	if (!n)
		return -ENODATA;
	len = sysfs_emit(buf, "%u %u %u %llu %llu", boot_id, seq, ts, rx_ns, count);
	for (i = 0; i < n; i++)
		len += sysfs_emit_at(buf, len, " %d", ch[i]);
	len += sysfs_emit_at(buf, len, "\n");
	return len;
}
static DEVICE_ATTR_RO(sample);

#define MCB_U32_ATTR(name, field)					\
static ssize_t name##_show(struct device *dev,				\
			   struct device_attribute *attr, char *buf)	\
{									\
	struct mcb_dev *mcb = dev_get_drvdata(dev);			\
	u32 v;								\
									\
	spin_lock(&mcb->lock);					\
	v = mcb->field;							\
	spin_unlock(&mcb->lock);					\
	return sysfs_emit(buf, "%u\n", v);				\
}									\
static DEVICE_ATTR_RO(name)

MCB_U32_ATTR(sample_seq, sample_seq);
MCB_U32_ATTR(sample_timestamp_us, sample_ts);
MCB_U32_ATTR(peer_boot_id, peer_boot_id);

static ssize_t fw_version_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct mcb_dev *mcb = dev_get_drvdata(dev);
	u32 v;

	spin_lock(&mcb->lock);
	v = mcb->fw_version;
	spin_unlock(&mcb->lock);
	return sysfs_emit(buf, "%u.%u.%u\n", v >> 16, (v >> 8) & 0xff, v & 0xff);
}
static DEVICE_ATTR_RO(fw_version);

static ssize_t link_up_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct mcb_dev *mcb = dev_get_drvdata(dev);
	bool up;

	spin_lock(&mcb->lock);
	up = mcb->link_up;
	spin_unlock(&mcb->lock);
	return sysfs_emit(buf, "%d\n", up);
}
static DEVICE_ATTR_RO(link_up);

static ssize_t rate_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct mcb_dev *mcb = dev_get_drvdata(dev);
	u16 v;

	spin_lock(&mcb->lock);
	v = mcb->rate_hz;
	spin_unlock(&mcb->lock);
	return sysfs_emit(buf, "%u\n", v);
}

static ssize_t rate_store(struct device *dev, struct device_attribute *attr,
			  const char *buf, size_t count)
{
	struct mcb_dev *mcb = dev_get_drvdata(dev);
	u8 payload[2];
	u16 hz;
	int ret;

	ret = kstrtou16(buf, 0, &hz);
	if (ret)
		return ret;
	if (hz < MCB_RATE_MIN_HZ || hz > MCB_RATE_MAX_HZ)
		return -EINVAL;
	mcb_put_le16(payload, hz);

	mutex_lock(&mcb->cmd_lock);
	mcb->cmd_rate = hz;	/* published to receive_buf by mcb_command()'s spin_unlock */
	ret = mcb_status_to_errno(mcb_command(mcb, MCB_TYPE_SET_RATE, payload, 2));
	mutex_unlock(&mcb->cmd_lock);
	return ret ?: count;
}
static DEVICE_ATTR_RW(rate);

static ssize_t ping_store(struct device *dev, struct device_attribute *attr,
			  const char *buf, size_t count)
{
	struct mcb_dev *mcb = dev_get_drvdata(dev);
	u8 token[MCB_PING_MAX_TOKEN];
	int ret;

	mutex_lock(&mcb->cmd_lock);
	mcb_put_le32(token, (u32)mcb->ping_token);
	mcb_put_le32(token + 4, (u32)(mcb->ping_token >> 32));
	mcb->ping_token++;
	ret = mcb_status_to_errno(mcb_command(mcb, MCB_TYPE_PING, token, sizeof(token)));
	mutex_unlock(&mcb->cmd_lock);
	return ret ?: count;
}
static DEVICE_ATTR_WO(ping);

static ssize_t ping_rtt_us_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct mcb_dev *mcb = dev_get_drvdata(dev);
	s64 ns;

	spin_lock(&mcb->lock);
	ns = mcb->ping_rtt_ns;
	spin_unlock(&mcb->lock);
	if (ns < 0)
		return -ENODATA;
	return sysfs_emit(buf, "%lld\n", div_s64(ns, NSEC_PER_USEC));
}
static DEVICE_ATTR_RO(ping_rtt_us);

static struct attribute *mcb_attrs[] = {
	&dev_attr_rx_frames.attr,
	&dev_attr_crc_errors.attr,
	&dev_attr_framing_errors.attr,
	&dev_attr_overruns.attr,
	&dev_attr_dropped_frames.attr,
	&dev_attr_resets.attr,
	&dev_attr_channels.attr,
	&dev_attr_sample.attr,
	&dev_attr_sample_seq.attr,
	&dev_attr_sample_timestamp_us.attr,
	&dev_attr_peer_boot_id.attr,
	&dev_attr_fw_version.attr,
	&dev_attr_link_up.attr,
	&dev_attr_rate.attr,
	&dev_attr_ping.attr,
	&dev_attr_ping_rtt_us.attr,
	NULL
};
ATTRIBUTE_GROUPS(mcb);

/* probe / remove ----------------------------------------------------------- */

static int mcb_probe(struct serdev_device *serdev)
{
	struct device *dev = &serdev->dev;
	struct mcb_dev *mcb;
	u32 baud = MCB_DEFAULT_BAUD;
	unsigned int actual;
	int ret;

	mcb = devm_kzalloc(dev, sizeof(*mcb), GFP_KERNEL);
	if (!mcb)
		return -ENOMEM;
	mcb->serdev = serdev;
	spin_lock_init(&mcb->lock);
	mutex_init(&mcb->cmd_lock);
	init_completion(&mcb->cmd_done);
	mcb->ping_rtt_ns = -1;
	mcb->boot_id = get_random_u32_inclusive(1, 255);
	INIT_DELAYED_WORK(&mcb->link_work, mcb_link_work);
	serdev_device_set_drvdata(serdev, mcb);
	serdev_device_set_client_ops(serdev, &mcb_serdev_ops);

	device_property_read_u32(dev, "current-speed", &baud);

	/*
	 * devm releases in reverse order: registered before the port is opened,
	 * the cancel runs after the port is closed, when receive_buf() can no
	 * longer re-arm the work, and before mcb is freed.
	 */
	ret = devm_add_action_or_reset(dev, mcb_cancel_link_work, mcb);
	if (ret)
		return ret;

	/* devm: closed automatically after remove(), before mcb is freed */
	ret = devm_serdev_device_open(dev, serdev);
	if (ret)
		return dev_err_probe(dev, ret, "failed to open serial port\n");

	actual = serdev_device_set_baudrate(serdev, baud);
	if (actual != baud)
		dev_warn(dev, "requested %u baud, got %u\n", baud, actual);
	serdev_device_set_flow_control(serdev, false);
	ret = serdev_device_set_parity(serdev, SERDEV_PARITY_NONE);
	if (ret)
		return dev_err_probe(dev, ret, "failed to set parity\n");

	dev_info(dev, "mcu-bridge at %u baud, boot_id %u\n", baud, mcb->boot_id);
	return 0;
}

static void mcb_remove(struct serdev_device *serdev)
{
	struct mcb_dev *mcb = serdev_device_get_drvdata(serdev);
	struct mcb_stats st;

	/*
	 * sysfs attributes are already gone (the driver core removes dev_groups
	 * first), but receive_buf() runs until devm closes the port after us.
	 */
	spin_lock(&mcb->lock);
	st = mcb->stats;
	spin_unlock(&mcb->lock);
	dev_info(&serdev->dev, "removed: %llu frames, %llu crc errors, %llu dropped\n",
		 st.rx_frames, st.crc_errors, st.dropped_frames);
}

static const struct of_device_id mcb_of_match[] = {
	{ .compatible = "mcubridge,esp32" },
	{ }
};
MODULE_DEVICE_TABLE(of, mcb_of_match);

static struct serdev_device_driver mcb_driver = {
	.probe = mcb_probe,
	.remove = mcb_remove,
	.driver = {
		.name = "mcb_bridge",
		.of_match_table = mcb_of_match,
		.dev_groups = mcb_groups,
	},
};
module_serdev_device_driver(mcb_driver);

MODULE_DESCRIPTION("mcu-bridge ESP32 serdev link");
MODULE_AUTHOR("Shashank <smshashanksm@gmail.com>");
MODULE_LICENSE("GPL");
