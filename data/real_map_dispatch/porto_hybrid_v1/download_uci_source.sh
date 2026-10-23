#!/usr/bin/env bash
set -euo pipefail

package_root="$(cd "$(dirname "$0")" && pwd)"
output_file="$package_root/raw/uci/train.csv.zip"

if [[ -e "$output_file" ]]; then
  echo "Refusing to replace existing file: $output_file" >&2
  exit 2
fi

mkdir -p "$(dirname "$output_file")"
python3 - "$output_file" <<'PY'
import concurrent.futures
import hashlib
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

OUTPUT = sys.argv[1]
URL = "https://ndownloader.figshare.com/files/43611822"
EXPECTED_SIZE = 540_159_049
EXPECTED_MD5 = "520c5c1b4d753e1b96e869d41a12fdda"
CHUNK_SIZE = 25 * 1024 * 1024
WORKERS = 4


def download_chunk(index, start, end, work_dir):
    expected_length = end - start + 1
    chunk_path = os.path.join(work_dir, f"chunk-{index:03d}")
    last_error = None
    for attempt in range(1, 5):
        try:
            request = urllib.request.Request(
                URL,
                headers={
                    "Range": f"bytes={start}-{end}",
                    "User-Agent": "porto-hybrid-real-map-dispatch-data-prep/1.0",
                },
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                if response.status != 206:
                    raise RuntimeError(f"chunk {index}: expected HTTP 206, got {response.status}")
                content_range = response.headers.get("Content-Range", "")
                expected_range = f"bytes {start}-{end}/{EXPECTED_SIZE}"
                if content_range != expected_range:
                    raise RuntimeError(
                        f"chunk {index}: unexpected Content-Range {content_range!r}; "
                        f"expected {expected_range!r}"
                    )
                received = 0
                with open(chunk_path + ".tmp", "wb") as out:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        out.write(block)
                        received += len(block)
                if received != expected_length:
                    raise RuntimeError(
                        f"chunk {index}: received {received} bytes, expected {expected_length}"
                    )
            os.replace(chunk_path + ".tmp", chunk_path)
            return chunk_path
        except (OSError, urllib.error.URLError, RuntimeError) as exc:
            last_error = exc
            for path in (chunk_path + ".tmp", chunk_path):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            if attempt < 4:
                time.sleep(attempt * 2)
    raise RuntimeError(f"chunk {index} failed after 4 attempts: {last_error}")


def digest(path, algorithm):
    hasher = hashlib.new(algorithm)
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


work_dir = tempfile.mkdtemp(prefix="porto-uci-download-", dir=os.path.dirname(OUTPUT))
assembled_path = OUTPUT + ".partial"
try:
    ranges = []
    start = 0
    index = 0
    while start < EXPECTED_SIZE:
        end = min(start + CHUNK_SIZE, EXPECTED_SIZE) - 1
        ranges.append((index, start, end, work_dir))
        start = end + 1
        index += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(download_chunk, *item) for item in ranges]
        paths = [future.result() for future in futures]

    with open(assembled_path, "wb") as assembled:
        for path in paths:
            with open(path, "rb") as chunk:
                shutil.copyfileobj(chunk, assembled, length=8 * 1024 * 1024)

    actual_size = os.path.getsize(assembled_path)
    if actual_size != EXPECTED_SIZE:
        raise RuntimeError(f"assembled size {actual_size} does not match {EXPECTED_SIZE}")
    actual_md5 = digest(assembled_path, "md5")
    if actual_md5 != EXPECTED_MD5:
        raise RuntimeError(f"MD5 mismatch: expected {EXPECTED_MD5}, got {actual_md5}")
    with zipfile.ZipFile(assembled_path) as archive:
        damaged_member = archive.testzip()
        if damaged_member is not None:
            raise RuntimeError(f"ZIP CRC check failed for {damaged_member}")

    actual_sha256 = digest(assembled_path, "sha256")
    os.replace(assembled_path, OUTPUT)
    print(f"verified_size_bytes={actual_size}")
    print(f"verified_md5={actual_md5}")
    print(f"sha256={actual_sha256}")
    print(f"output={OUTPUT}")
finally:
    try:
        os.remove(assembled_path)
    except FileNotFoundError:
        pass
    shutil.rmtree(work_dir, ignore_errors=True)
PY
