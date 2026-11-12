"""`proof evidence`: one self-describing, checksummed package per engagement.

The package carries what measured the engagement and what it measured: the bundle's receipt and
the rendered specs, the models manifest, the compose files and image digests, the doctor's
reports, each run's Yield manifest and stored objects, and each run's Lens result. It never
carries a secret (ADR 0001): an export whose files hold any value of the operator's secrets file
is refused whole.
"""
