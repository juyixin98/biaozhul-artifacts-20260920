"""DBC domain model: a small, explicit subset of the DBC grammar.

Supported subset
----------------
* ``VERSION ""``                  (optional)
* ``NS_ :`` namespace block       (skipped)
* ``BS_:``                        (optional, skipped)
* ``BU_:`` node list              (optional, node names checked when used)
* ``BO_`` standard (11-bit) frame definitions
* ``SG_`` signals: Intel (@1) / Motorola (@0), signed/unsigned,
  scale + offset, and single-layer multiplexing (one ``M`` switch and
  any number of ``m<id>`` branches)
* ``CM_ "";`` database comments   (optional, skipped)

Anything else is rejected with a precise error: extended (29-bit) frame
IDs, extended multiplexing (``m0M``), signal groups, value tables,
attributes, BA_DEF / VAL_ / SGTYPE_ / ... and so on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import DBCParseError

# 11-bit standard frames: 0x000..0x7FF. DBC sets bit 31 to mark 29-bit IDs.
MAX_STANDARD_FRAME_ID = 0x7FF
EXTENDED_ID_FLAG = 0x80000000
MAX_DLC = 8
MAX_BIT_WIDTH = 64

_SIGN_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_NUMBER = r"[+-]?(?:\d+\.\d+|\.\d+|\d+)(?:[eE][+-]?\d+)?"

_BO_RE = re.compile(
    r"^BO_\s+(\d+)\s+(" + _SIGN_NAME + r")\s*:\s+(\d+)\s+(" + _SIGN_NAME + r")\s*$"
)
_SG_RE = re.compile(
    r"^SG_\s+(" + _SIGN_NAME + r")\s+"
    r"(M|m\d+|m\d+M|Mm\d+|m\d+U\d+(?:_\d+)*)?"  # multiplex indicator
    r"\s*:\s+(\d+)\|(\d+)@(0|1)([+-])\s+"
    r"\(" + r"(" + _NUMBER + r"),(" + _NUMBER + r")\)\s+"
    r"\[" + _NUMBER + r"\|" + _NUMBER + r"\]\s+"
    r'"((?:[^"\\]|\\.)*)"\s+'
    r"(.+?)\s*$"
)


@dataclass(frozen=True)
class Signal:
    name: str
    start: int
    length: int
    byte_order: str          # "intel" | "motorola"
    signed: bool
    factor: float
    offset: float
    unit: str
    receivers: tuple[str, ...]
    # None  -> plain (non-multiplexed) signal
    # "M"   -> the multiplexor switch signal
    # int   -> signal active when the switch value equals this id
    mux_kind: str | None = None
    mux_id: int | None = None
    bits: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Message:
    frame_id: int
    name: str
    dlc: int
    sender: str
    signals: tuple[Signal, ...]
    mux_switch: str | None


@dataclass(frozen=True)
class Database:
    version: str
    nodes: tuple[str, ...]
    messages: tuple[Message, ...]


def signal_bit_positions(signal: Signal) -> list[int]:
    """Ordered DBC bit numbers of the signal, MSB -> LSB (extraction order).

    Bit numbering (verified empirically against cantools 44.x): bit ``n``
    lives in byte ``n // 8`` at bit position ``n % 8`` (0 = LSB of byte).

    Intel (little-endian): extraction walks the ascending bit range from
    the highest number down.

    Motorola (big-endian): start marks the MSB. Within a byte the walk
    descends toward lower bit numbers; at column 0 it wraps with +15 into
    the next byte at column 7.
    """
    if signal.byte_order == "intel":
        return list(range(signal.start + signal.length - 1, signal.start - 1, -1))

    bits = [signal.start]
    for _ in range(signal.length - 1):
        b = bits[-1]
        bits.append(b + 15 if b % 8 == 0 else b - 1)
    return bits


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text

    def parse(self) -> Database:
        statements = self._statements()

        version = ""
        nodes: tuple[str, ...] = ()
        messages: list[Message] = []
        pending_signals: dict[int, list[Signal]] = {}
        bo_seen: set[int] = set()

        for line_no, stmt in statements:
            head = stmt.split(None, 1)[0] if stmt.strip() else ""

            if head == "VERSION":
                version = self._parse_version(stmt, line_no)
            elif head == "BS_:":
                self._expect_bare(stmt, "BS_:", line_no)
            elif head == "BU_:":
                nodes = self._parse_nodes(stmt, line_no)
            elif head == "BO_":
                msg = self._parse_bo(stmt, line_no)
                if msg.frame_id in bo_seen:
                    raise DBCParseError(
                        f"duplicate BO_ frame id {msg.frame_id} "
                        f"(0x{msg.frame_id:03X})",
                        line_no,
                    )
                bo_seen.add(msg.frame_id)
                messages.append(msg)
                pending_signals[msg.frame_id] = []
            elif head == "SG_":
                sig = self._parse_sg(stmt, line_no)
                if not pending_signals and not messages:
                    raise DBCParseError("SG_ without a preceding BO_", line_no)
                current_id = messages[-1].frame_id
                existing = pending_signals[current_id]
                if any(s.name == sig.name for s in existing):
                    raise DBCParseError(
                        f"duplicate signal name '{sig.name}' in frame "
                        f"{current_id} (0x{current_id:03X})",
                        line_no,
                    )
                existing.append(sig)
            elif head == "CM_":
                # Only database-level comments: CM_ "text";
                m = re.match(r'^CM_\s+"((?:[^"\\]|\\.)*)"\s*;\s*$', stmt)
                if not m:
                    raise DBCParseError(
                        "only database-level CM_ comments are supported "
                        "(CM_ BO_/CM_ SG_ comments are rejected)",
                        line_no,
                    )
            else:
                raise DBCParseError(
                    f"unsupported DBC construct '{head}'. Supported subset: "
                    "VERSION, NS_ block, BS_:, BU_:, BO_, SG_ (standard IDs, "
                    "Intel/Motorola, signed, scale/offset, single-layer mux), "
                    "and database-level CM_ comments",
                    line_no,
                )

        if not messages:
            raise DBCParseError("DBC contains no BO_ message definitions")

        final_messages = []
        for msg in messages:
            sigs = tuple(pending_signals[msg.frame_id])
            final_messages.append(
                Message(
                    frame_id=msg.frame_id,
                    name=msg.name,
                    dlc=msg.dlc,
                    sender=msg.sender,
                    signals=self._finalize_signals(sigs, msg, nodes),
                    mux_switch=next(
                        (s.name for s in sigs if s.mux_kind == "M"), None
                    ),
                )
            )

        return Database(version=version, nodes=nodes, messages=tuple(final_messages))

    # -- tokenisation -------------------------------------------------------- #

    def _statements(self) -> list[tuple[int, str]]:
        """Split text into statements.

        DBC is line-oriented: ``BO_`` and ``SG_`` definitions occupy one
        physical line each (semicolons are not used there), while
        ``VERSION``/``BS_:``/``BU_:``/``CM_`` are single-line statements
        terminated by ``;``. We therefore treat each non-empty physical
        line (after comment stripping) as one statement.

        The ``NS_ :`` block is skipped: every line after its header up to
        and including the next blank line belongs to the namespace.
        """
        raw_lines = self.text.splitlines()
        out: list[tuple[int, str]] = []
        in_ns = False
        for index, raw in enumerate(raw_lines):
            line = re.sub(r"//.*$", "", raw).strip()

            if in_ns:
                if line == "":
                    in_ns = False
                continue
            if re.match(r"^NS_\s*:", line):
                in_ns = True
                continue
            if line:
                out.append((index + 1, line))
        return out

    # -- construct parsers --------------------------------------------------- #

    @staticmethod
    def _parse_version(stmt: str, line_no: int) -> str:
        m = re.match(r'^VERSION\s+"((?:[^"\\]|\\.)*)"\s*;?\s*$', stmt)
        if not m:
            raise DBCParseError("malformed VERSION statement", line_no)
        return _unescape_dbc_string(m.group(1))

    @staticmethod
    def _expect_bare(stmt: str, token: str, line_no: int) -> None:
        if not re.match(r"^" + re.escape(token) + r"\s*;?\s*$", stmt):
            raise DBCParseError(
                f"'{token}' must be empty; custom bit timing is not supported",
                line_no,
            )

    @staticmethod
    def _parse_nodes(stmt: str, line_no: int) -> tuple[str, ...]:
        m = re.match(r"^BU_:\s*(.*?)\s*;?\s*$", stmt)
        if not m:
            raise DBCParseError("malformed BU_: statement", line_no)
        body = m.group(1).split()
        for node in body:
            if not re.fullmatch(_SIGN_NAME, node):
                raise DBCParseError(f"invalid node name '{node}'", line_no)
        return tuple(body)

    def _parse_bo(self, stmt: str, line_no: int) -> Message:
        m = _BO_RE.match(stmt.rstrip(";").rstrip())
        if not m:
            raise DBCParseError(
                "malformed BO_ statement, expected "
                "'BO_ <id> <Name>: <dlc> <Sender>;'",
                line_no,
            )
        frame_id = int(m.group(1))
        dlc = int(m.group(3))

        if frame_id & EXTENDED_ID_FLAG:
            raise DBCParseError(
                f"frame 0x{frame_id & 0x1FFFFFFF:08X} is an extended (29-bit) "
                "CAN ID; only standard 11-bit frames are supported",
                line_no,
            )
        if frame_id > MAX_STANDARD_FRAME_ID:
            raise DBCParseError(
                f"frame id {frame_id} (0x{frame_id:X}) is not a valid standard "
                "11-bit ID (allowed range 0..2047/0x000..0x7FF)",
                line_no,
            )
        if not 0 <= dlc <= MAX_DLC:
            raise DBCParseError(
                f"DLC {dlc} out of range (allowed 0..8)", line_no
            )
        return Message(
            frame_id=frame_id,
            name=m.group(2),
            dlc=dlc,
            sender=m.group(4),
            signals=(),
            mux_switch=None,
        )

    def _parse_sg(self, stmt: str, line_no: int) -> Signal:
        m = _SG_RE.match(stmt.rstrip(";").rstrip())
        if not m:
            raise DBCParseError(
                "malformed SG_ statement, expected "
                "'SG_ <Name> [M|m<id>] : <start>|<length>@<0|1><+|-> "
                "(<factor>,<offset>) [<min>|<max>] \"<unit>\" <Receivers>'",
                line_no,
            )
        name, mux, start, length, order, sign, factor, offset, unit, recv = (
            m.groups()
        )

        # Reject extended/nested multiplexing explicitly. Only bare 'M'
        # (switch) and 'm<id>' (single-level branch) are supported.
        if mux is not None and mux not in ("M",) and not re.fullmatch(
            r"m\d+", mux
        ):
            raise DBCParseError(
                f"extended/nested multiplexing '{mux}' is not supported; "
                "only a single 'M' switch and one-level 'm<id>' branches are",
                line_no,
            )

        start_i, length_i = int(start), int(length)
        if not 1 <= length_i <= MAX_BIT_WIDTH:
            raise DBCParseError(
                f"signal '{name}' has illegal bit width {length_i} "
                f"(allowed 1..{MAX_BIT_WIDTH})",
                line_no,
            )
        if not 0 <= start_i < 64:
            raise DBCParseError(
                f"signal '{name}' start bit {start_i} out of range (0..63)",
                line_no,
            )

        mux_kind: str | None = None
        mux_id: int | None = None
        if mux == "M":
            mux_kind = "M"
        elif mux is not None:
            mux_kind = "m"
            mux_id = int(mux[1:])
            if not 0 <= mux_id <= 0xFFFFFFFF:
                raise DBCParseError(
                    f"signal '{name}' multiplex id {mux_id} out of range",
                    line_no,
                )

        receivers = tuple(
            token.strip() for token in recv.split(",") if token.strip()
        )
        if receivers == ("Vector__XXX",):
            receivers = ()

        return Signal(
            name=name,
            start=start_i,
            length=length_i,
            byte_order="intel" if order == "1" else "motorola",
            signed=(sign == "-"),
            factor=float(factor),
            offset=float(offset),
            unit=_unescape_dbc_string(unit),
            receivers=receivers,
            mux_kind=mux_kind,
            mux_id=mux_id,
        )

    def _finalize_signals(
        self,
        sigs: list[Signal],
        msg: Message,
        nodes: tuple[str, ...],
    ) -> tuple[Signal, ...]:
        dlc = msg.dlc
        if dlc == 0 and sigs:
            raise DBCParseError(
                f"frame {msg.name} (0x{msg.frame_id:03X}) has DLC 0 but "
                "defines signals"
            )

        switches = [s for s in sigs if s.mux_kind == "M"]
        if len(switches) > 1:
            raise DBCParseError(
                f"frame {msg.name} (0x{msg.frame_id:03X}) has multiple "
                f"multiplexor switches: {[s.name for s in switches]}"
            )

        resolved: list[Signal] = []
        for s in sigs:
            bits = signal_bit_positions(s)
            if any(n < 0 for n in bits):
                raise DBCParseError(
                    f"signal '{s.name}' in frame {msg.name} starts at bit "
                    f"{s.start} and extends before byte 0"
                )
            if any(n >= dlc * 8 for n in bits):
                raise DBCParseError(
                    f"signal '{s.name}' ({s.length} bits starting at {s.start}, "
                    f"{s.byte_order}) does not fit in frame {msg.name} "
                    f"(0x{msg.frame_id:03X}) with DLC {dlc}"
                )
            if len(set(bits)) != s.length:
                raise DBCParseError(
                    f"signal '{s.name}' bit pattern wraps onto itself "
                    f"(start={s.start}, length={s.length}, {s.byte_order})"
                )
            for r in s.receivers:
                if nodes and r not in nodes:
                    raise DBCParseError(
                        f"signal '{s.name}' receiver '{r}' is not declared in BU_:"
                    )
            resolved.append(
                Signal(
                    name=s.name,
                    start=s.start,
                    length=s.length,
                    byte_order=s.byte_order,
                    signed=s.signed,
                    factor=s.factor,
                    offset=s.offset,
                    unit=s.unit,
                    receivers=s.receivers,
                    mux_kind=s.mux_kind,
                    mux_id=s.mux_id,
                    bits=frozenset(bits),
                )
            )

        self._check_overlaps(resolved, msg)
        return tuple(resolved)

    @staticmethod
    def _check_overlaps(sigs: list[Signal], msg: Message) -> None:
        """Reject overlapping signals that can be active at the same time.

        Plain signals are always active. The multiplexor switch is always
        active. Branch signals with different mux ids are never active
        simultaneously, so overlaps *between* branches are legal (this is
        how multiplexed layouts share bytes); overlaps within the same
        branch, or between a branch and an always-active signal, are not.
        """
        # Groups keyed by the "activation set" that can contain them:
        #   ("always",)          plain signals + the M switch
        #   ("branch", mux_id)   signals gated on that switch value
        groups: dict[tuple, list[Signal]] = {}
        for s in sigs:
            if s.mux_kind in (None, "M"):
                key = ("always",)
            else:
                key = ("branch", s.mux_id)
            groups.setdefault(key, []).append(s)

        # Any two signals in the same group cannot overlap.
        def _pairwise(group: list[Signal]) -> None:
            for i, a in enumerate(group):
                for b in group[i + 1 :]:
                    clash = a.bits & b.bits
                    if clash:
                        raise DBCParseError(
                            f"signals '{a.name}' and '{b.name}' overlap in frame "
                            f"{msg.name} (0x{msg.frame_id:03X}) at bits "
                            f"{sorted(clash)}"
                        )

        for key, group in groups.items():
            _pairwise(group)
            if key != ("always",) and ("always",) in groups:
                for a in group:
                    for b in groups[("always",)]:
                        clash = a.bits & b.bits
                        if clash:
                            label = (
                                "the multiplexor switch"
                                if b.mux_kind == "M"
                                else f"signal '{b.name}'"
                            )
                            raise DBCParseError(
                                f"signal '{a.name}' (mux branch {a.mux_id}) "
                                f"overlaps with always-active {label} in frame "
                                f"{msg.name} (0x{msg.frame_id:03X}) at bits "
                                f"{sorted(clash)}"
                            )


def _unescape_dbc_string(raw: str) -> str:
    """Decode DBC string escapes (``\\"``, ``\\\\``) found in quoted tokens."""
    return re.sub(r'\\(.)', r"\1", raw)


def parse_dbc(text: str) -> Database:
    """Parse a DBC text document into a validated :class:`Database`."""
    return _Parser(text).parse()
