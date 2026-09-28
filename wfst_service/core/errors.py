"""Kernel-level errors (re-exported with stable codes)."""

from __future__ import annotations

from ..corpus.errors import BudgetExhausted, WfstError


class CoreError(WfstError):
    """Base class for kernel failures."""

    code = "core_error"


class TopologyError(CoreError):
    """An FST definition is structurally invalid at the kernel boundary."""

    code = "topology_error"
