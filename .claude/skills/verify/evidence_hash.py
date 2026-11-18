"""Fingerprint an evidence folder so the hashes survive the project share.

Some time after a PNG is copied to the share (seen from seconds to about 42
minutes, no known upper bound), the share inserts one ancillary chunk of its
own, `caBX` (C2PA provenance), always as the chunk straight after IHDR.
There it does not change what the image shows. So for a PNG this hashes
every byte except that one chunk: the signature, every other chunk whole
(length, type, data, CRC, so IHDR, gAMA, tRNS, APNG frames and the rest all
count), and anything after IEND. A `caBX` anywhere else is hashed like any
other chunk, because a chunk between IDATs or before IHDR does change what a
decoder draws. Every other file is hashed whole. Output is
`sha256sum`-style: "<hex>  <name>" (two spaces, as sha256sum prints), with
"[png-no-caBX]" after the name for PNGs.

  python3 evidence_hash.py DIR            # print the list
  python3 evidence_hash.py DIR --check F  # exit 1 if any file changed, is
                                          # missing, or was added since F
"""

from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path

_PNG = b"\x89PNG\r\n\x1a\n"
_SHARE_CHUNK = b"caBX"
_SKIP = {"SHA256SUMS", "EVIDENCE.sha256"}


def fingerprint(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    if not data.startswith(_PNG):
        return hashlib.sha256(data).hexdigest(), path.name
    h = hashlib.sha256(_PNG)
    pos, index = len(_PNG), 0
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        end = pos + 12 + length
        # only where the share puts it: the second chunk, right after IHDR
        if not (index == 1 and data[pos + 4 : pos + 8] == _SHARE_CHUNK):
            h.update(data[pos:end])
        pos, index = end, index + 1
    h.update(data[pos:])  # a truncated chunk or bytes after IEND still count
    return h.hexdigest(), f"{path.name} [png-no-caBX]"


def listing(folder: Path) -> list[str]:
    return [
        "{}  {}".format(*fingerprint(p))
        for p in sorted(folder.iterdir())
        if p.is_file() and p.name not in _SKIP
    ]


def main(argv: list[str]) -> int:
    folder = Path(argv[1])
    if len(argv) == 4 and argv[2] == "--check":
        saved = {line for line in Path(argv[3]).read_text().splitlines() if line}
        now = set(listing(folder))
        problems = [f"CHANGED or MISSING: {line}" for line in sorted(saved - now)]
        problems += [f"NEW or CHANGED: {line}" for line in sorted(now - saved)]
        print("\n".join(problems) if problems else "OK")
        return 1 if problems else 0
    print("\n".join(listing(folder)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
