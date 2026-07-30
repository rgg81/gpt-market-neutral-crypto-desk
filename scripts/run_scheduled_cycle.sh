#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly USER_LOCAL_BIN="${XDG_BIN_HOME:-${HOME:-}/.local/bin}"
export PATH="${USER_LOCAL_BIN}:/usr/local/bin:/usr/bin:/bin${PATH:+:${PATH}}"

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly DESK_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly CODEX_BIN="${CODEX_BIN_OVERRIDE:-$(command -v codex || true)}"
readonly UV_BIN="${UV_BIN_OVERRIDE:-$(command -v uv || true)}"
readonly FLOCK_BIN="/usr/bin/flock"
readonly TIMEOUT_BIN="/usr/bin/timeout"
readonly DATE_BIN="/usr/bin/date"
readonly PROMPT_FILE="${DESK_ROOT}/ops/desk-cycle-prompt.md"
readonly LOG_DIR="${DESK_ROOT}/logs"
readonly LOCK_FILE="${LOG_DIR}/desk-cycle.lock"
readonly MODEL="gpt-5.6-sol"
readonly EFFORT="xhigh"
readonly MAX_RUNTIME="${MAX_RUNTIME_OVERRIDE:-100m}"

export UV_CACHE_DIR="${DESK_ROOT}/.uv-cache"
unset OPENAI_API_KEY AZURE_OPENAI_API_KEY CODEX_API_KEY

timestamp() {
    "${DATE_BIN}" -u +"%Y-%m-%dT%H:%M:%SZ"
}

preflight() {
    test -n "${CODEX_BIN}"
    test -n "${UV_BIN}"
    test -x "${CODEX_BIN}"
    test -x "${UV_BIN}"
    test -x "${FLOCK_BIN}"
    test -x "${TIMEOUT_BIN}"
    test -s "${PROMPT_FILE}"
    "${CODEX_BIN}" login status
    (
        cd "${DESK_ROOT}"
        "${UV_BIN}" lock --check
        "${UV_BIN}" run python scripts/reflector_apply.py \
            --memory-dir live_memory --agents-dir agents --check-existing
    )
    echo "READY model=${MODEL} effort=${EFFORT} schedule=00:07/08:07/16:07Z"
}

if [[ "${1:-}" == "--check" ]]; then
    preflight
    exit 0
fi

if [[ "${1:-}" == "--scheduled" ]]; then
    readonly UTC_SLOT="$("${DATE_BIN}" -u +'%H:%M')"
    case "${UTC_SLOT}" in
        00:07|08:07|16:07) ;;
        *)
            echo "$(timestamp) scheduler guard: ${UTC_SLOT}Z is not a funding-aligned slot; stand down"
            exit 0
            ;;
    esac
elif [[ -n "${1:-}" ]]; then
    echo "usage: $0 [--check|--scheduled]" >&2
    exit 2
fi

mkdir -p "${LOG_DIR}" "${DESK_ROOT}/live_state" "${DESK_ROOT}/live_memory"
exec 9>"${LOCK_FILE}"
if ! "${FLOCK_BIN}" -n 9; then
    echo "$(timestamp) overlap guard: another v2 desk cycle owns ${LOCK_FILE}; stand down"
    exit 0
fi

preflight
echo "$(timestamp) starting GPT desk cycle model=${MODEL} effort=${EFFORT}"

set +e
(
    cd "${DESK_ROOT}"
    "${TIMEOUT_BIN}" --signal=TERM --kill-after=5m "${MAX_RUNTIME}" \
        "${CODEX_BIN}" \
        --enable multi_agent \
        --search \
        --model "${MODEL}" \
        --sandbox workspace-write \
        --ask-for-approval never \
        --cd "${DESK_ROOT}" \
        --config "model_reasoning_effort=\"${EFFORT}\"" \
        --config "sandbox_workspace_write.network_access=true" \
        exec \
        --skip-git-repo-check \
        "$(<"${PROMPT_FILE}")"
)
status=$?
set -e

if [[ "${status}" -eq 124 ]]; then
    echo "$(timestamp) GPT desk cycle timed out after ${MAX_RUNTIME}; prior completed book stands"
elif [[ "${status}" -ne 0 ]]; then
    echo "$(timestamp) GPT desk cycle failed with exit=${status}; inspect this log before retrying"
else
    echo "$(timestamp) GPT desk cycle finished successfully"
fi
exit "${status}"
