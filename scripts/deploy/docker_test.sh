#!/usr/bin/env bash
# Run the built image for real and prove it behaves: starts it (non-default $PORT on purpose), waits for
# the Docker HEALTHCHECK to report healthy, runs the smoke test (version must equal the baked GIT_SHA),
# runs both image guards (ours, inside the image, and the demo engine's), then stops it and checks the SIGTERM shutdown was graceful.
#
#   scripts/deploy/docker_test.sh <image> <expected-git-sha>
#
# Environment: DOCKER (default "docker"; "sudo docker" on the dev VM), PLATFORM (default linux/amd64),
#   CONTAINER (default stockflow-test), HOST_PORT (default: a free port), START_TIMEOUT (default 180 s;
#   an emulated amd64 container on arm64 needs about 20 s to start).
set -euo pipefail

IMAGE="${1:?usage: docker_test.sh <image> <expected-git-sha>}"
EXPECT_SHA="${2:?usage: docker_test.sh <image> <expected-git-sha>}"
DOCKER="${DOCKER:-docker}"
PLATFORM="${PLATFORM:-linux/amd64}"
CONTAINER="${CONTAINER:-stockflow-test}"
if [ -z "${HOST_PORT:-}" ]; then # a free port, so concurrent runs (or a busy 18080) cannot collide
  HOST_PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
fi
CONTAINER_PORT=9123 # not the image default (8000): proves $PORT is respected
START_TIMEOUT="${START_TIMEOUT:-180}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"

cleanup() { ${DOCKER} rm -f "${CONTAINER}" >/dev/null 2>&1; }
dump_logs() { echo "---- container logs ----"; ${DOCKER} logs "${CONTAINER}" 2>&1 | tail -n 40; echo "------------------------"; }
trap cleanup EXIT
cleanup

echo "== start ${IMAGE} (${PLATFORM}) on host port ${HOST_PORT}, container PORT=${CONTAINER_PORT}"
${DOCKER} run -d --name "${CONTAINER}" --platform "${PLATFORM}" \
  -e PORT="${CONTAINER_PORT}" -p "127.0.0.1:${HOST_PORT}:${CONTAINER_PORT}" \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  --health-interval 5s --health-start-period 5s --health-timeout 5s --health-retries 3 \
  "${IMAGE}" >/dev/null

echo "== wait for HEALTHCHECK to report healthy (max ${START_TIMEOUT}s)"
deadline=$((SECONDS + START_TIMEOUT))
status=starting
while [ "${SECONDS}" -lt "${deadline}" ]; do
  status="$(${DOCKER} inspect --format '{{if .State.Running}}{{.State.Health.Status}}{{else}}exited{{end}}' "${CONTAINER}")"
  [ "${status}" = "healthy" ] && break
  [ "${status}" = "exited" ] && break
  sleep 2
done
echo "health status: ${status}"
if [ "${status}" != "healthy" ]; then dump_logs; echo "FAIL: container did not become healthy" >&2; exit 1; fi

echo "== smoke test (expects /api/version git_sha ${EXPECT_SHA})"
if ! uv run python "${ROOT}/scripts/smoke_test.py" --base-url "http://127.0.0.1:${HOST_PORT}" \
  --expect-sha "${EXPECT_SHA}" --timeout 60; then
  dump_logs; echo "FAIL: smoke test" >&2; exit 1
fi

echo "== image guard"
${DOCKER} run --rm -i --platform "${PLATFORM}" --entrypoint sh "${IMAGE}" -s <"${HERE}/image_guard.sh"

echo "== demo engine image guard (python -m demo image-guard)"
(cd "${ROOT}" && uv run python -m demo image-guard "${IMAGE}")

echo "== graceful shutdown (SIGTERM -> exit 0 within 10 s)"
start=${SECONDS}
${DOCKER} stop --timeout 10 "${CONTAINER}" >/dev/null
elapsed=$((SECONDS - start))
code="$(${DOCKER} inspect --format '{{.State.ExitCode}}' "${CONTAINER}")"
echo "stopped in ${elapsed}s with exit code ${code}"
if [ "${code}" != "0" ]; then dump_logs; echo "FAIL: container did not exit cleanly on SIGTERM (exit ${code})" >&2; exit 1; fi

echo "docker-test PASSED: ${IMAGE}"
