#!/bin/sh
# Install proof on an appliance.
#
#   sh install.sh                      from a copy of this repository
#   curl ... | sh                      straight from GitHub, with GH_TOKEN exported (see README.md)
#
# It installs uv when missing, installs proof with `uv tool install` (its own Python 3.12 and
# environment, `proof` in ~/.local/bin), and prepares ~/.proof and the secrets file. Downloaded,
# the source lands in ~/src/proof-headless, for the manifests and example bundles. Running it
# again upgrades proof and leaves the models manifest and the secrets as they are.
#
# Environment:
#   PROOF_VERSION       release tag to install (default: the latest release); a branch works too
#   GH_TOKEN            a GitHub token that can read Ethicompass/proof-headless, needed only
#                       without a local copy; handed to curl on stdin, never written to disk
#   PROOF_MANIFEST      the manifest under manifests/ copied to $PROOF_HOME/models.yaml
#                       (default dgx-spark)
#   PROOF_HOME          default ~/.proof
#   PROOF_SECRETS_FILE  default ~/.config/proof/secrets.env
#
# Everything runs from `main` on the last line, so a download cut off mid-stream runs nothing.
set -eu

REPO=Ethicompass/proof-headless
SRC="$HOME/src/proof-headless"
PROOF_HOME="${PROOF_HOME:-$HOME/.proof}"
MANIFEST="${PROOF_MANIFEST:-dgx-spark}"
SECRETS="${PROOF_SECRETS_FILE:-$HOME/.config/proof/secrets.env}"
BIN="$HOME/.local/bin"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

github() {
    printf 'header = "Authorization: Bearer %s"\n' "$GH_TOKEN" | curl -fsSL -K - "$@"
}

download() {
    [ -n "${GH_TOKEN:-}" ] || die "no local copy of proof-headless here, and GH_TOKEN is not set to download one"
    command -v curl >/dev/null || die "curl is required"
    api="https://api.github.com/repos/$REPO"
    version="${PROOF_VERSION:-}"
    if [ -z "$version" ]; then
        version=$(github "$api/releases/latest" \
            | sed -n 's/^ *"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)
        [ -n "$version" ] || die "could not read the latest release of $REPO; does GH_TOKEN have access?"
    fi
    say "Downloading $REPO $version"
    rm -rf "$SRC.new"
    mkdir -p "$SRC.new"
    github "$api/tarball/$version" | tar -xzf - -C "$SRC.new" --strip-components=1 \
        || die "could not download $version"
    [ -f "$SRC.new/pyproject.toml" ] || die "the download of $version holds no pyproject.toml"
    rm -rf "$SRC"
    mv "$SRC.new" "$SRC"
    source_dir="$SRC"
}

main() {
    here=
    case "$0" in
        *install.sh) here=$(cd "$(dirname "$0")" && pwd) ;;
    esac
    if [ -n "$here" ] && grep -qs '^name = "proof-headless"' "$here/pyproject.toml"; then
        source_dir="$here"
        say "Installing from $source_dir"
    else
        download
    fi

    if ! command -v uv >/dev/null && [ ! -x "$BIN/uv" ]; then
        say "Installing uv"
        curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null || true
        [ -x "$BIN/uv" ] || die "uv did not install; see https://docs.astral.sh/uv/ and run this again"
    fi
    uv=$(command -v uv || echo "$BIN/uv")

    say "Installing proof"
    "$uv" tool install --force --python 3.12 --quiet "$source_dir"

    mkdir -p "$PROOF_HOME"
    if [ ! -e "$PROOF_HOME/models.yaml" ] && [ -f "$source_dir/manifests/$MANIFEST/models.yaml" ]; then
        cp "$source_dir/manifests/$MANIFEST/models.yaml" "$PROOF_HOME/models.yaml"
        say "Models manifest: $PROOF_HOME/models.yaml ($MANIFEST)"
    fi
    if [ ! -e "$SECRETS" ]; then
        secrets_dir=$(dirname "$SECRETS")
        if [ ! -d "$secrets_dir" ]; then
            mkdir -p "$secrets_dir"
            chmod 700 "$secrets_dir"
        fi
        (umask 077 && : >"$SECRETS")
        say "Secrets file: $SECRETS (empty, mode 600)"
    fi

    command -v docker >/dev/null || warn "docker is not installed"
    docker compose version >/dev/null 2>&1 || warn "docker compose is not available to $(id -un)"
    command -v nvidia-smi >/dev/null || warn "nvidia-smi not found: the models need an NVIDIA GPU"

    "$BIN/proof" version
    case ":$PATH:" in
        *":$BIN:"*) ;;
        *) warn "$BIN is not on PATH; log in again or add it" ;;
    esac
    cat <<EOF

Next:
  proof secrets set HF_TOKEN       # gated weights (Llama Guard)
  docker login ghcr.io             # Yield's and Lens's images are private
  proof models up && proof doctor
  proof stack pin --channel release && proof stack up --bundle <bundle>
EOF
}

main "$@"
