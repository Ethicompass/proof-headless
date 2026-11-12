"""Where the stack's services answer: from the host, and from a container on the stack's networks.

One module so a URL the stack renders and a URL a command calls cannot drift apart. The host URLs
are loopback, because every port the stack publishes binds 127.0.0.1. The in-network ones are what
a spec or a setting names, for a container to reach (`proof stack` puts `lens-api`, `yield-minio`
and `brain-registry` on the shared network).
"""

from __future__ import annotations

YIELD_API = "http://127.0.0.1:8080"
YIELD_COMPUTE = "http://127.0.0.1:8081"
LENS_API = "http://127.0.0.1:8090"
BRAIN_REGISTRY = "localhost:5051"
"""Where vitruvio pushes a brain from the host, over plain HTTP."""

YIELD_COMPUTE_IN_NETWORK = "http://yield-compute:8080"
LENS_API_IN_NETWORK = "http://lens-api:8080"
LENS_WEBHOOK = f"{LENS_API_IN_NETWORK}/webhooks/yield"
"""A run's `webhook_url`: Yield's measurement job posts its delivery to Lens by name."""
BRAIN_REGISTRY_IN_NETWORK = "brain-registry:5000"
"""The registry a run's `kb_ref` names; the same digests as `BRAIN_REGISTRY`."""
