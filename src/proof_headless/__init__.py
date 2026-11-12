"""The Ethi Proof appliance CLI.

One host runs everything an engagement needs: the self-hosted models, Yield and Lens, and the
client's bundle. `proof` brings each of those up from a declaration, proves they answer, and runs
the engagement to an evidence package. Nothing in this package names a client, a model or an
endpoint; those arrive in manifests and bundles (ADR 0001).
"""

from importlib.metadata import version

__version__ = version("proof-headless")
