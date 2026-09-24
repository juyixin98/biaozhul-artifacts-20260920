"""Hand-verifiable bit maps for DBC messages.

Renders a per-byte grid of a frame showing which signal occupies every
bit, using DBC bit numbering (bit 7 on the left of each byte). This is
the diagram a reviewer cross-checks by hand against the DBC file.
"""

from __future__ import annotations

from .dbc import Database, Message, Signal


def render_message_bitmap(message: Message) -> str:
    """Return a monospaced bit-ownership diagram for one message."""
    lines: list[str] = []
    lines.append(
        f"Frame 0x{message.frame_id:03X} ({message.frame_id}) "
        f"'{message.name}' DLC={message.dlc}"
    )
    lines.append("        " + "  ".join(f"b{b}" for b in range(7, -1, -1)))

    labels = _unique_labels(message)
    labels_by_bit = {
        n: labels[sig.name] for sig in message.signals for n in sig.bits
    }
    for byte_index in range(message.dlc):
        cells = []
        for col in range(7, -1, -1):
            dbc_bit = byte_index * 8 + col
            cells.append(f" {labels_by_bit.get(dbc_bit, '.'):>2}")
        lines.append(f"byte{byte_index:<2} " + " ".join(cells))

    legend = _legend(message, labels)
    if legend:
        lines.append("")
        lines.append("legend:")
        lines.extend(f"  {lab:>2} = {text}" for lab, text in legend.items())
    return "\n".join(lines)


def _label(sig: Signal) -> str:
    """Up-to-2-char label derived from the signal name."""
    alnum = [c for c in sig.name if c.isalnum()]
    if not alnum:
        return "?"
    return (alnum[0] + (alnum[-1] if len(alnum) > 1 else "")).upper()[:2]


def _unique_labels(message: Message) -> dict[str, str]:
    """Map every signal name to a unique <=2-char grid label."""
    labels: dict[str, str] = {}
    used: set[str] = set()
    for index, sig in enumerate(message.signals):
        base = _label(sig)
        candidate = base
        if candidate in used:
            # Disambiguate with a single digit/letter suffix derived from
            # the declaration index; keeps the grid two columns wide.
            suffix_alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            suffix = suffix_alphabet[index % len(suffix_alphabet)]
            candidate = base[0] + suffix
        while candidate in used:
            index += 1
            suffix = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"[
                index % 36
            ]
            candidate = base[0] + suffix
        labels[sig.name] = candidate
        used.add(candidate)
    return labels


def _legend(message: Message, labels: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for sig in message.signals:
        lab = labels[sig.name]
        mux = ""
        if sig.mux_kind == "M":
            mux = " [MUX switch]"
        elif sig.mux_kind == "m":
            mux = f" [mux={sig.mux_id}]"
        out[lab] = (
            f"{sig.name} start={sig.start} len={sig.length} "
            f"{'motorola' if sig.byte_order == 'motorola' else 'intel'}"
            f"{' signed' if sig.signed else ''}{mux}"
        )
    return out


def render_database_bitmaps(db: Database) -> str:
    return "\n\n".join(render_message_bitmap(m) for m in db.messages)
