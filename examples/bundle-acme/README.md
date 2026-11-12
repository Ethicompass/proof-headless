# Example bundle: ACME (fictitious)

The shape every client bundle takes. ACME does not exist, so the catalogue is empty and the brain
directory holds only its `vitruvio.toml`. The profile follows Yield's `json_sync` protocol (the
same shape as Yield's `plain-http` connector fixture). Tests copy this directory and load it
against a fake compute.

A real bundle lives outside this repository, next to the client's material:

```sh
proof bundle validate path/to/bundle
proof bundle load path/to/bundle --compute http://127.0.0.1:8081 --registry localhost:5051
```

`load` records what it published in `$PROOF_HOME/bundles/<client>/receipt.json`: each catalogue,
profile, prior and mapping's version, and each brain's digest. `proof spec render` (#6) pins the
run specs to that receipt.
