"""Where the appliance's own services are, in one place.

Two kinds of address. The in-network ones are how one container of the stack reaches another on
the `proof-stack` network; they go into what a container reads, such as a run spec's
`webhook_url`. The loopback ones are how `proof` on the host reaches the stack's published ports.
Both are the appliance's own (ADR 0001 lets defaults name them), never a client's, and `proof stack`
renders the services these name, so a change here and a change there are one change.
"""

from __future__ import annotations

STACK_PROJECT = "proof-stack"
"""The compose project `proof stack` brings up."""

LENS_WEBHOOK = "http://lens-api:8080/webhooks/yield"
"""Where Yield's measure delivers a run's results: Lens's receiver, as a job container on the
`proof-stack` network reaches it. A spec without a `webhook_url` is measured and delivered
nowhere, and its raw outputs stay in Yield's bucket."""

YIELD_API = "http://127.0.0.1:8080"
"""Yield's API from the host."""

YIELD_COMPUTE = "http://127.0.0.1:8081"
"""Yield compute from the host."""

LENS_API = "http://127.0.0.1:8090"
"""Lens's API from the host."""
