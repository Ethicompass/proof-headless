# Contributing

## Branches and releases

| Branch | What a merge does |
|---|---|
| `develop` | Integration. Every change lands here through a pull request. |
| `main` | Releases. A promotion from `develop` opens a release-please PR, and merging that PR cuts the tag, the release and the changelog. |

Open each pull request from a branch off `develop`, and link the issue it closes.

A promotion only opens a release PR when it carries a commit whose type has a visible changelog
section in `release-please-config.json` (`feat`, `fix`, `perf`, `refactor`); `chore`, `docs`,
`test` and `ci` are hidden and not released on their own. A change to what the appliance runs, such as the
`release` channel in `channels.yaml`, is a `fix(stack)` (or `feat(stack)`), because an operator
installing the release gets it. When such a change already went in under another type, a commit
whose body ends with `Release-As: <version>` makes release-please cut that version.

## Commits

Use Conventional Commits with a **mandatory scope**: `feat(models): ...`, `fix(runs): ...`. The
scopes mirror the `area:` labels: `cli` `models` `stack` `bundle` `runs` `lens` `evidence` `docs`
`ci` `deps` `repo`. `uv tool run --from commitizen cz commit` offers them. CI checks every commit
of a pull request. Do not add `Co-Authored-By` trailers.

## Code

- Everything is in English: code, commits, pull requests, docs and ADRs.
- Explain why something is the way it is in docstrings, not in `#` comments that narrate the code.
- Nothing in the code names a client, a model id or a client's endpoint. Those belong in manifests
  and bundles. Defaults may name the appliance's own loopback services (`127.0.0.1:8081`).
- Every command renders with Rich, and read commands also take `--json`.
