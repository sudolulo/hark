#!/usr/bin/env bash
# Builds hark's Docker image against a multi-repo context: hark depends on
# adscrub as a local path dependency (../adscrub, editable — see
# pyproject.toml [tool.uv.sources]), which a plain `docker build .` run from
# this repo alone can't resolve (the build context wouldn't contain adscrub's
# source at all). This script stages git-archive-clean copies of both repos
# (tracked files only — no .venv/, hark.db/data/, .git/) side by side and
# builds against that staging directory instead.
#
# Usage:
#   scripts/build-image.sh [TAG] [--gpu]
#
# TAG defaults to hark's own __version__. Override the image name with REGISTRY_IMAGE to
# push somewhere of your own, e.g. REGISTRY_IMAGE=ghcr.io/you/hark
set -euo pipefail

HARK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADSCRUB_DIR="${ADSCRUB_DIR:-$HARK_DIR/../adscrub}"
REGISTRY_IMAGE="${REGISTRY_IMAGE:-hark}"

GPU=0
TAG=""
for arg in "$@"; do
  case "$arg" in
    --gpu) GPU=1 ;;
    *) TAG="$arg" ;;
  esac
done
if [ -z "$TAG" ]; then
  TAG="$(cd "$HARK_DIR" && python3 -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('src/hark/__init__.py').read())[1])")"
fi

if [ ! -d "$ADSCRUB_DIR" ]; then
  echo "adscrub checkout not found at $ADSCRUB_DIR (set ADSCRUB_DIR to override)" >&2
  exit 1
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "staging hark ($HARK_DIR) -> $STAGE/hark"
mkdir -p "$STAGE/hark"
git -C "$HARK_DIR" archive HEAD | tar -x -C "$STAGE/hark"

echo "staging adscrub ($ADSCRUB_DIR) -> $STAGE/adscrub"
mkdir -p "$STAGE/adscrub"
git -C "$ADSCRUB_DIR" archive HEAD | tar -x -C "$STAGE/adscrub"

IMAGE="$REGISTRY_IMAGE:$TAG"
if [ "$GPU" = "0" ]; then
  # The deploy target reserves a GPU for the transcribe service, so a CPU-only image
  # there does not fail — it quietly runs Whisper on ~4 CPU cores at roughly 10x the
  # wall time (shipped by accident in 0.17.2-0.17.4; see CHANGELOG). Anything destined
  # for the deploy wants --gpu.
  echo "WARNING: building a CPU-only image (no --gpu). The deployed app passes a GPU" >&2
  echo "         through, so this image will fall back to slow CPU transcription there." >&2
fi
echo "building $IMAGE (GPU=$GPU) from staged context $STAGE"
docker build -f "$STAGE/hark/Dockerfile" --build-arg "GPU=$GPU" -t "$IMAGE" "$STAGE"

echo "built $IMAGE"
echo "push with: docker push $IMAGE"
