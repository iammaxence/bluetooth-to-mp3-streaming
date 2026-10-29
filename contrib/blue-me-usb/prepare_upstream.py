#!/usr/bin/env python3
"""Bound the upstream demo queue and restrict its socket to localhost."""

from pathlib import Path
import sys

# Locate the historical upstream checkout passed on the command line.
root = Path(sys.argv[1])
# Apply the legacy queue and receiver adjustments to that checkout.
source = root / "stream/bufferqueue.go"
body = source.read_text()
needle = "\tbq.buffer = append(bq.buffer, bytes...)\n"
assert body.count(needle) == 1, "Unexpected upstream queue"
body = body.replace(
    needle,
    needle + """\t// Keep roughly ten seconds while the radio is idle.
\tconst maximum = 160 * 1024
\tif len(bq.buffer) > maximum {
\t\tbq.buffer = append([]byte(nil), bq.buffer[len(bq.buffer)-maximum:]...)
\t}
""",
)
for line in [
    '\t"fmt"\n',
    '\tfmt.Printf("[BQ] Buffer size: %d\\n", len(bq.buffer))\n',
    '\tbefore := len(bq.buffer)\n',
    '\tafter := len(bq.buffer)\n',
    '\tfmt.Printf("[BQ] New buffer size: %d -> %d\\n", before, after)\n',
]:
    body = body.replace(line, "")
source.write_text(body)
source = root / "stream/receiver.go"
body = source.read_text()
assert body.count('net.Listen("tcp", ":5000")') == 1
source.write_text(
    body.replace('net.Listen("tcp", ":5000")', 'net.Listen("tcp", "127.0.0.1:5000")')
)
print("Applied bounded queue, quiet queue logging, localhost socket.")
