#!/usr/bin/env bash
# Append the tail of a report to the GitHub job summary: summarize.sh <title> <file> [lines]
# Absent reports are stated, not hidden. Reads nothing from untrusted input.
set -euo pipefail
title="${1:?usage: summarize.sh <title> <file> [lines]}"
file="${2:?usage: summarize.sh <title> <file> [lines]}"
lines="${3:-40}"
out="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
{
  echo "### ${title}"
  if [ -s "${file}" ]; then
    echo '```text'
    tail -n "${lines}" "${file}"
    echo '```'
  else
    echo "_no report was produced (${file}); see the failed step above_"
  fi
} >>"${out}"
