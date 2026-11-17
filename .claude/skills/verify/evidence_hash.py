"""Fingerprint an evidence folder so the hashes survive the project share.

The share adds an ancillary metadata chunk (caBX) to PNG files some time
after they are copied, at an unpredictable delay. Pixels never change, so for
a PNG this hashes only its image data (the IDAT chunks, in order); every other
file is hashed whole. Output is `sha256sum`-style: "<hex>  <name>", with
"[png-idat]" after the name for PNGs.

  python3 evidence_hash.py DIR            # print the list
  python3 evidence_hash.py DIR --check F  # re-check a saved list; exit 1 on any change
"""

from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path

_PNG = b"\x89PNG\r\n\x1a\n"


def fingerprint(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    if not data.startswith(_PNG):
        return hashlib.sha256(data).hexdigest(), path.name
    h, pos = hashlib.sha256(), len(_PNG)
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        if kind == b"IDAT":
            h.update(data[pos + 8 : pos + 8 + length])
        pos += 12 + length
    return h.hexdigest(), f"{path.name} [png-idat]"


def listing(folder: Path) -> list[str]:
    skip = {"SHA256SUMS", "EVIDENCE.sha256"}
    return [
        "{}  {}".format(*fingerprint(p))
        for p in sorted(folder.iterdir())
        if p.is_file() and p.name not in skip
    ]


def main(argv: list[str]) -> int:
    folder = Path(argv[1])
    if len(argv) == 4 and argv[2] == "--check":
        saved = Path(argv[3]).read_text().splitlines()
        now = set(listing(folder))
        bad = [line for line in saved if line and line not in now]
        for line in bad:
            print("CHANGED or MISSING:", line)
        print("OK" if not bad else f"{len(bad)} changed")
        return 1 if bad else 0
    print("\n".join(listing(folder)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
