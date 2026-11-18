"""Errors shared by the payment provider adapters."""

from __future__ import annotations


class PaymentStatusCheckError(RuntimeError):
    """A provider status refresh did not produce any provider answer.

    Raised only when the caller explicitly asks for strict propagation — the
    background verification worker. The manual/admin path keeps the lenient
    behavior and receives the unchanged local record instead, so an operator
    is never shown an exception while the payment is untouched.

    The message deliberately carries no provider payload, payment identifier
    or transport details: callers may log the error, and alert digests must
    stay free of them.
    """
