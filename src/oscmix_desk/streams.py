"""Observed USB playback capacity, separate from OSC register addresses.

The 0.7.1 recordings show that ALSA accepts modes which do not carry all
their channels, or do not transfer audio correctly. This module reads the
active hardware PCM, not the requested rate of a resampled desktop stream.
It never opens a PCM or changes a clock. Physical digital I/O and capture
content are separate questions; these playback measurements cannot answer
them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .constants import DEFAULT_DEVICE_NAME, DEFAULT_USB_ID
from .log import log
from .model import Config

_CHANNELS_BY_ALTSET = {1: 20, 2: 16, 3: 14, 4: 8}


@dataclass(frozen=True)
class PlaybackMode:
    """Nominal hardware rate and the USB setting actually running."""

    rate: int
    channels: int
    altset: int


@dataclass(frozen=True)
class PlaybackState:
    """An identity and its current stream; no mode means no active PCM."""

    card: Optional[int]
    serial: str
    mode: Optional[PlaybackMode]


def _playback_status(text: str) -> Tuple[str, Optional[int]]:
    """The active setting, not the first entry in the descriptor list."""
    playback = re.search(r"^Playback:\n(.*?)(?=^\S|\Z)", text,
                         re.MULTILINE | re.DOTALL)
    if playback is None:
        raise ValueError("USB playback status is missing")
    status = playback.group(1).split("\n  Interface ", 1)[0]
    states = re.findall(r"^  Status: (.*)$", status, re.MULTILINE)
    if states == ["Stop"]:
        return status, None
    if states != ["Running"]:
        raise ValueError("USB playback status is unknown")
    interface = re.findall(r"^    Interface = (\d+)$", status, re.MULTILINE)
    altset = re.findall(r"^    Altset = (\d+)$", status, re.MULTILINE)
    if interface != ["1"] or len(altset) != 1:
        raise ValueError("active USB playback interface/setting is ambiguous")
    return playback.group(1), int(altset[0])


def parse_playback(stream: str, params: str) -> Optional[PlaybackMode]:
    """Decode one UCX II proc recording; inconsistent active data is refused.

    ``Status: Stop`` is an idle PCM, not a 48-kHz default. Advertised
    rates and the descriptors of inactive alternate settings are not
    observations of the mode in use.
    """
    playback, altset = _playback_status(stream)
    if altset is None:
        return None
    descriptors = re.findall(
        r"^  Interface (\d+)\n    Altset (\d+)\n(.*?)(?=^  Interface |\Z)",
        playback, re.MULTILINE | re.DOTALL)
    selected = [body for interface, alternate, body in descriptors
                if interface == "1" and int(alternate) == altset]
    if len(selected) != 1:
        raise ValueError("active USB playback descriptor is missing or ambiguous")
    channels = re.findall(r"^    Channels: (\d+)$", selected[0], re.MULTILINE)
    formats = re.findall(r"^    Format: (\S+)$", selected[0], re.MULTILINE)
    pcm_channels = re.findall(r"^channels: (\d+)$", params, re.MULTILINE)
    pcm_formats = re.findall(r"^format: (\S+)$", params, re.MULTILINE)
    rates = re.findall(r"^rate: (\d+) \((\d+)/(\d+)\)$", params, re.MULTILINE)
    if (len(channels) != 1 or channels != pcm_channels
            or formats != ["S24_3LE"] or pcm_formats != formats
            or len(rates) != 1):
        raise ValueError("USB playback and ALSA hardware parameters disagree")
    nominal, numerator, denominator = map(int, rates[0])
    if nominal <= 0 or denominator <= 0 or numerator != nominal * denominator:
        raise ValueError("ALSA hardware rate is not a consistent integer rate")
    count = int(channels[0])
    if _CHANNELS_BY_ALTSET.get(altset) != count:
        raise ValueError("USB playback channel/alternate-setting map is unmeasured")
    return PlaybackMode(nominal, count, altset)


def _stable_playback(stream: Path, first: str) -> Optional[PlaybackMode]:
    """Correlate two stream observations with two hardware-parameter reads."""
    _status, active = _playback_status(first)
    params_path = stream.parent / "pcm0p/sub0/hw_params"
    params = params_path.read_text() if active is not None else ""
    second = stream.read_text()
    if first.partition("\n")[0] != second.partition("\n")[0]:
        raise ValueError("USB playback identity changed during inspection")
    mode = parse_playback(first, params)
    if parse_playback(second, params) != mode:
        raise ValueError("USB playback mode changed during inspection")
    # hw_params can change while stream0 still says Running/alt N.
    if active is not None and params_path.read_text() != params:
        raise ValueError("ALSA hardware parameters changed during inspection")
    return mode


def read_playback(config: Config, proc_root: Path) -> PlaybackState:
    """Read only the exact UCX II's stream and correlate it with hw_params.

    Read twice around hw_params so a change of alternate setting or card
    cannot be silently combined with an earlier PCM's parameters. A DAW
    can still change the mode after the read; no desk lock owns that PCM.
    """
    candidates = []
    for stream in sorted((proc_root / "asound").glob("card[0-9]*/stream0")):
        if re.fullmatch(r"card\d+", stream.parent.name) is None:
            continue
        try:
            text = stream.read_text(errors="replace")
        except OSError as exc:
            try:
                identity = (stream.parent / "usbid").read_text().strip()
            except OSError:
                identity = ""
            if identity == config.usb_id:
                raise OSError("cannot read the UCX II playback stream: %s" % exc) from exc
            continue
        header = re.match(r"RME Fireface UCX II \((\d+)\) at ", text)
        if header is None:
            # An empty/damaged UCX stream file is not evidence of an idle
            # card. Other USB audio models remain outside this guard.
            try:
                identity = (stream.parent / "usbid").read_text().strip()
            except OSError:
                identity = ""
            if identity == config.usb_id:
                raise OSError("UCX II playback identity is missing from its stream report")
            continue
        if config.serial and header[1] != config.serial:
            continue
        try:
            usb_id = (stream.parent / "usbid").read_text().strip()
        except OSError as exc:
            raise OSError("cannot identify the UCX II playback card: %s" % exc) from exc
        if usb_id != config.usb_id:
            raise OSError("UCX II playback card has an unexpected USB identity")
        candidates.append((stream, header[1], text))
    if not candidates:
        return PlaybackState(None, config.serial, None)
    if len(candidates) != 1:
        raise OSError("more than one UCX II playback card matches; set [device] serial")
    stream, serial, first = candidates[0]
    try:
        mode = _stable_playback(stream, first)
    except (OSError, ValueError) as exc:
        raise OSError("cannot validate the active USB playback mode: %s" % exc) from exc
    return PlaybackState(int(stream.parent.name[4:]), serial, mode)


def playback_problem(config: Config, mode: PlaybackMode) -> Optional[str]:
    """Known playback limits, from the 24 recorded 0.7.1 combinations."""
    description = "%d Hz / %d USB channels" % (mode.rate, mode.channels)
    if _CHANNELS_BY_ALTSET.get(mode.altset) != mode.channels:
        return "%s has an unmeasured USB alternate-setting map" % description
    if mode.rate not in (44100, 48000, 88200, 96000, 176400, 192000):
        return "%s is not a measured UCX II playback mode" % description
    if mode.rate in (176400, 192000) and mode.channels > 14:
        return ("%s failed the measured audio-transfer checks; select a "
                "14- or 8-channel hardware stream before applying playback routes"
                % description)
    limit = min(mode.channels, 16) if mode.rate in (88200, 96000) else mode.channels
    for route in config.routes:
        unavailable = [channel for channel in route.playback if channel > limit]
        if unavailable:
            return ("route %r needs playback %s, but %s carries playback 1..%d"
                    % (route.name, "/".join(map(str, unavailable)), description, limit))
    return None


class PlaybackGuard:
    """Recheck before each write phase; report any mid-apply mode change.

    An idle or absent PCM gives no live-rate validation. Keep offline/startup
    routing usable and say so once; never guess 48 kHz. Once an apply has
    begun, changing between unknown and known modes is a change too.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.before: Optional[PlaybackState] = None

    def check(self) -> None:
        config = self.config
        if (config.device_name != DEFAULT_DEVICE_NAME or config.usb_id != DEFAULT_USB_ID
                or not any(route.playback for route in config.routes)):
            return
        state = read_playback(config, Path(os.environ.get("OSCMIX_PROC_ROOT", "/proc")))
        if self.before is not None and state != self.before:
            raise OSError("USB playback mode or identity changed during the apply; "
                          "remaining writes refused, retry after the hardware stream settles")
        if state.mode is not None:
            problem = playback_problem(config, state.mode)
            if problem is not None:
                raise OSError(problem)
            if self.before is None:
                log.info("active USB playback on ALSA card %s (serial %s): "
                         "%d Hz, %d channels, alternate setting %d",
                         state.card, state.serial, state.mode.rate,
                         state.mode.channels, state.mode.altset)
        elif self.before is None:
            log.warning("no active UCX II hardware playback stream is observable; "
                        "live USB mode is not validated (register addresses are checked)")
        self.before = state
