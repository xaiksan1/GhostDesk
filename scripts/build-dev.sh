#!/usr/bin/env bash
# Build ghostdesk:dev image with exec_shell + browser-harness + CLI-Anything
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$(dirname "$SCRIPT_DIR")"

BROWSER_HARNESS_DIR="/home/ichigo/alexandria/browser-harness"
CLI_ANYTHING_DIR="/home/ichigo/alexandria/CLI-Anything"

for d in "$BROWSER_HARNESS_DIR" "$CLI_ANYTHING_DIR"; do
    if [[ ! -d "$d" ]]; then
        echo "ERROR: $d not found" >&2
        exit 1
    fi
done

echo "Building ghostdesk:dev ..."
docker buildx build \
    --build-context "browser-harness=${BROWSER_HARNESS_DIR}" \
    --build-context "cli-anything=${CLI_ANYTHING_DIR}" \
    -f "${BUILD_DIR}/Dockerfile.dev" \
    -t ghostdesk:dev \
    "${BUILD_DIR}"

echo "Done. To restart container:"
echo "  docker rm -f ghostdesk && docker run -d --name ghostdesk -p 3000:3000 -p 6080:6080 ghostdesk:dev"
