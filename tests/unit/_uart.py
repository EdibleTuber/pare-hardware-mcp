# tests/unit/_uart.py
"""A UART receiver, in software, so garbage in these tests is DERIVED.

Every baud-detection test in this repo used to supply its own rates against
synthetic data -- `bytes(range(128, 256))` for "bad" and a hand-typed U-Boot
line for "good". Three defects survived 214 such tests, ten task reviews,
four spec reviewers and a whole-branch review, and the bench found all three
in one session. The common cause was not any one weak assertion: it was that
nothing in the suite had ever seen what a WRONG RATE actually produces.

So this module produces it. `to_waveform` serialises bytes into UART line
levels at one rate, and `receive` runs a textbook start-bit-hunting receiver
over that waveform at ANOTHER rate -- the same thing a misconfigured FTDI
does, with the same result. `misframed()` is the fixture the tests use: a
real-shaped Rockchip boot log transmitted at 1 500 000 and received at
whatever wrong rate is asked for.

CALIBRATION AGAINST THE BENCH, which is what makes this more than another
synthetic fixture. The bench measured `printable_ratio` 0.75 for real
garbage from a real Rockchip target listened to at 115200 while it talked at
1500000. This simulator, swept across inter-byte timings, produces misframed
`printable_ratio` anywhere in 0.00-0.79 -- the measurement sits inside the
range, and the RANGE is the finding: `printable_ratio` for a wrong rate is
not a stable ~0.39 as the original analysis assumed, it moves with the
target's transmit pattern. That is why `baud.score_sample` no longer decides
on it alone.

WHAT THIS IS NOT: a model of a floating ground, of TX/RX swapped, or of
framing-error counts. It models one fault -- the receiver's clock is wrong --
because that is the one the bench measured. Do not read a passing test here
as evidence about the other two.
"""
from __future__ import annotations

BOOT_LOG = b"\r\n".join([
    b"U-Boot 2017.09-g6ba9b1a (Jan 01 2024 - 00:00:00 +0000)",
    b"",
    b"Model: Rockchip RK3399 Evaluation Board",
    b"PreSerial: 2, raw, 0xff1a0000",
    b"DRAM:  4 GiB",
    b"Sysmem: init",
    b"Relocation Offset: f5d78000",
    b"Using default environment",
    b"",
    b"mmc@fe320000: 1, mmc@fe330000: 0",
    b"MMC0: HS400 Enhanced Strobe, 200Mhz",
    b"PartType: EFI",
    b"boot mode: None",
    b"Found DTB in boot part",
    b"HASH(c): OK",
    b"Total: 803.539/1039.348 ms",
    b"",
    b"Starting kernel ...",
    b"",
    b"[    0.000000] Booting Linux on physical CPU 0x0000000000 [0x412fd034]",
    b"[    0.000000] Linux version 4.19.232 (build@builder) (gcc version 9.3.0)",
    b"[    0.000000] Machine model: Rockchip RK3399 Evaluation Board",
    b"[    0.000000] earlycon: uart8250 at MMIO32 0x00000000ff1a0000 (options '')",
    b"[    0.000000] Memory policy: Data cache writealloc",
    b"[    0.000000] cma: Reserved 16 MiB at 0x00000000fd800000",
    b"[    0.000000] psci: PSCIv1.1 detected in firmware.",
    b"[    0.000000] percpu: Embedded 23 pages/cpu s56472 r8192 d30056 u94208",
    b"[    0.000000] Kernel command line: earlycon=uart8250,mmio32,0xff1a0000",
    b"[    0.000000] Dentry cache hash table entries: 524288 (order: 10)",
    b"[    0.000000] Memory: 3878716K/4061184K available (12734K kernel code)",
    b"[    0.000000] SLUB: HWalign=64, Order=0-3, MinObjects=0, CPUs=6, Nodes=1",
    b"[    0.000000] rcu: Hierarchical RCU implementation.",
    b"[    0.000000] arch_timer: cp15 timer(s) running at 24.00MHz (phys).",
    b"[    0.000363] Console: colour dummy device 80x25",
    b"[    0.000384] console [tty1] enabled",
    b"[    0.000440] pid_max: default: 32768 minimum: 301",
    b"[    0.000556] Mount-cache hash table entries: 8192 (order: 4)",
    b"[    0.010101] rcu: Hierarchical SRCU implementation.",
    b"[    0.020202] smp: Bringing up secondary CPUs ...",
    b"[    0.040404] CPU1: Booted secondary processor 0x0000000001",
    b"[    0.060606] SMP: Total of 6 processors activated.",
    b"[    0.090909] vdso: 2 pages (1 code @ ffff0000092c1000, 1 data)",
    b"[    0.111111] pinctrl core: initialized pinctrl subsystem",
    b"[    0.131313] audit: initializing netlink subsys (disabled)",
    b"[    0.161616] SCSI subsystem initialized",
    b"[    0.171717] usbcore: registered new interface driver usbfs",
    b"[    0.202020] dwc3 fe800000.dwc3: Failed to get clk 'ref': -2",
    b"[    0.212121] mmc_host mmc0: Bus speed (slot 0) = 148500000Hz",
    b"[    0.222222] EXT4-fs (mmcblk0p6): mounted filesystem, ordered data mode",
    b"[    0.242424] Freeing unused kernel memory: 8256K",
    b"[    0.252525] Run /sbin/init as init process",
    b"",
    b"Welcome to Buildroot",
    b"buildroot login: ",
])
"""A Rockchip boot log of the shape the bench captured (U-Boot then kernel).

Not the bench's own bytes -- those were not kept -- but the same structure:
banner, driver lines, bracketed timestamps, a login prompt. What matters for
these tests is that it is ORDINARY console output, not a fixture chosen to
flatter the scorer.
"""

ROCKCHIP_CONSOLE_BAUD = 1500000
"""What the bench's target actually ran at, and what `DEFAULT_RATES` could
not reach before this change."""


def to_waveform(data: bytes, idle_bits_between: int = 0,
                lead_idle: int = 16) -> list[int]:
    """UART line levels, one entry per transmit bit time. Idle/mark is 1.

    8N1: a start bit (0), eight data bits LSB-first, a stop bit (1).
    `idle_bits_between` is the gap a transmitter leaves between bytes -- a
    CPU that cannot keep the FIFO full, or the pause between two printk
    lines. It is a free parameter because the real one is unknowable, and
    because sweeping it is exactly how this module shows `printable_ratio`
    to be unstable.
    """
    bits = [1] * lead_idle
    for byte in data:
        bits.append(0)
        for k in range(8):
            bits.append((byte >> k) & 1)
        bits.append(1)
        if idle_bits_between:
            bits.extend([1] * idle_bits_between)
    bits.extend([1] * 32)
    return bits


def receive(bits: list[int], tx_baud: float, rx_baud: float) -> tuple[bytes, int]:
    """Run a receiver clocked at `rx_baud` over a waveform sent at `tx_baud`.

    Hunts for a falling edge, rejects it if the middle of the would-be start
    bit is not low, samples eight data bits at their midpoints, and notes a
    framing error when the stop bit is not high. The byte is delivered
    either way -- ftdi_sio passes a framing-errored byte through to the
    application, which is precisely why a wrong rate yields plausible
    garbage rather than an error.

    Returns `(bytes, framing_errors)`. The framing count is returned for
    documentation only: `baud.score_sample` does NOT measure framing errors
    (see its docstring), and no test here may assert on a signal the
    production code cannot see.
    """
    total_t = len(bits) / tx_baud
    rx_bit = 1.0 / rx_baud
    step = min(rx_bit / 16.0, 1.0 / tx_baud)

    def level(t: float) -> int:
        i = int(t * tx_baud)
        return bits[i] if 0 <= i < len(bits) else 1

    out = bytearray()
    framing = 0
    t = 0.0
    prev = 1
    while t < total_t:
        cur = level(t)
        if prev == 1 and cur == 0:
            start = t
            if level(start + rx_bit * 0.5) != 0:
                prev = cur
                t += step
                continue
            value = 0
            for k in range(8):
                if level(start + rx_bit * (1.5 + k)):
                    value |= 1 << k
            if level(start + rx_bit * 9.5) != 1:
                framing += 1
            out.append(value)
            t = start + rx_bit * 9.5
            prev = 1
            continue
        prev = cur
        t += step
    return bytes(out), framing


def misframed(rx_baud: int, idle_bits_between: int = 0,
              data: bytes = BOOT_LOG,
              tx_baud: int = ROCKCHIP_CONSOLE_BAUD) -> bytes:
    """`data` sent at `tx_baud`, heard at `rx_baud`. The bench's garbage."""
    return receive(to_waveform(data, idle_bits_between), tx_baud, rx_baud)[0]
