"""Resolve a serial adapter from a `/dev/serial/by-id` path -- never a raw tty.

`/dev/ttyUSBn` numbering is assigned by USB enumeration order. It is not
stable across reboots, and it is not stable across a second adapter
appearing on the bus -- which phase 2 of this project guarantees, since the
relay board adds a third serial node next to the Tigard's two. A resolver
that accepted a raw tty path, or that fell back to scanning when a by-id
path didn't resolve, could silently hand a caller the wrong device. So this
module refuses instead: the only accepted input is a `by-id` symlink, and a
declared serial that doesn't match the device found there is an error, not
a warning.

Voltage is never reported here. The Tigard's level selector is a physical
switch with no software read-back; a value we can't read shouldn't be
guessed at.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class DeviceError(RuntimeError):
    """A device path or declared serial could not be resolved. Always a refusal."""


@dataclass(frozen=True)
class ResolvedDevice:
    by_id: str
    tty: str
    serial: str | None
    interface: str | None


def _parse_by_id_name(name: str) -> tuple[str | None, str | None]:
    """Pull (serial, interface) out of a udev by-id filename.

    The stock rule for a USB-serial adapter produces
    `usb-<vendor>_<model>_<serial>-<ifNN>-port0`. Parsed from the right: the
    last two hyphen-separated segments are the interface and the port: The
    piece before them is `<vendor>_<model>_<serial>`, and the serial is that
    piece's final underscore-separated field. Neither the vendor nor the
    model is assumed to be hyphen- or underscore-free -- only the position
    from the right is trusted.
    """
    parts = name.rsplit("-", 2)
    if len(parts) != 3:
        return None, None
    prefix, interface, _port = parts
    fields = prefix.split("_")
    serial = fields[-1] if len(fields) > 1 else None
    return serial, interface


def resolve_device(by_id_path: str, *, expect_serial: str | None) -> ResolvedDevice:
    """Resolve a `by-id` symlink to its tty, asserting the serial if declared.

    Refuses (raises `DeviceError`) rather than falling back, on: a path that
    doesn't exist, a path that exists but isn't a symlink (a raw tty), a
    by-id name that doesn't parse into serial/interface, or a declared
    `expect_serial` that doesn't match what the device reports.
    """
    if not os.path.lexists(by_id_path):
        raise DeviceError(f"no such device path: {by_id_path}")

    if not os.path.islink(by_id_path):
        raise DeviceError(
            f"{by_id_path} is not a by-id symlink -- refusing to resolve it. "
            "Pass a path under /dev/serial/by-id/, not a raw tty: ttyUSBn "
            "numbering depends on USB enumeration order and is not stable."
        )

    name = os.path.basename(by_id_path)
    serial, interface = _parse_by_id_name(name)

    if expect_serial is not None:
        if serial is None:
            raise DeviceError(
                f"expected serial {expect_serial!r} but {by_id_path} has no "
                "serial encoded in its by-id name"
            )
        if serial != expect_serial:
            raise DeviceError(
                f"serial mismatch on {by_id_path}: expected {expect_serial!r}, "
                f"found {serial!r}"
            )

    tty = os.path.realpath(by_id_path)
    return ResolvedDevice(by_id=by_id_path, tty=tty, serial=serial, interface=interface)


def list_serial_devices(root: str = "/dev/serial/by-id") -> list[ResolvedDevice]:
    """List every by-id entry under `root`.

    No adapter plugged in means an absent `root` -- that is a normal bench
    state, reported as an empty list, not an error. An entry that fails to
    resolve (not a symlink, unparseable name) is skipped rather than
    aborting the whole listing: one malformed node shouldn't hide the
    others.
    """
    if not os.path.isdir(root):
        return []

    devices = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        try:
            devices.append(resolve_device(path, expect_serial=None))
        except DeviceError:
            continue
    return devices
