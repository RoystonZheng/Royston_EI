#!/usr/bin/env bash
set -euo pipefail

package_root="$(cd "$(dirname "$0")" && pwd)"
query_file="$package_root/raw/osm/porto_candidate.overpass.ql"
output_file="$package_root/raw/osm/porto_candidate.osm"
partial_file="$output_file.partial"
endpoint='https://overpass-api.de/api/interpreter'

if [[ -e "$output_file" ]]; then
  echo "Refusing to replace existing file: $output_file" >&2
  exit 2
fi

curl --fail --location --silent --show-error --max-time 300 \
  --user-agent 'porto-hybrid-real-map-dispatch/1.0 (research data snapshot)' \
  --header 'Accept: application/xml' \
  --data-urlencode "data@$query_file" \
  --output "$partial_file" \
  "$endpoint"

python3 - "$partial_file" <<'PY'
import json
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1]
root = ET.parse(path).getroot()
if root.tag != "osm":
    raise SystemExit(f"Expected OSM XML root; received {root.tag!r}")
counts = {"node": 0, "way": 0, "relation": 0}
for element in root:
    if element.tag in counts:
        counts[element.tag] += 1
meta = next((element.attrib for element in root if element.tag == "meta"), {})
if counts["way"] == 0 or counts["node"] == 0:
    raise SystemExit(f"OSM response contains no usable road geometry: {counts}")
print(json.dumps({"counts": counts, "meta": meta}, ensure_ascii=False))
PY

mv "$partial_file" "$output_file"
shasum -a 256 "$output_file"
