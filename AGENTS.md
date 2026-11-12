# Working on proof-headless

- Read [ADR 0001](docs/adr/0001-the-appliance-is-generic-a-client-is-a-bundle.md) first. The
  appliance is generic: no client name, model id or endpoint in code or in this repository's
  defaults, except in example manifests and the fictitious test bundle.
- Pipeline: `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -q -m "not appliance"`.
- Commits: Conventional Commits with a mandatory scope (see CONTRIBUTING.md). No `Co-Authored-By`
  trailer and no tool attribution in commits, pull requests or issues.
- Secrets never enter the repository, a bundle or a log. They live in the operator's secrets file
  and travel by `secret_ref`.
- The CLI renders with Rich; the interactive views are Textual. Results go to stdout, operator
  messages to stderr.
