#!/bin/sh
# Runs as root just long enough to fix ownership of the mounted /app/data
# (Docker creates bind mounts and anonymous volumes as root, which the
# unprivileged `hark` user can't write to), then execs into that user for
# everything else. No application code ever runs as root.
set -e
mkdir -p /app/data
chown -R hark:hark /app/data
exec gosu hark "$@"
