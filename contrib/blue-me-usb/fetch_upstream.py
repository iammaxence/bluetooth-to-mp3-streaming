#!/usr/bin/env python3
"""Download a pinned source archive with retries, then publish a verified checkout."""

import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request


# Reuse only a checkout or source directory matching the requested revision.
def existing_revision(target, revision):
    """Reuse only a checkout or source directory matching the requested revision."""
    if not target.exists():
        return False
    marker = target / ".radionav-revision"
    if marker.is_file() and marker.read_text().strip() == revision:
        return True
    if (target / ".git").is_dir():
        result = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stdout.strip() == revision:
            return True
    raise RuntimeError("Existing upstream directory has another or unknown revision")


# Extract only a complete, bounded archive into a new staging directory.
def fetch(revision, target):
    """Extract only a complete, bounded archive into a new staging directory."""
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Expected a complete Git commit ID")
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if existing_revision(target, revision):
        print("Pinned source already available.", flush=True)
        return
    prefix = "gambiconf-usb-streaming-" + revision
    url = (
        "https://codeload.github.com/rgsilva/gambiconf-usb-streaming/tar.gz/" + revision
    )
    with tempfile.TemporaryDirectory(
        prefix=".radionav-download-", dir=target.parent
    ) as work:
        stage = Path(work)
        archive_path = stage / "source.tar.gz"
        for attempt in range(1, 6):
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "radionav-usb-installer"}
                )
                print(
                    "Downloading pinned source archive, attempt " + str(attempt),
                    flush=True,
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    with archive_path.open("wb") as output:
                        total = 0
                        while chunk := response.read(65536):
                            total += len(chunk)
                            if total > 8 * 1024 * 1024:
                                raise ValueError("Archive exceeds expected size")
                            output.write(chunk)
                with tarfile.open(archive_path, mode="r:gz") as archive:
                    members = archive.getmembers()
                    if not members or sum(m.size for m in members) > 16 * 1024 * 1024:
                        raise ValueError("Unexpected archive contents")
                    for member in members:
                        parts = Path(member.name).parts
                        if not parts or parts[0] != prefix or ".." in parts:
                            raise ValueError("Unexpected archive path")
                        if not (member.isfile() or member.isdir()):
                            raise ValueError("Archive contains a link or special file")
                break
            except (OSError, EOFError, tarfile.TarError) as error:
                print("Download interrupted: " + str(error), flush=True)
                if attempt == 5:
                    raise
                time.sleep(min(2**attempt, 8))
        with tarfile.open(archive_path, mode="r:gz") as archive:
            archive.extractall(stage, filter="data")
        source = stage / prefix
        required = [
            "go.mod",
            "cmd/streamfs.go",
            "stream/file.go",
            "files/demo3/imagem.raw.tar.gz",
        ]
        if not all((source / name).is_file() for name in required):
            raise ValueError("Required source files are missing")
        (source / ".radionav-revision").write_text(revision + "\n")
        source.rename(target)
    print(
        json.dumps({"source": str(target), "revision": revision, "complete": True}),
        flush=True,
    )


if __name__ == "__main__":
    fetch(sys.argv[1], Path(sys.argv[2]))
