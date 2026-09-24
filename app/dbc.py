"""Parser for a deliberately small, explicit subset of the DBC grammar.

Supported grammar (line oriented; anything outside it is REJECTED with a
``DBCSyntaxError`` that names the offending line):

* ``VERSION "..."``
* ``NS_ :`` followed by a body containing only known NS_ symbols
  (``NS_DESC_``, ``CM_``, ``BA_DEF_``, ``BA_``, ``VAL_``, ``CAT_DEF_``,
  ``CAT_``, ``FILTER``, ``BA_DEF_DEF_``, ``EV_DATA_``, ``ENVVAR_DATA_``,
  ``SGTYPE_``, ``SGTYPE_VAL_``, ``BA_DEF_SGTYPE_``, ``BA_SGTYPE_``,
  ``SIG_TYPE_REF_``, ``VAL_TABLE_``, ``SIG_GROUP_``, ``SIG_VALTYPE_``,
  ``SIGTYPE_VALTYPE_``, ``BO_TX_BU_``, ``BA_DEF_REL_``, ``BA_REL_``,
  ``BA_DEF_DEF_REL_``, ``BU_SG_REL_``, ``BU_EV_REL_``, ``BU_BO_REL_``,
  ``SG_MUL_VAL_``) -- the body carries no semantics for decoding.
* ``BS_:`` (always empty in this subset)
* ``BU_:`` followed by zero or more node names
* ``BO_ <id> <name>: <dlc> <transmitter>`` -- standard (11-bit) ids only,
  DLC 0..8; ``Vector__XXX`` and empty transmitter lists are accepted.
* ``SG_ <name> [mux] : <start>|<length>@<order><sign> (factor,offset)
  [min|max] "unit" <receivers>`` with multiplex markers restricted to the
  single-level form: ``M`` or ``M0`` (the multiplexor switch) and ``mN``
  with N >= 1 (a branch). ``M`` is the Vector-standard token; ``M0`` is a
  commonly seen alias accepted for compatibility.
* ``CM_ ... ;`` comments, which may span several lines (skipped).

Explicitly rejected (among others): extended CAN ids (``BO_`` id with the
high bit set, i.e. >= 0x80000000), multiplexed-extended markers such as
``m1m0`` / ``m0M``, unknown statement keywords, malformed numbers, signals
referenced outside a message, DLC > 8 or != 8 bits boundaries overflow,
duplicate message ids / signal names, missing multiplexors referenced by
branches, multiple multiplexors in one message, illegal bit widths and
overlapping signals within the same multiplex group.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .bitops import (
    INTEL,
    MOTOROLA,
    BYTE_ORDER_BY_DBC_CODE,
    BitLayoutError,
    validate_within_frame,
)

STANDARD_ID_MAX = 0x7FF
EXTENDED_ID_FLAG = 0x80000000
MAX_DLC = 8

KNOWN_NS_SYMBOLS = {
    "NS_DESC_", "CM_", "BA_DEF_", "BA_", "VAL_", "CAT_DEF_", "CAT_",
    "FILTER", "BA_DEF_DEF_", "EV_DATA_", "ENVVAR_DATA_", "SGTYPE_",
    "SGTYPE_VAL_", "BA_DEF_SGTYPE_", "BA_SGTYPE_", "SIG_TYPE_REF_",
    "VAL_TABLE_", "SIG_GROUP_", "SIG_VALTYPE_", "SIGTYPE_VALTYPE_",
    "BO_TX_BU_", "BA_DEF_REL_", "BA_REL_", "BA_DEF_DEF_REL_",
    "BU_SG_REL_", "BU_EV_REL_", "BU_BO_REL_", "SG_MUL_VAL_",
}


class DBCError(ValueError):
    """Base class for all DBC-related rejections (carries a stable code)."""

    code = "dbc_error"

    def __init__(self, message: str, *, line: int | None = None):
        super().__init__(message)
        self.message = message
        self.line = line

    def detail(self) -> dict:
        detail = {"code": self.code, "message": self.message}
        if self.line is not None:
            detail["line"] = self.line
        return detail


class DBCSyntaxError(DBCError):
    code = "dbc_syntax_error"


class DBCLayoutError(DBCError):
    code = "dbc_layout_error"


@dataclass(frozen=True)
class Signal:
    name: str
    start: int
    length: int
    byte_order: str  # INTEL / MOTOROLA
    signed: bool
    factor: Decimal
    offset: Decimal
    minimum: Decimal | None
    maximum: Decimal | None
    unit: str
    receivers: tuple[str, ...]
    mux_kind: str  # "plain" | "switch" | "branch"
    mux_value: int | None  # branch selector value; None for plain/switch

    def to_canonical(self) -> dict:
        def num(value: Decimal | None) -> str | None:
            return None if value is None else format(value, "f")

        return {
            "name": self.name,
            "start": self.start,
            "length": self.length,
            "byte_order": self.byte_order,
            "signed": self.signed,
            "factor": num(self.factor),
            "offset": num(self.offset),
            "minimum": num(self.minimum),
            "maximum": num(self.maximum),
            "unit": self.unit,
            "receivers": list(self.receivers),
            "mux_kind": self.mux_kind,
            "mux_value": self.mux_value,
        }


@dataclass
class Message:
    frame_id: int
    name: str
    dlc: int
    transmitter: str
    signals: list[Signal] = field(default_factory=list)

    @property
    def mux_switch(self) -> Signal | None:
        for sig in self.signals:
            if sig.mux_kind == "switch":
                return sig
        return None

    def to_canonical(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "name": self.name,
            "dlc": self.dlc,
            "transmitter": self.transmitter,
            "signals": [s.to_canonical() for s in self.signals],
        }


@dataclass
class Database:
    version: str
    nodes: tuple[str, ...]
    messages: list[Message]
    source_sha256: str = ""  # filled by the caller (crypto module)

    def message_by_id(self, frame_id: int) -> Message | None:
        for message in self.messages:
            if message.frame_id == frame_id:
                return message
        return None

    def to_canonical(self) -> dict:
        return {
            "version": self.version,
            "nodes": list(self.nodes),
            "messages": sorted(
                (m.to_canonical() for m in self.messages),
                key=lambda m: m["frame_id"],
            ),
        }


_BO_RE = re.compile(
    r"^BO_\s+(\d+)\s+([A-Za-z_][\w]*)\s*:\s*(\d+)\s*(\S*)\s*$"
)
_SG_RE = re.compile(
    r"^SG_\s+([A-Za-z_][\w]*)\s*"
    r"(?:([A-Za-z0-9]+))?\s*"
    r":\s*(\d+)\|(\d+)@([01])([+-])\s+"
    r"\(\s*([^,()]*?)\s*,\s*([^()]*?)\s*\)\s*"
    r"(?:\[([^|\]\[]+)\|([^\]\[]+)\]\s*)?"
    r'"((?:[^"\\]|\\.)*)"\s*'
    r"(.*)$"
)
_MUX_SWITCH_RE = re.compile(r"^[Mm](?:0)?$")
_MUX_BRANCH_RE = re.compile(r"^[mM]([0-9]+)$")
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_NODE_NAME_RE = re.compile(r"^[A-Za-z_][\w-]*$")


def _parse_decimal(raw: str, *, what: str, line: int) -> Decimal:
    raw = raw.strip()
    if not _NUMBER_RE.match(raw):
        raise DBCSyntaxError(f"invalid {what}: {raw!r}", line=line)
    try:
        return Decimal(raw)
    except InvalidOperation as exc:  # pragma: no cover - regex gates this
        raise DBCSyntaxError(f"invalid {what}: {raw!r}", line=line) from exc


def _parse_mux_token(token: str | None, *, line: int) -> tuple[str, int | None]:
    if token is None:
        return "plain", None
    if token in ("M", "m", "M0", "m0"):
        return "switch", None
    match = _MUX_BRANCH_RE.match(token)
    if match:
        value = int(match.group(1))
        if value == 0:
            return "switch", None
        if value > 0x7FFFFFFF:
            raise DBCSyntaxError(
                f"multiplex value {value} too large", line=line
            )
        return "branch", value
    raise DBCSyntaxError(
        f"unsupported multiplex marker {token!r}: only single-level "
        "markers 'M'/'M0' (switch) and 'mN' with N>=1 (branch) are "
        "supported; extended multiplexing (e.g. 'm1m0') is rejected",
        line=line,
    )


def _check_signal_layout(signal: Signal, message: Message) -> None:
    if signal.length > 8 * message.dlc:
        raise DBCLayoutError(
            f"signal {signal.name!r} is {signal.length} bits wide but "
            f"message {message.name} has DLC {message.dlc} "
            f"({8 * message.dlc} bits)"
        )
    try:
        validate_within_frame(
            signal.start, signal.length, signal.byte_order, message.dlc
        )
    except BitLayoutError as exc:
        raise DBCLayoutError(str(exc)) from exc


def _coexist(kind_a: str, mux_a: int | None,
             kind_b: str, mux_b: int | None) -> bool:
    """Whether two signals are present in the frame at the same time.

    * the multiplexor switch always coexists with everything;
    * a plain (non-multiplexed) signal always coexists with everything;
    * two branch signals coexist only when they share the branch value.
    """

    if kind_a == "switch" or kind_b == "switch":
        return True
    if kind_a == "plain" or kind_b == "plain":
        return True
    return mux_a == mux_b


def _check_overlaps(message: Message) -> None:
    """Detect bit overlaps; signals in different mux branches may overlap."""

    layouts = [
        (sig, validate_within_frame(
            sig.start, sig.length, sig.byte_order, message.dlc
        ))
        for sig in message.signals
    ]
    for i, (sig_i, pos_i) in enumerate(layouts):
        for sig_j, pos_j in layouts[i + 1:]:
            if set(pos_i).isdisjoint(pos_j):
                continue
            if _coexist(
                sig_i.mux_kind, sig_i.mux_value,
                sig_j.mux_kind, sig_j.mux_value,
            ):
                position = sorted(set(pos_i) & set(pos_j))[0]
                raise DBCLayoutError(
                    f"signal overlap in message {message.name} "
                    f"(frame id 0x{message.frame_id:X}): "
                    f"{sig_i.name!r} overlaps {sig_j.name!r} at DBC bit "
                    f"position {position}"
                )


def parse_dbc(text: str) -> Database:
    """Parse DBC text into a validated :class:`Database` or raise."""

    if not text or not text.strip():
        raise DBCSyntaxError("empty DBC input")

    lines = text.splitlines()
    version = ""
    nodes: tuple[str, ...] = ()
    messages: list[Message] = []

    frame_ids: set[int] = set()
    current_message: Message | None = None
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        stripped = raw_line.strip()
        line_no = index + 1
        index += 1

        if not stripped or stripped.startswith("//"):
            continue

        if stripped.startswith("VERSION"):
            match = re.match(r'^VERSION\s+"((?:[^"\\]|\\.)*)"\s*$', stripped)
            if not match:
                raise DBCSyntaxError(
                    "malformed VERSION line (expected VERSION \"...\")",
                    line=line_no,
                )
            version = _unescape_dbc_string(match.group(1))
            continue

        if stripped == "NS_ :":
            # Skip NS_ body: only known symbols may appear. The body ends at
            # the first structural keyword / blank section boundary.
            while index < len(lines):
                body = lines[index].strip()
                body_line = index + 1
                index += 1
                if not body:
                    break
                if body in ("BS_:",) or body.startswith("BU_:"):
                    index -= 1  # re-process the boundary line
                    break
                token = body.rstrip(";").strip()
                if token and token not in KNOWN_NS_SYMBOLS:
                    raise DBCSyntaxError(
                        f"unsupported NS_ symbol {token!r}", line=body_line
                    )
            continue

        if stripped == "BS_:" or re.match(r"^BS_\s*:\s*;?\s*$", stripped):
            continue

        if stripped.startswith("BU_"):
            remainder = stripped[3:].lstrip()
            if not remainder.startswith(":"):
                raise DBCSyntaxError("malformed BU_ line", line=line_no)
            names = remainder[1:].split()
            for name in names:
                if not _NODE_NAME_RE.match(name):
                    raise DBCSyntaxError(
                        f"invalid node name {name!r}", line=line_no
                    )
            nodes = tuple(names)
            continue

        if stripped.startswith("BO_"):
            current_message = _parse_bo(stripped, line_no, frame_ids)
            messages.append(current_message)
            continue

        if stripped.startswith("SG_"):
            if current_message is None:
                raise DBCSyntaxError(
                    "SG_ signal definition outside of any BO_ message",
                    line=line_no,
                )
            signal = _parse_sg(
                stripped, line_no, current_message
            )
            if any(s.name == signal.name for s in current_message.signals):
                raise DBCSyntaxError(
                    f"duplicate signal name {signal.name!r} in message "
                    f"{current_message.name}",
                    line=line_no,
                )
            current_message.signals.append(signal)
            continue

        if stripped.startswith("CM_"):
            # Comments may span multiple lines until the terminating ';'.
            consumed, _ = _consume_statement(lines, index - 1, line_no, "CM_")
            index = consumed
            continue

        raise DBCSyntaxError(
            f"unsupported DBC statement: {stripped.split()[0]!r}. "
            "Supported statements: VERSION, NS_, BS_, BU_, BO_, SG_, CM_.",
            line=line_no,
        )

    _validate_messages(messages)
    return Database(version=version, nodes=nodes, messages=messages)


def _consume_statement(
    lines: list[str], start_index: int, line_no: int, keyword: str
) -> tuple[int, str]:
    """Consume a ``KEYWORD ... ;`` statement (possibly multi-line)."""

    buffer = lines[start_index]
    index = start_index + 1
    while ";" not in buffer and index < len(lines):
        buffer += "\n" + lines[index]
        index += 1
    if ";" not in buffer:
        raise DBCSyntaxError(
            f"unterminated {keyword} statement (missing ';')", line=line_no
        )
    head = buffer.split(None, 1)[0]
    if not head.startswith(keyword):  # pragma: no cover - caller guarantees
        raise DBCSyntaxError(f"expected {keyword}", line=line_no)
    return index, buffer


def _unescape_dbc_string(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _parse_bo(
    stripped: str, line_no: int, frame_ids: set[int]
) -> Message:
    match = _BO_RE.match(stripped)
    if not match:
        raise DBCSyntaxError(
            "malformed BO_ line (expected: "
            "BO_ <id> <name>: <dlc> <transmitter>)",
            line=line_no,
        )
    raw_id, name, raw_dlc, transmitter = match.groups()
    frame_id = int(raw_id)
    if frame_id & EXTENDED_ID_FLAG:
        raise DBCSyntaxError(
            f"extended CAN frame id 0x{frame_id:X} is not supported "
            "(only standard 11-bit ids 0..0x7FF)",
            line=line_no,
        )
    if frame_id > STANDARD_ID_MAX:
        raise DBCSyntaxError(
            f"frame id {frame_id} (0x{frame_id:X}) exceeds the standard "
            f"11-bit maximum 0x{STANDARD_ID_MAX:X}",
            line=line_no,
        )
    dlc = int(raw_dlc)
    if not 0 <= dlc <= MAX_DLC:
        raise DBCSyntaxError(
            f"invalid DLC {dlc} for message {name!r}: must be 0..{MAX_DLC}",
            line=line_no,
        )
    if frame_id in frame_ids:
        raise DBCSyntaxError(f"duplicate BO_ frame id {frame_id}", line=line_no)
    frame_ids.add(frame_id)
    transmitter = transmitter.strip()
    if transmitter.endswith(";"):
        transmitter = transmitter[:-1].strip()
    return Message(
        frame_id=frame_id,
        name=name,
        dlc=dlc,
        transmitter=transmitter,
    )


def _parse_sg(stripped: str, line_no: int, message: Message) -> Signal:
    match = _SG_RE.match(stripped)
    if not match:
        raise DBCSyntaxError(
            "malformed SG_ line (expected: SG_ Name [mux] : "
            "start|length@<0|1><+|-> (factor,offset) [min|max] "
            '"unit" receivers)',
            line=line_no,
        )
    (
        name,
        mux_token,
        raw_start,
        raw_length,
        order_code,
        sign_code,
        raw_factor,
        raw_offset,
        raw_min,
        raw_max,
        raw_unit,
        raw_receivers,
    ) = match.groups()

    mux_kind, mux_value = _parse_mux_token(mux_token, line=line_no)
    start = int(raw_start)
    length = int(raw_length)
    if length < 1:
        raise DBCLayoutError(
            f"signal {name!r} has illegal bit width {length} (must be >= 1)",
            line=line_no,
        )
    if length > 64:
        raise DBCLayoutError(
            f"signal {name!r} has illegal bit width {length} "
            "(maximum is 64 bits)",
            line=line_no,
        )
    if start < 0 or start >= 8 * message.dlc:
        raise DBCLayoutError(
            f"signal {name!r} start bit {start} is outside the "
            f"{message.dlc}-byte message {message.name}",
            line=line_no,
        )
    byte_order = BYTE_ORDER_BY_DBC_CODE[order_code]
    signal = Signal(
        name=name,
        start=start,
        length=length,
        byte_order=byte_order,
        signed=(sign_code == "-"),
        factor=_parse_decimal(raw_factor, what="factor", line=line_no),
        offset=_parse_decimal(raw_offset, what="offset", line=line_no),
        minimum=(
            _parse_decimal(raw_min, what="minimum", line=line_no)
            if raw_min is not None
            else None
        ),
        maximum=(
            _parse_decimal(raw_max, what="maximum", line=line_no)
            if raw_max is not None
            else None
        ),
        unit=_unescape_dbc_string(raw_unit),
        receivers=tuple(raw_receivers.split()),
        mux_kind=mux_kind,
        mux_value=mux_value,
    )
    _check_signal_layout(signal, message)
    return signal


def _validate_messages(messages: list[Message]) -> None:
    if not messages:
        raise DBCSyntaxError("DBC contains no BO_ message definitions")
    for message in messages:
        switches = [s for s in message.signals if s.mux_kind == "switch"]
        branches = [s for s in message.signals if s.mux_kind == "branch"]
        if len(switches) > 1:
            raise DBCLayoutError(
                f"message {message.name} defines {len(switches)} multiplexor "
                "switches; single-level multiplexing allows exactly one"
            )
        if branches and not switches:
            raise DBCLayoutError(
                f"message {message.name} contains multiplexed branches but "
                "no multiplexor switch (m0)"
            )
        if branches:
            switch = switches[0]
            branch_values = {s.mux_value for s in branches}
            for value in sorted(v for v in branch_values if v is not None):
                if value >= (1 << switch.length):
                    raise DBCLayoutError(
                        f"branch m{value} in message {message.name} cannot be "
                        f"selected: switch {switch.name!r} is only "
                        f"{switch.length} bit(s) wide (max value "
                        f"{(1 << switch.length) - 1})"
                    )
        _check_overlaps(message)
