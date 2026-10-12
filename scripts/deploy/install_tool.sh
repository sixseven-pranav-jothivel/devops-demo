#!/usr/bin/env bash
# Install a pinned, checksum-verified release binary (no marketplace action, no curl | sh).
#
#   scripts/deploy/install_tool.sh <trivy|actionlint|k6> [install-dir]
#
# The SHA-256 values below were verified against the upstream release checksum files on 2026-10-10
# (trivy v0.75.0, actionlint v1.7.12, k6 v2.3.0). Bumping a version means updating the version AND all hashes.
# Supported: linux/amd64 (GitHub runners) and linux/arm64 (dev VM).
set -euo pipefail

TOOL="${1:?usage: install_tool.sh <trivy|actionlint|k6> [install-dir]}"
DEST="${2:-${HOME}/.local/bin}"

TRIVY_VERSION="0.75.0"
TRIVY_SHA256_AMD64="c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f"
TRIVY_SHA256_ARM64="a1ee9f6ffb7d112b64ff726a2a0717c21175c1114361391f4a132956751a13b3"

ACTIONLINT_VERSION="1.7.12"
ACTIONLINT_SHA256_AMD64="8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
ACTIONLINT_SHA256_ARM64="325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6"

K6_VERSION="2.3.0"
K6_SHA256_AMD64="39c3117b6af817592dcd0ce4242105c0a7af10948c2a425306f0be8f7a8a8ab1"
K6_SHA256_ARM64="5ca3433e8201da72a284aaa241a1bb5fb47f4abb4e384d39410ddd8062f49b90"

case "$(uname -m)" in
  x86_64 | amd64) ARCH="amd64" ;;
  aarch64 | arm64) ARCH="arm64" ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 2 ;;
esac

case "${TOOL}" in
  trivy)
    if [ "${ARCH}" = "amd64" ]; then
      FILE="trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz"; SUM="${TRIVY_SHA256_AMD64}"
    else
      FILE="trivy_${TRIVY_VERSION}_Linux-ARM64.tar.gz"; SUM="${TRIVY_SHA256_ARM64}"
    fi
    URL="https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/${FILE}"
    ;;
  actionlint)
    FILE="actionlint_${ACTIONLINT_VERSION}_linux_${ARCH}.tar.gz"
    if [ "${ARCH}" = "amd64" ]; then SUM="${ACTIONLINT_SHA256_AMD64}"; else SUM="${ACTIONLINT_SHA256_ARM64}"; fi
    URL="https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/${FILE}"
    ;;
  k6)
    FILE="k6-v${K6_VERSION}-linux-${ARCH}.tar.gz"
    if [ "${ARCH}" = "amd64" ]; then SUM="${K6_SHA256_AMD64}"; else SUM="${K6_SHA256_ARM64}"; fi
    URL="https://github.com/grafana/k6/releases/download/v${K6_VERSION}/${FILE}"
    MEMBER="k6-v${K6_VERSION}-linux-${ARCH}/k6"
    ;;
  *) echo "unknown tool: ${TOOL}" >&2; exit 2 ;;
esac
MEMBER="${MEMBER:-${TOOL}}"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
curl --fail --silent --show-error --location --retry 3 --output "${WORK}/${FILE}" "${URL}"
echo "${SUM}  ${WORK}/${FILE}" | sha256sum --check --strict -
mkdir -p "${DEST}" "${WORK}/x"
tar -xzf "${WORK}/${FILE}" -C "${WORK}/x" "${MEMBER}"
install -m 0755 "${WORK}/x/${MEMBER}" "${DEST}/${TOOL}"
echo "installed ${TOOL} -> ${DEST}/${TOOL}"
