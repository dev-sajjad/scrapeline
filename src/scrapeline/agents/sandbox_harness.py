"""Entry point inside the offline sandbox container. Grows into the parser harness in step 4.

Contract: reads inputs from /work (read-only mount), writes one JSON report to stdout.
Never imports fetch/, store/ or settings; never touches the network.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    sys.stdout.write(json.dumps({"status": "ok", "harness": "v0"}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
