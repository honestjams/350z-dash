"""
Consult-II register address map for the 350Z (VQ35DE).

IMPORTANT — verification status:

The Consult-II protocol uses 1-byte register addresses. The ECU streams the
RAW byte value back; the formulas below convert raw bytes into engineering
units. These formulas are derived from community work (Nissan DataScan II,
ECUtalk, the openconsult project), NOT from official Nissan documentation.

When you first plug in your cable:
  1. Run `python -m server.main --probe`
  2. Compare the values it reports against another tool (NDSII, Torque Pro
     via OBD-II for the params both expose) WHILE the engine is running.
  3. Adjust SCALE and OFFSET below for any register that's off.

Registers marked `# VERIFY` are best guesses from community sources for the
350Z specifically — they're plausible but were not personally validated.
"""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Register:
    """A single Consult-II register definition."""
    addr: int                   # ECU register address (1 byte)
    name: str                   # Field name used in JSON payload to the UI
    decode: Callable[[int], float]  # raw_byte -> engineering value
    unit: str
    desc: str


# Decoder helpers — keep them tiny and pure for testability.
def _passthrough(b: int) -> float:
    return float(b)


def _offset(offset: float):
    return lambda b: float(b) - offset


def _scale(factor: float):
    return lambda b: float(b) * factor


def _scale_offset(factor: float, offset: float):
    return lambda b: float(b) * factor - offset


# RPM is two registers (high byte * 256 + low byte) * 12.5
# Handled specially in consult.py, not as a single register.

# ---------------------------------------------------------------------
# Single-byte register definitions
# ---------------------------------------------------------------------
REGISTERS: dict[str, Register] = {
    # Coolant temperature: raw byte = (°C + 50), so subtract 50.
    "coolant": Register(
        addr=0x08, name="coolant",
        decode=_offset(50.0), unit="°C",
        desc="Engine coolant temperature",
    ),

    # Vehicle speed: raw byte = km/h * 2, so divide by 2.
    "speed": Register(
        addr=0x0B, name="speed",
        decode=_scale(0.5), unit="km/h",
        desc="Vehicle speed sensor",
    ),

    # Throttle position: raw byte = % * 2, so divide by 2.
    "tps": Register(
        addr=0x09, name="tps",
        decode=_scale(0.5), unit="%",
        desc="Throttle position",
    ),

    # Battery voltage: raw byte = volts * 10. # VERIFY
    "battery": Register(
        addr=0x0C, name="battery",
        decode=_scale(0.08), unit="V",
        desc="Battery voltage (key on)",
    ),

    # Ignition timing advance: raw byte = degrees BTDC + 20. # VERIFY
    "ignAdv": Register(
        addr=0x18, name="ignAdv",
        decode=_offset(20.0), unit="° BTDC",
        desc="Ignition timing advance",
    ),

    # MAF sensor voltage: raw byte = volts * 50, so divide by 50. # VERIFY
    "maf": Register(
        addr=0x0D, name="maf",
        decode=_scale(0.02), unit="V",
        desc="Mass airflow sensor voltage",
    ),

    # Injection pulse width: raw byte = ms * 10, so divide by 10. # VERIFY
    "injPW": Register(
        addr=0x05, name="injPW",
        decode=_scale(0.1), unit="ms",
        desc="Fuel injector pulse width",
    ),

    # AFR derived from narrowband O2 sensor (very rough — narrowband only
    # indicates rich/lean of stoich, not actual AFR). For real AFR you
    # need a wideband sensor + controller. Set to a placeholder for now.
    "afr": Register(
        addr=0x1A, name="afr",
        decode=lambda b: 14.7,  # placeholder until wideband is added
        unit="AFR",
        desc="Air/fuel ratio (placeholder — needs wideband)",
    ),
}


# Registers that AREN'T on the Consult-II bus for the NA VQ35DE,
# but the UI shows them. These come from aftermarket sensors via an
# ADC HAT on the Pi (or are computed). Listed here for reference.
EXTERNAL_FIELDS = {
    "oilTemp",       # External sender + ADC
    "oilPressure",   # External sender + ADC
    "map",           # ECU has MAP but the register isn't well-documented;
                     # showing as external for now
}


# Order to subscribe — fast-changing values first so they update sooner.
SUBSCRIPTION_ORDER = [
    "tps",       # changes fastest
    "speed",
    "coolant",
    "ignAdv",
    "maf",
    "injPW",
    "battery",
    "afr",
]
