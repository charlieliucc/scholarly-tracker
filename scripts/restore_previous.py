#!/usr/bin/env python3
"""Best-effort restore of the last deployed data before a new daily build."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if not base:
        print("No deployed Pages URL is available; starting from repository data.")
        return 0
    restored = 0
    for name in ("papers.json", "status.json"):
        url = f"{base}/data/{name}"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "ScholarlyTracker/1.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read())
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            restored += 1
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
            print(f"Previous {name} was not restored: {error}")
    print(f"Restored {restored} deployed data file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

