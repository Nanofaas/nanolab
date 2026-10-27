"""The one error that stops the experiment-02 matrix without a retry.

A refusal says the protocol does not allow the cell to be measured: a budget
the kernel does not enforce, a process outside it, something an earlier cell
left running, a deployment other than the one asked. Retrying cannot change
that, so the caller stops at once. The caller recognises it by this class, not
by `ValueError`, which it extends only as the invalid state it is: a
`ValueError` from output that does not parse is a failure like any other,
which the caller may retry.
"""

from __future__ import annotations


class CellRefusedError(ValueError):
    """The protocol's preconditions for measuring a cell do not hold."""
