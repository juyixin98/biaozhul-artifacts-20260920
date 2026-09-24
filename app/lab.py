"""Lab harness: a *simulated* device endpoint for acceptance testing.

This is deliberately an emulator: the request/response exchange, host-side
timestamps (``time.time`` around a real HTTP call through uvicorn) and all
estimation are real, but the one-way network delays and the device clock are
injected/controlled here so that asymmetric-delay, wrap, reboot and
drift-change scenarios are reproducible.  It is mounted under ``/lab`` and is
the thing ``scripts/probe.py`` talks to; it is not part of the calibration
API proper.

Device clock model::

    device_time(t) = t_start + (1 + ppm*1e-6) * (t - t_start)
    counter(t)     = hz * (device_time(t) - device_epoch)   [mod M if wrapping]

A reboot resets ``device_epoch`` (counter restarts near zero); a wrap is just
modular arithmetic on top of an otherwise continuous clock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

router = APIRouter(prefix="/lab", tags=["lab"])


@dataclass
class VirtualDevice:
    device_id: str
    hz: float = 1_000_000.0          # nominal counter ticks per second
    ppm: float = 0.0                 # frequency error, parts per million
    modulus: Optional[float] = None
    counter_ref_host: float = field(default_factory=time.time)
    ticks_at_ref: float = 0.0        # counter value at counter_ref_host
    uptime_at_epoch: float = 0.0
    reboot_count: int = 0
    ticks_per_s: float = field(init=False)

    def __post_init__(self) -> None:
        self.ticks_per_s = self.hz * (1.0 + self.ppm * 1e-6)

    def counter_at(self, host_t: float) -> float:
        # Counter value (modulus applied by caller wrapper) is the number of
        # ticks accumulated since counter_ref_host, at rate ticks_per_s.
        raw = self.ticks_at_ref + self.ticks_per_s * (host_t - self.counter_ref_host)
        if self.modulus is not None:
            raw = raw % self.modulus
        return raw

    def reboot(self) -> None:
        self.reboot_count += 1
        # Restart the counter near zero at the current instant.
        self.counter_ref_host = time.time()
        self.ticks_at_ref = 0.0
        self.uptime_at_epoch = 0.0

    def set_drift(self, ppm: float) -> None:
        """Change oscillator rate while keeping the counter continuous.

        Pin the current counter value, then change ticks/second; afterwards the
        counter keeps climbing from exactly the same value but at the new rate.
        """
        now = time.time()
        current_counter = self.counter_at(now)
        self.ticks_at_ref = current_counter
        self.counter_ref_host = now
        self.ppm = ppm
        self.ticks_per_s = self.hz * (1.0 + self.ppm * 1e-6)


_REG: dict[str, VirtualDevice] = {}
_LOCK = threading.Lock()


class LabCreate(BaseModel):
    device_id: str
    hz: float = 1_000_000.0
    ppm: float = 0.0
    modulus: Optional[float] = None


class TickRequest(BaseModel):
    downlink_delay: float = Field(0.0, ge=0.0, le=2.0,
                                  description="injected downlink delay (s), slept after c_send is stamped")
    processing_delay: float = Field(0.0, ge=0.0, le=1.0)


class TickResponse(BaseModel):
    c_recv: float
    c_send: Optional[float]
    server_recv: float
    server_send: float


def get_device(device_id: str) -> VirtualDevice:
    with _LOCK:
        dev = _REG.get(device_id)
        if dev is None:
            raise HTTPException(404, f"lab device {device_id!r} does not exist")
        return dev


@router.post("/devices", status_code=201)
def create_device(body: LabCreate) -> dict:
    with _LOCK:
        if body.device_id in _REG:
            raise HTTPException(409, "lab device already exists")
        dev = VirtualDevice(
            device_id=body.device_id, hz=body.hz, ppm=body.ppm,
            modulus=body.modulus,
        )
        _REG[body.device_id] = dev
        return {
            "device_id": dev.device_id, "hz": dev.hz, "ppm": dev.ppm,
            "modulus": dev.modulus,
        }


@router.post("/devices/{device_id}/tick", response_model=TickResponse)
def tick(device_id: str, body: TickRequest) -> TickResponse:
    dev = get_device(device_id)
    # Real arrival timestamp on this (simulated) device.
    t_recv = time.time()
    c_recv = dev.counter_at(t_recv)
    if body.processing_delay > 0:
        time.sleep(body.processing_delay)
    t_send = time.time()
    c_send = dev.counter_at(t_send)
    # Emulate slow downlink *after* the response-departure counter is stamped,
    # exactly like a real network: c_send stays inside [t0, t3].
    if body.downlink_delay > 0:
        time.sleep(body.downlink_delay)
    return TickResponse(
        c_recv=c_recv, c_send=c_send,
        server_recv=t_recv, server_send=t_send,
    )


@router.post("/devices/{device_id}/reboot", status_code=200)
def reboot(device_id: str) -> dict:
    dev = get_device(device_id)
    with _LOCK:
        dev.reboot()
    return {"device_id": device_id, "reboot_count": dev.reboot_count}


@router.post("/devices/{device_id}/drift")
def set_drift(device_id: str, ppm: float) -> dict:
    dev = get_device(device_id)
    with _LOCK:
        dev.set_drift(ppm)
    return {"device_id": device_id, "ppm": dev.ppm,
            "ticks_per_s": dev.ticks_per_s}


@router.get("/devices/{device_id}")
def get_state(device_id: str) -> dict:
    dev = get_device(device_id)
    with _LOCK:
        return {
            "device_id": dev.device_id, "hz": dev.hz, "ppm": dev.ppm,
            "ticks_per_s": dev.ticks_per_s, "modulus": dev.modulus,
            "reboot_count": dev.reboot_count,
        }


@router.delete("/devices/{device_id}", status_code=204)
def delete_device(device_id: str) -> Response:
    with _LOCK:
        _REG.pop(device_id, None)
    return Response(status_code=204)
