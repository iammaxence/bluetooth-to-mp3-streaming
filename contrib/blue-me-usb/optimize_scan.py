# Verify adaptive PCM state changes, buffering limits, and audio transitions.
from pathlib import Path
import subprocess, sys

root = Path(sys.argv[1])
p = root / "stream/fs.go"
body = p.read_text()
old = '''\tcurBlock := make([]byte, 4)
\tfor i := int64(0); i < stat.Size()-1; i++ {
\t\t_, err := diskFile.ReadAt(curBlock, i)
\t\tif err != nil {
\t\t\tpanic(err)
\t\t}
'''
new = '''\t// Cache sequential reads instead of issuing a system call for every byte.
\tblock := make([]byte, 1024*1024)
\tblockStart := int64(-1)
\tblockSize := 0
\tfor i := int64(0); i+4 <= stat.Size(); i++ {
\t\tif blockStart < 0 || i < blockStart || i+4 > blockStart+int64(blockSize) {
\t\t\tblockStart = i
\t\t\tblockSize, err = diskFile.ReadAt(block, i)
\t\t\tif err != nil && err != io.EOF {
\t\t\t\tpanic(err)
\t\t\t}
\t\t}
\t\tcurBlock := block[i-blockStart : i-blockStart+4]
'''
if old in body:
    assert body.count(old) == 1
    p.with_name("fs.go.before-block-scan").write_text(body)
    body = body.replace(old, new).replace('\t"errors"\n', '\t"errors"\n\t"io"\n')
    p.write_text(body)
else:
    assert "// Cache sequential reads" in body
subprocess.run(["gofmt", "-w", str(p)], check=True)
