# SPDX-License-Identifier: MIT
"""swdlib.py - shared SWD/pyOCD plumbing for the PCAP04 v1.5 board.

Target : STM32G0B1CBT6   Probe : any CMSIS-DAP (Raspberry Pi Debug Probe)
"""
import sys, time

TARGET = "stm32g0b1cbtx"

# ---- STM32G0B1 registers used by the host-side tools ----------------------
RCC_BASE      = 0x40021000
RCC_CR        = RCC_BASE + 0x00
RCC_CFGR      = RCC_BASE + 0x08
RCC_PLLCFGR   = RCC_BASE + 0x0C
RCC_IOPENR    = RCC_BASE + 0x34
RCC_APBENR1   = RCC_BASE + 0x3C
RCC_APBENR2   = RCC_BASE + 0x40
RCC_CSR       = RCC_BASE + 0x60

FLASH_BASE    = 0x40022000
FLASH_ACR     = FLASH_BASE + 0x00
FLASH_KEYR    = FLASH_BASE + 0x08
FLASH_OPTKEYR = FLASH_BASE + 0x0C
FLASH_SR      = FLASH_BASE + 0x10
FLASH_CR      = FLASH_BASE + 0x14
FLASH_OPTR    = FLASH_BASE + 0x20
FLASH_ACR_EMPTY   = 1 << 16
FLASH_CR_OBL_LAUNCH = 1 << 27
FLASH_CR_OPTLOCK    = 1 << 30
FLASH_CR_LOCK       = 1 << 31
KEY1, KEY2        = 0x45670123, 0xCDEF89AB
OPTKEY1, OPTKEY2  = 0x08192A3B, 0x4C5D6E7F

GPIOA, GPIOB  = 0x50000000, 0x50000400
MODER, OTYPER, OSPEEDR, PUPDR, IDR, ODR, BSRR, AFRL, AFRH, BRR = (
    0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x20, 0x24, 0x28)

USART2 = 0x40004400
SPI1   = 0x40013000
SPI_CR1, SPI_CR2, SPI_SR, SPI_DR = 0x00, 0x04, 0x08, 0x0C
ADC1     = 0x40012400
ADC_ISR, ADC_IER, ADC_CR, ADC_CFGR1, ADC_CFGR2, ADC_SMPR = 0x00,0x04,0x08,0x0C,0x10,0x14
ADC_CHSELR, ADC_DR, ADC_CCR = 0x28, 0x40, 0x308

DBGMCU_IDCODE = 0x40015800
UID_BASE      = 0x1FFF7590
FLASHSIZE     = 0x1FFF75E0
VREFINT_CAL   = 0x1FFF75AA

FLASH_ORIGIN  = 0x08000000
MAILBOX_ADDR  = 0x20000000          # linker puts the console mailbox here
MBX_MAGIC0, MBX_MAGIC1 = 0x50434150, 0x4D42582D

# ---- board pin map (see board.h) -----------------------------------------
PIN = {
    "PWR_SENSE":   (GPIOA, 0),
    "UART_TX":     (GPIOA, 2),
    "UART_RX":     (GPIOA, 3),
    "SPI_SCK":     (GPIOA, 5),
    "SPI_MISO":    (GPIOA, 6),
    "SPI_MOSI":    (GPIOA, 7),
    "PCAP_CS":     (GPIOB, 0),
    "PCAP_IRQ":    (GPIOB, 1),
    "PCAP_PWR_EN": (GPIOB, 2),
    "SPI_OE_N":    (GPIOB, 10),
    "HOST_READY":  (GPIOB, 11),
    "MEAS_BUSY":   (GPIOB, 12),
}
RAIL_DIV = 430.0 / 100.0     # RPS1 330k + RPS2 100k
RAIL_ON_MV, RAIL_OFF_MV = 3000, 50


class Board:
    """Thin wrapper around a pyOCD session with helpers for this board."""

    def __init__(self, freq=1_000_000, connect_mode="attach", verbose=False):
        from pyocd.core.helpers import ConnectHelper
        self.session = ConnectHelper.session_with_chosen_probe(
            target_override=TARGET, connect_mode=connect_mode, blocking=False,
            options={"frequency": freq, "warning.cortex_m_default": False,
                     "logging": {"level": "debug"} if verbose else {}})
        if self.session is None:
            raise SystemExit("no CMSIS-DAP probe found - is the Debug Probe plugged in?")
        self.session.open()
        self.t = self.session.target

    def close(self):
        try: self.session.close()
        except Exception: pass

    def __enter__(self): return self
    def __exit__(self, *a): self.close()

    # -- memory ------------------------------------------------------------
    def rd(self, a):        return self.t.read32(a)
    def wr(self, a, v):     self.t.write32(a, v & 0xFFFFFFFF)
    def rd8(self, a):       return self.t.read_memory(a, transfer_size=8)
    def wr8(self, a, v):    self.t.write_memory(a, v & 0xFF, transfer_size=8)
    def setbits(self, a, m): self.wr(a, self.rd(a) | m)
    def clrbits(self, a, m): self.wr(a, self.rd(a) & ~m)

    # -- identity ----------------------------------------------------------
    def idcode(self):    return self.rd(DBGMCU_IDCODE)
    def uid(self):       return tuple(self.rd(UID_BASE + 4 * i) for i in range(3))
    def flash_kb(self):  return self.rd(FLASHSIZE) & 0xFFFF
    def vrefint_cal(self): return (self.rd(VREFINT_CAL & ~3) >> (8 * (VREFINT_CAL & 3))) & 0xFFFF

    def identify(self):
        idc = self.idcode()
        return {
            "idcode": idc, "dev_id": idc & 0xFFF, "rev_id": idc >> 16,
            "is_g0b1": (idc & 0xFFF) == 0x467,
            "uid": self.uid(), "flash_kb": self.flash_kb(),
            "optr": self.rd(FLASH_OPTR), "acr": self.rd(FLASH_ACR),
            "flash0": self.rd(FLASH_ORIGIN), "csr": self.rd(RCC_CSR),
        }

    # -- GPIO --------------------------------------------------------------
    def pin_mode(self, name, mode):
        port, p = PIN[name]
        v = self.rd(port + MODER)
        self.wr(port + MODER, (v & ~(3 << (p * 2))) | (mode << (p * 2)))

    def pin_pull(self, name, pull):
        port, p = PIN[name]
        v = self.rd(port + PUPDR)
        self.wr(port + PUPDR, (v & ~(3 << (p * 2))) | (pull << (p * 2)))

    def pin_af(self, name, af):
        port, p = PIN[name]
        reg = port + (AFRL if p < 8 else AFRH)
        sh = (p % 8) * 4
        self.wr(reg, (self.rd(reg) & ~(0xF << sh)) | (af << sh))

    def pin_write(self, name, level):
        port, p = PIN[name]
        self.wr(port + BSRR, (1 << p) if level else (1 << (p + 16)))

    def pin_read(self, name):
        port, p = PIN[name]
        return (self.rd(port + IDR) >> p) & 1


def fmt_uid(uid):
    return "%08X-%08X-%08X" % uid


def decode_optr(optr):
    return {
        "RDP":       optr & 0xFF,
        "nBOOT_SEL": (optr >> 24) & 1,
        "nBOOT1":    (optr >> 25) & 1,
        "nBOOT0":    (optr >> 26) & 1,
        "NRST_MODE": (optr >> 27) & 3,
        "raw":       optr,
    }
