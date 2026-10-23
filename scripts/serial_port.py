"""Serial port helpers shared by the bridge and the firmware test."""
import fcntl, os, struct, termios, time, tty


def open_tty(path):
    """Open a serial port raw at 115200 8N1, like `stty raw 115200`."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd)  # no line editing, echo, CR/LF mapping or signal chars
    attr = termios.tcgetattr(fd)
    attr[2] |= termios.CLOCAL | termios.CREAD  # ignore modem lines, enable receiver
    attr[2] &= ~(termios.CSTOPB | termios.PARENB | termios.CRTSCTS)
    attr[4] = attr[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, attr)
    termios.tcflush(fd, termios.TCIOFLUSH)  # drop bytes buffered before we opened
    return fd


def pulse_reset(fd):
    """EN low via RTS while DTR stays deasserted (GPIO0 high: normal boot)."""
    set_bits = lambda req, bits: fcntl.ioctl(fd, req, struct.pack("I", bits))
    set_bits(termios.TIOCMBIC, termios.TIOCM_DTR)
    set_bits(termios.TIOCMBIS, termios.TIOCM_RTS)
    time.sleep(0.1)
    set_bits(termios.TIOCMBIC, termios.TIOCM_RTS)
