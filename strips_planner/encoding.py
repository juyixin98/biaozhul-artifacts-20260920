"""Canonical state encoding used for search de-duplication and evidence ids."""

from __future__ import annotations

import hashlib

from .model import Atom, State


def encode_state(state: State) -> str:
    """Deterministic textual encoding of a state.

    Two states are the same state iff their encodings are equal, regardless of
    the order in which atoms were produced. Atoms are sorted by their tuple
    form, which is total over ``str`` tuples.
    """
    return " | ".join(_atom_token(a) for a in sorted(state))


def state_hash(state: State) -> str:
    """Stable 16-char identifier used as a foreign key in evidence tables."""
    digest = hashlib.sha256(encode_state(state).encode("utf-8")).hexdigest()
    return digest[:16]


def decode_state(encoding: str) -> State:
    """Inverse of :func:`encode_state` (used when replaying stored runs)."""
    if not encoding:
        return frozenset()
    atoms: set[Atom] = set()
    for token in encoding.split(" | "):
        if "(" not in token:
            atoms.add((token,))
            continue
        pred, rest = token.split("(", 1)
        args = tuple(a.strip() for a in rest.rstrip(")").split(",") if a.strip())
        atoms.add((pred, *args))
    return frozenset(atoms)


def _atom_token(atom: Atom) -> str:
    if len(atom) == 1:
        return atom[0]
    return atom[0] + "(" + ",".join(atom[1:]) + ")"
