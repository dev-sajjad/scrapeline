#!/usr/bin/env bash
# Proves sandbox isolation on the active container runtime (Kiri, Docker Desktop, ...).
# A runtime that accepts flags but does not enforce them fails here.
set -euo pipefail
IMAGE="${SANDBOX_IMAGE:-scrapeline-sandbox:latest}"
RUN=(docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL
     --security-opt no-new-privileges --memory 1g --pids-limit 128 --entrypoint python "$IMAGE" -I -c)
fail=0
check() { if "$@"; then echo "PASS: $desc"; else echo "FAIL: $desc"; fail=1; fi; }

desc="no network egress (TCP to 1.1.1.1:443 must fail)"
check "${RUN[@]}" 'import socket,sys
try:
    socket.create_connection(("1.1.1.1",443),timeout=3); sys.exit(1)
except OSError: sys.exit(0)'

desc="no DNS resolution"
check "${RUN[@]}" 'import socket,sys
try:
    socket.getaddrinfo("example.com",443); sys.exit(1)
except OSError: sys.exit(0)'

desc="root filesystem is read-only"
check "${RUN[@]}" 'import sys
try:
    open("/opt/sl/x","w"); sys.exit(1)
except OSError: sys.exit(0)'

desc="/tmp is writable tmpfs"
check "${RUN[@]}" 'open("/tmp/x","w").write("ok")'

desc="runs as non-root"
check "${RUN[@]}" 'import os,sys; sys.exit(0 if os.getuid()!=0 else 1)'

desc="no secrets in environment"
check "${RUN[@]}" 'import os,sys; sys.exit(1 if any(k.startswith(("ANTHROPIC","AWS","DATABASE")) for k in os.environ) else 0)'

exit $fail
