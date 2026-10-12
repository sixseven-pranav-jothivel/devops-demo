#!/bin/sh
# Image guard (docs/SECURITY.md 10.7). Runs INSIDE the image, fed on stdin so nothing is mounted:
#
#   docker run --rm -i --entrypoint sh "$IMAGE" -s < scripts/deploy/image_guard.sh
#
# Fails (exit 1) when the image carries test/demo/CI material, a defect marker, build tooling,
# or runs as root.
# shellcheck disable=SC2329  # helpers are invoked indirectly through check()
fail=0
check() { # check <description> <command...>
  desc="$1"; shift
  if "$@"; then echo "PASS  $desc"; else echo "FAIL  $desc"; fail=1; fi
}
absent() { # none of the given paths exists
  for p in "$@"; do [ -e "$p" ] && { echo "      found: $p"; return 1; }; done
  return 0
}
no_command() { # none of the given commands is on PATH
  for c in "$@"; do command -v "$c" >/dev/null 2>&1 && { echo "      found: $c"; return 1; }; done
  return 0
}
no_suid() { # no setuid/setgid file anywhere on the image filesystem
  found="$(find / -xdev -perm /6000 -type f 2>/dev/null)"
  [ -z "$found" ] || { echo "$found" | sed 's/^/      found: /'; return 1; }
}
no_marker() { ! grep -rIl "DEMO-DEFECT" /app 2>/dev/null | grep . ; }

check "runs as a non-root user (uid $(id -u))" test "$(id -u)" -ne 0
check "no demo/, tests/, load-tests/, docs/, .github/, .demo-state/, scripts/, reports/ in /app" \
  absent /app/demo /app/tests /app/load-tests /app/docs /app/.github /app/.demo-state /app/scripts /app/reports /demo /tests
check "no DEMO-DEFECT marker in /app" no_marker
check "no git metadata, env files or local databases in /app" \
  absent /app/.git /app/.env /app/stockflow.db /app/Dockerfile /app/compose.yaml
check "no package installers or build tools (uv, pip, gcc, make, git)" no_command uv uvx pip pip3 gcc cc make git
check "no setuid/setgid binaries left" no_suid
check "application code is not writable by the runtime user" test ! -w /app/app/main.py
check "application package is present" test -f /app/app/main.py
exit "$fail"
