#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 4 || $# -gt 5 ]]; then
    echo "usage: $0 URL SIZE_BYTES DESTINATION SHA256 [PARTS]" >&2
    exit 2
fi

url=$1
size=$2
destination=$3
expected_sha=$4
parts=${5:-8}
part_dir="${destination}.parts"
temporary="${destination}.partial"

mkdir -p "$(dirname "$destination")" "$part_dir"
rm -f "$temporary"

chunk=$(((size + parts - 1) / parts))
pids=()

for ((index = 0; index < parts; index++)); do
    start=$((index * chunk))
    ((start < size)) || break
    end=$((start + chunk - 1))
    ((end < size)) || end=$((size - 1))
    expected=$((end - start + 1))
    part=$(printf "%s/part-%03d" "$part_dir" "$index")

    if [[ -f "$part" ]] && [[ $(stat -c %s "$part") -eq $expected ]]; then
        echo "reuse part $index bytes=$expected"
        continue
    fi

    rm -f "$part"
    (
        curl \
            --fail \
            --silent \
            --show-error \
            --retry 20 \
            --retry-all-errors \
            --connect-timeout 15 \
            --range "${start}-${end}" \
            --output "${part}.partial" \
            "$url"
        actual=$(stat -c %s "${part}.partial")
        if [[ $actual -ne $expected ]]; then
            echo "part $index size mismatch: expected=$expected actual=$actual" >&2
            exit 1
        fi
        mv "${part}.partial" "$part"
        echo "completed part $index bytes=$actual"
    ) &
    pids+=("$!")
done

for pid in "${pids[@]}"; do
    wait "$pid"
done

find "$part_dir" -maxdepth 1 -type f -name 'part-*' -print0 \
    | sort -z \
    | xargs -0 cat > "$temporary"

actual_size=$(stat -c %s "$temporary")
if [[ $actual_size -ne $size ]]; then
    echo "assembled size mismatch: expected=$size actual=$actual_size" >&2
    exit 1
fi

actual_sha=$(sha256sum "$temporary" | awk '{print $1}')
if [[ $actual_sha != "$expected_sha" ]]; then
    echo "sha256 mismatch: expected=$expected_sha actual=$actual_sha" >&2
    exit 1
fi

mv "$temporary" "$destination"
rm -rf "$part_dir"
echo "verified $destination size=$actual_size sha256=$actual_sha"
