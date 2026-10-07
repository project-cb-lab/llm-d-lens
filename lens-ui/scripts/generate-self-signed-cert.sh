#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Ensures a self-signed TLS certificate exists for running Prism over HTTPS on
# a bare-metal box that only has an internal IP (no public DNS name, so a real
# Let's Encrypt certificate isn't an option). Browsers still treat HTTPS with
# a self-signed cert as a "secure context" (once the one-time warning is
# accepted), which is enough to unlock features like microphone access that
# require a secure context.
#
# Idempotent: if a cert already exists for the requested host, it's reused
# as-is (so scripts/dev.sh and the installer can call this on every start
# without regenerating/rotating certs each time). Pass --force to regenerate.
#
# Usage:
#   ./scripts/generate-self-signed-cert.sh [host_or_ip] [output_dir] [--force]
#
# Examples:
#   ./scripts/generate-self-signed-cert.sh 10.0.5.23
#   ./scripts/generate-self-signed-cert.sh prism.internal.example.com ./certs
#
# On success, prints two lines to stdout (and nothing else) so callers can
# eval them directly:
#   TLS_CERT_FILE=/abs/path/prism.crt
#   TLS_KEY_FILE=/abs/path/prism.key
set -euo pipefail

FORCE=0
POSITIONAL=()
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        *) POSITIONAL+=("$arg") ;;
    esac
done

HOST="${POSITIONAL[0]:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
OUT_DIR="${POSITIONAL[1]:-./certs}"
DAYS="${TLS_CERT_DAYS:-825}"

if [[ -z "$HOST" ]]; then
    echo "Could not auto-detect this machine's IP. Pass it explicitly:" >&2
    echo "  $0 <host_or_ip> [output_dir]" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"
KEY_FILE="$OUT_DIR/prism.key"
CERT_FILE="$OUT_DIR/prism.crt"

# Reuse an existing cert if it's already valid for this host and not expired,
# unless the caller explicitly asked to regenerate.
if [[ "$FORCE" != "1" && -s "$CERT_FILE" && -s "$KEY_FILE" ]] \
    && openssl x509 -in "$CERT_FILE" -noout -checkend 86400 >/dev/null 2>&1 \
    && openssl x509 -in "$CERT_FILE" -noout -text 2>/dev/null | grep -qF "$HOST"; then
    echo "TLS_CERT_FILE=$(cd "$OUT_DIR" && pwd)/prism.crt"
    echo "TLS_KEY_FILE=$(cd "$OUT_DIR" && pwd)/prism.key"
    exit 0
fi

# Build the subjectAltName list. Modern browsers ignore the CN and require a
# matching SAN entry, so add the target as an IP SAN if it looks like one,
# otherwise as a DNS SAN; always include localhost/127.0.0.1 too so the same
# cert also works over an SSH port-forward.
if [[ "$HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN="IP:$HOST,DNS:localhost,IP:127.0.0.1"
else
    SAN="DNS:$HOST,DNS:localhost,IP:127.0.0.1"
fi

openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -days "$DAYS" \
    -subj "/CN=$HOST" \
    -addext "subjectAltName=$SAN" >/dev/null 2>&1

chmod 600 "$KEY_FILE"

echo "TLS_CERT_FILE=$(cd "$OUT_DIR" && pwd)/prism.crt"
echo "TLS_KEY_FILE=$(cd "$OUT_DIR" && pwd)/prism.key"
