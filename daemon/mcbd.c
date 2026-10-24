// SPDX-License-Identifier: GPL-2.0-only
/*
 * mcbd: user-space daemon for the mcb_bridge driver. Logs every sample to a
 * size-capped CSV file and link events to syslog.
 *
 * One thread, one poll() loop over:
 *   - <dev>/sample   notified by the driver for each DATA frame
 *   - <dev>/link_up  notified on both link edges
 *   - a signalfd     SIGTERM/SIGINT exit, SIGHUP reopens the CSV, SIGUSR1 logs status
 * and a timeout to the nearest deadline (stale check, stats, device search).
 *
 * sysfs poll(): a file must be read once after open to arm it; after a wakeup it
 * is re-read from offset 0 (pread). sysfs keeps the latest value only, so a
 * reader that falls behind skips samples: the driver's per-sample count tells
 * how many (missed_by_reader), separately from frames lost on the wire
 * (the driver's dropped_frames).
 *
 * Staleness is policy, decided here: the driver reports each sample's
 * CLOCK_MONOTONIC receive time; a sample is stale once it is older than
 * max(3 periods, 100 ms) at the current rate (1 s if the rate is unknown).
 * The driver's link_up (3 s of silence) is the authority on the link itself.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <glob.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/signalfd.h>
#include <sys/stat.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#define DEV_GLOB	"/sys/bus/serial/drivers/mcb_bridge/*/sample"
#define DEF_CSV		"/var/log/mcbd/samples.csv"
#define DEF_CSV_MAX	(1024 * 1024)
#define DEF_STATS_S	10
#define SEARCH_NS	1000000000LL	/* retry interval while no device is bound */
#define STALE_MIN_NS	100000000LL
#define STALE_UNKNOWN_NS 1000000000LL
#define MAX_CH		16

static const char *const counter_names[] = {
	"rx_frames", "crc_errors", "framing_errors", "overruns", "dropped_frames", "resets",
};
#define NCOUNTERS (sizeof(counter_names) / sizeof(counter_names[0]))

struct sample {
	unsigned int boot_id;
	uint32_t seq, ts_us;
	int64_t rx_ns;
	uint64_t count;
	int nch;
	int32_t ch[MAX_CH];
};

struct mcbd {
	/* options */
	const char *csv_path;
	long csv_max;
	int stats_s;

	/* device; dir[0] == 0 while none is bound */
	char dir[256];			/* sysfs device paths are short */
	int sample_fd, link_fd;
	bool link_up, stale, have_sample;
	struct sample last;
	unsigned int rate_hz;		/* 0: unknown */
	uint64_t counters[NCOUNTERS];
	uint64_t missed_by_reader, missed_reported;

	/* output */
	FILE *csv;
	long csv_bytes;
	uint64_t rows;

	int64_t next_search, next_stats;
};

static int64_t now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* Read a sysfs attribute from offset 0; returns length or -errno. */
static int pread_attr(int fd, char *buf, size_t size)
{
	ssize_t n = pread(fd, buf, size - 1, 0);

	if (n < 0)
		return -errno;
	buf[n] = '\0';
	return (int)n;
}

static int read_attr(const char *dir, const char *name, char *buf, size_t size)
{
	char path[PATH_MAX];
	int fd, n;

	snprintf(path, sizeof(path), "%s/%s", dir, name);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -errno;
	n = pread_attr(fd, buf, size);
	close(fd);
	return n;
}

static bool read_u64(const char *dir, const char *name, uint64_t *v)
{
	char buf[32];

	if (read_attr(dir, name, buf, sizeof(buf)) <= 0)
		return false;
	*v = strtoull(buf, NULL, 10);
	return true;
}

static void read_rate(struct mcbd *d)
{
	uint64_t v;

	if (read_u64(d->dir, "rate", &v))
		d->rate_hz = (unsigned int)v;
}

static int64_t stale_after_ns(const struct mcbd *d)
{
	int64_t ns;

	if (!d->rate_hz)
		return STALE_UNKNOWN_NS;
	ns = 3 * 1000000000LL / d->rate_hz;
	return ns > STALE_MIN_NS ? ns : STALE_MIN_NS;
}

static bool parse_sample(const char *buf, struct sample *s)
{
	unsigned long long rx_ns, count;
	const char *p;
	char *end;
	int off;

	if (sscanf(buf, "%u %" SCNu32 " %" SCNu32 " %llu %llu%n",
		   &s->boot_id, &s->seq, &s->ts_us, &rx_ns, &count, &off) != 5)
		return false;
	s->rx_ns = (int64_t)rx_ns;
	s->count = count;
	p = buf + off;
	for (s->nch = 0; s->nch < MAX_CH; s->nch++) {
		long v = strtol(p, &end, 10);

		if (end == p)
			break;
		s->ch[s->nch] = (int32_t)v;
		p = end;
	}
	return s->nch > 0;
}

/* CSV ---------------------------------------------------------------------- */

static void csv_open(struct mcbd *d)
{
	char dir[PATH_MAX], *slash;
	struct stat st;

	snprintf(dir, sizeof(dir), "%s", d->csv_path);
	slash = strrchr(dir, '/');
	if (slash && slash != dir) {
		*slash = '\0';
		mkdir(dir, 0755);
	}
	d->csv = fopen(d->csv_path, "ae");
	if (!d->csv) {
		syslog(LOG_ERR, "cannot open %s: %m", d->csv_path);
		return;
	}
	setvbuf(d->csv, NULL, _IOLBF, 0);
	d->csv_bytes = fstat(fileno(d->csv), &st) ? 0 : (long)st.st_size;
	if (!d->csv_bytes)
		d->csv_bytes = fprintf(d->csv, "mono_ns,boot_id,seq,ts_us,age_us,values\n");
}

static void csv_close(struct mcbd *d)
{
	if (d->csv)
		fclose(d->csv);
	d->csv = NULL;
}

/* Keep one old generation: samples.csv -> samples.csv.1 */
static void csv_rotate(struct mcbd *d)
{
	char old[PATH_MAX];

	csv_close(d);
	snprintf(old, sizeof(old), "%s.1", d->csv_path);
	if (rename(d->csv_path, old))
		syslog(LOG_WARNING, "rotate %s: %m", d->csv_path);
	csv_open(d);
}

static void csv_row(struct mcbd *d, int64_t now, const struct sample *s)
{
	int i, n;

	if (!d->csv)
		return;
	n = fprintf(d->csv, "%" PRId64 ",%u,%" PRIu32 ",%" PRIu32 ",%" PRId64 ",",
		    now, s->boot_id, s->seq, s->ts_us, (now - s->rx_ns) / 1000);
	for (i = 0; i < s->nch; i++)
		n += fprintf(d->csv, i ? " %" PRId32 : "%" PRId32, s->ch[i]);
	n += fprintf(d->csv, "\n");
	d->rows++;
	d->csv_bytes += n;
	if (d->csv_bytes >= d->csv_max)
		csv_rotate(d);
}

/* device ------------------------------------------------------------------- */

static void dev_close(struct mcbd *d, const char *why)
{
	syslog(LOG_WARNING, "device %s gone (%s), searching", d->dir, why);
	close(d->sample_fd);
	close(d->link_fd);
	d->sample_fd = d->link_fd = -1;
	d->dir[0] = '\0';
	d->link_up = d->stale = d->have_sample = false;
}

static void read_link(struct mcbd *d)
{
	char buf[8];
	bool up;

	if (pread_attr(d->link_fd, buf, sizeof(buf)) <= 0) {
		dev_close(d, "link_up read failed");
		return;
	}
	up = buf[0] == '1';
	if (up == d->link_up)
		return;
	d->link_up = up;
	if (up) {
		read_rate(d);
		syslog(LOG_NOTICE, "link up (rate %u Hz)", d->rate_hz);
	} else if (d->have_sample) {
		syslog(LOG_WARNING, "link down: last sample seq %" PRIu32 " is %" PRId64 " ms old",
		       d->last.seq, (now_ns() - d->last.rx_ns) / 1000000);
	} else {
		syslog(LOG_WARNING, "link down");
	}
}

static void read_sample(struct mcbd *d)
{
	struct sample s;
	char buf[256];
	int64_t now;
	int n;

	n = pread_attr(d->sample_fd, buf, sizeof(buf));
	if (n == -ENODATA)
		return;			/* no DATA frame since probe */
	if (n <= 0) {
		dev_close(d, "sample read failed");
		return;
	}
	if (!parse_sample(buf, &s)) {
		syslog(LOG_ERR, "unparsable sample: %s", buf);
		return;
	}
	if (d->have_sample && s.count == d->last.count)
		return;			/* woken by link_up, or nothing new */

	now = now_ns();
	if (d->have_sample) {
		if (s.count > d->last.count + 1)
			d->missed_by_reader += s.count - d->last.count - 1;
		if (s.boot_id != d->last.boot_id) {
			read_rate(d);
			syslog(LOG_NOTICE, "peer rebooted (boot_id %u -> %u)", d->last.boot_id,
			       s.boot_id);
		}
	}
	if (d->stale)
		syslog(LOG_NOTICE, "samples fresh again");
	d->stale = false;
	d->last = s;
	d->have_sample = true;
	csv_row(d, now, &s);
}

static void dev_search(struct mcbd *d)
{
	char path[PATH_MAX], buf[8];
	glob_t g;
	size_t i;

	if (glob(DEV_GLOB, 0, NULL, &g))
		return;
	snprintf(d->dir, sizeof(d->dir), "%s", g.gl_pathv[0]);
	globfree(&g);
	*strrchr(d->dir, '/') = '\0';

	snprintf(path, sizeof(path), "%s/sample", d->dir);
	d->sample_fd = open(path, O_RDONLY | O_CLOEXEC);
	snprintf(path, sizeof(path), "%s/link_up", d->dir);
	d->link_fd = open(path, O_RDONLY | O_CLOEXEC);
	if (d->sample_fd < 0 || d->link_fd < 0) {	/* unbound between glob and open */
		if (d->sample_fd >= 0)
			close(d->sample_fd);
		if (d->link_fd >= 0)
			close(d->link_fd);
		d->sample_fd = d->link_fd = -1;
		d->dir[0] = '\0';
		return;
	}
	for (i = 0; i < NCOUNTERS; i++)
		read_u64(d->dir, counter_names[i], &d->counters[i]);
	d->missed_reported = d->missed_by_reader;
	read_rate(d);

	/* the first read of each file arms poll() on it */
	if (pread_attr(d->link_fd, buf, sizeof(buf)) > 0)
		d->link_up = buf[0] == '1';
	syslog(LOG_NOTICE, "device %s, link %s, rate %u Hz", d->dir,
	       d->link_up ? "up" : "down", d->rate_hz);
	read_sample(d);
}

static void check_stale(struct mcbd *d, int64_t now)
{
	if (!d->have_sample || d->stale || now - d->last.rx_ns < stale_after_ns(d))
		return;
	read_rate(d);		/* a lowered rate makes longer gaps normal */
	if (now - d->last.rx_ns < stale_after_ns(d))
		return;
	d->stale = true;
	syslog(LOG_WARNING, "sample stale: seq %" PRIu32 " is %" PRId64 " ms old (limit %" PRId64
	       " ms), link %s", d->last.seq, (now - d->last.rx_ns) / 1000000,
	       stale_after_ns(d) / 1000000, d->link_up ? "up" : "down");
}

/* Log the counters that moved since the last report. */
static void report_counters(struct mcbd *d)
{
	char msg[512];
	size_t len = 0;
	uint64_t v;
	size_t i;

	for (i = 0; i < NCOUNTERS; i++) {
		if (!read_u64(d->dir, counter_names[i], &v))
			return;		/* device going away; poll() will tell */
		if (i && v != d->counters[i])	/* rx_frames moving is normal */
			len += (size_t)snprintf(msg + len, sizeof(msg) - len, " %s +%" PRIu64,
						counter_names[i], v - d->counters[i]);
		d->counters[i] = v;
	}
	if (d->missed_by_reader != d->missed_reported)
		len += (size_t)snprintf(msg + len, sizeof(msg) - len, " missed_by_reader +%" PRIu64,
					d->missed_by_reader - d->missed_reported);
	d->missed_reported = d->missed_by_reader;
	if (len)
		syslog(LOG_WARNING, "counters:%s", msg);
	read_rate(d);
}

static void log_status(struct mcbd *d, int64_t now)
{
	if (d->dir[0])
		read_rate(d);	/* cached copy may lag a write to "rate" */
	syslog(LOG_INFO, "status: device %s, link %s, sample %s seq %" PRIu32 " count %" PRIu64
	       " age %" PRId64 " ms, rate %u Hz, rows %" PRIu64 ", missed_by_reader %" PRIu64,
	       d->dir[0] ? d->dir : "none", d->link_up ? "up" : "down",
	       !d->have_sample ? "none" : d->stale ? "stale" : "fresh", d->last.seq,
	       d->last.count, d->have_sample ? (now - d->last.rx_ns) / 1000000 : -1,
	       d->rate_hz, d->rows, d->missed_by_reader);
}

static int signal_fd(void)
{
	sigset_t set;

	sigemptyset(&set);
	sigaddset(&set, SIGTERM);
	sigaddset(&set, SIGINT);
	sigaddset(&set, SIGHUP);
	sigaddset(&set, SIGUSR1);
	if (sigprocmask(SIG_BLOCK, &set, NULL))
		return -1;
	return signalfd(-1, &set, SFD_CLOEXEC);
}

static void usage(const char *argv0)
{
	fprintf(stderr,
		"usage: %s [-e] [-o csv] [-m max_bytes] [-s stats_seconds]\n"
		"  -e  also log to stderr\n"
		"  -o  sample CSV (default " DEF_CSV ")\n"
		"  -m  rotate the CSV to .1 at this size (default %d)\n"
		"  -s  counter report interval (default %d)\n",
		argv0, DEF_CSV_MAX, DEF_STATS_S);
	exit(2);
}

int main(int argc, char **argv)
{
	struct mcbd d = {
		.csv_path = DEF_CSV, .csv_max = DEF_CSV_MAX, .stats_s = DEF_STATS_S,
		.sample_fd = -1, .link_fd = -1,
	};
	int logopt = LOG_PID, sfd, opt;

	while ((opt = getopt(argc, argv, "eo:m:s:")) != -1) {
		switch (opt) {
		case 'e':
			logopt |= LOG_PERROR;
			break;
		case 'o':
			d.csv_path = optarg;
			break;
		case 'm':
			d.csv_max = atol(optarg);
			break;
		case 's':
			d.stats_s = atoi(optarg);
			break;
		default:
			usage(argv[0]);
		}
	}
	if (optind != argc || d.csv_max <= 0 || d.stats_s <= 0)
		usage(argv[0]);

	openlog("mcbd", logopt, LOG_DAEMON);
	sfd = signal_fd();
	if (sfd < 0) {
		syslog(LOG_ERR, "signalfd: %m");
		return 1;
	}
	csv_open(&d);
	syslog(LOG_NOTICE, "started, samples to %s", d.csv_path);
	d.next_stats = now_ns() + d.stats_s * 1000000000LL;

	for (;;) {
		struct pollfd pfd[3] = {
			{ .fd = sfd, .events = POLLIN },
			{ .fd = d.sample_fd, .events = POLLPRI },
			{ .fd = d.link_fd, .events = POLLPRI },
		};
		int64_t now = now_ns(), deadline;
		struct signalfd_siginfo si;
		int nfds, timeout;

		if (!d.dir[0] && now >= d.next_search) {
			dev_search(&d);
			d.next_search = now + SEARCH_NS;
		}
		if (d.dir[0])
			check_stale(&d, now);
		if (now >= d.next_stats) {
			if (d.dir[0])
				report_counters(&d);
			d.next_stats = now + d.stats_s * 1000000000LL;
		}

		deadline = d.next_stats;
		if (!d.dir[0] && d.next_search < deadline)
			deadline = d.next_search;
		if (d.dir[0] && d.have_sample && !d.stale &&
		    d.last.rx_ns + stale_after_ns(&d) < deadline)
			deadline = d.last.rx_ns + stale_after_ns(&d);
		timeout = deadline > now ? (int)((deadline - now + 999999) / 1000000) : 0;
		nfds = d.dir[0] ? 3 : 1;

		if (poll(pfd, (nfds_t)nfds, timeout) < 0) {
			if (errno == EINTR)
				continue;
			syslog(LOG_ERR, "poll: %m");
			return 1;
		}

		if ((pfd[0].revents & POLLIN) &&
		    read(sfd, &si, sizeof(si)) == (ssize_t)sizeof(si)) {
			if (si.ssi_signo == SIGHUP) {
				csv_close(&d);
				csv_open(&d);
			} else if (si.ssi_signo == SIGUSR1) {
				log_status(&d, now_ns());
			} else {
				syslog(LOG_NOTICE, "exiting on signal %u, %" PRIu64 " rows",
				       si.ssi_signo, d.rows);
				csv_close(&d);
				return 0;
			}
		}
		/* sysfs reports POLLERR|POLLPRI on notify, and forever once removed */
		if (nfds == 3 && pfd[2].revents)
			read_link(&d);
		if (nfds == 3 && d.dir[0] && pfd[1].revents)
			read_sample(&d);
	}
}
