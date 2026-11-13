"""`proof lens`: every finished Yield run ends with its Lens result.

Lens receives Yield's delivery on its webhook, derives a run from it and launches `lens-run`
(Lens ADR 0015, 0022, 0025). The delivery is persisted and admitted before the launch, so when the
launch did not finish the run is still there to be launched again; nothing has to be re-sent by
Yield. This package reads where each of a client's Yield runs stands in Lens and launches the ones
that need it: through Lens's own relaunch route first, and by hand, with the environment lens-api
launches with, when that route cannot.
"""
