#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly USER_LOCAL_BIN="${XDG_BIN_HOME:-${HOME:-}/.local/bin}"
export PATH="${USER_LOCAL_BIN}:/usr/local/bin:/usr/bin:/bin${PATH:+:${PATH}}"

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly DESK_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly UV_BIN="${UV_BIN_OVERRIDE:-$(command -v uv || true)}"
readonly PYTHON_BIN="${PYTHON_BIN_OVERRIDE:-$(command -v python3 || true)}"
readonly CURL_BIN="${CURL_BIN_OVERRIDE:-$(command -v curl || true)}"
readonly FLOCK_BIN="/usr/bin/flock"
readonly TIMEOUT_BIN="/usr/bin/timeout"
readonly DATE_BIN="/usr/bin/date"
readonly LOCK_FILE="${DESK_ROOT}/logs/desk-cycle.lock"
readonly CLAIMS_FILE="${DESK_ROOT}/logs/desk-schedule-claims.json"
readonly SCHEDULE_GATE="${SCRIPT_DIR}/desk_schedule_gate.py"

export UV_CACHE_DIR="${DESK_ROOT}/.uv-cache"
unset OPENAI_API_KEY AZURE_OPENAI_API_KEY CODEX_API_KEY

timestamp() {
    "${DATE_BIN}" -u +"%Y-%m-%dT%H:%M:%SZ"
}

preflight() {
    test -n "${UV_BIN}"
    test -n "${PYTHON_BIN}"
    test -n "${CURL_BIN}"
    test -x "${UV_BIN}"
    test -x "${PYTHON_BIN}"
    test -x "${CURL_BIN}"
    test -x "${FLOCK_BIN}"
    test -x "${TIMEOUT_BIN}"
    test -s "${SCHEDULE_GATE}"
    "${CURL_BIN}" -fsS --max-time 10 https://fapi.binance.com/fapi/v1/time >/dev/null
    (
        cd "${DESK_ROOT}"
        "${UV_BIN}" lock --check
    )
    echo "READY token-free PAPER heartbeat schedule=08:07/16:07Z"
}

if [[ "${1:-}" == "--check" ]]; then
    preflight
    exit 0
fi

readonly SCHEDULED="${1:-}"
if [[ "${SCHEDULED}" == "--scheduled" ]]; then
    :
elif [[ -n "${1:-}" ]]; then
    echo "usage: $0 [--check|--scheduled]" >&2
    exit 2
fi

schedule_gate() {
    local action="$1"
    local output status
    set +e
    output="$("${PYTHON_BIN}" "${SCHEDULE_GATE}" --kind heartbeat \
        --claims "${CLAIMS_FILE}" "${action}" 2>&1)"
    status=$?
    set -e
    if [[ "${status}" -eq 3 ]]; then
        return 1
    fi
    if [[ "${status}" -ne 0 ]]; then
        echo "$(timestamp) heartbeat schedule gate failed: ${output}" >&2
        exit "${status}"
    fi
    echo "${output}"
}

if [[ "${SCHEDULED}" == "--scheduled" ]] && ! schedule_gate --check; then
    exit 0
fi

mkdir -p "${DESK_ROOT}/logs" "${DESK_ROOT}/live_state"
exec 9>"${LOCK_FILE}"
if ! "${FLOCK_BIN}" -n 9; then
    echo "$(timestamp) overlap guard: another v2 desk task owns ${LOCK_FILE}; stand down"
    exit 0
fi

if [[ "${SCHEDULED}" == "--scheduled" ]] && ! schedule_gate --check; then
    exit 0
fi

preflight
SCHEDULE_SLOT=""
if [[ "${SCHEDULED}" == "--scheduled" ]]; then
    set +e
    claim_output="$(schedule_gate --claim)"
    claim_status=$?
    set -e
    if [[ "${claim_status}" -ne 0 ]]; then
        exit 0
    fi
    echo "${claim_output}"
    SCHEDULE_SLOT="$("${PYTHON_BIN}" -c \
        'import json,sys; print(json.loads(sys.argv[1])["slot"])' "${claim_output}")"
fi
readonly SCHEDULE_SLOT
echo "$(timestamp) starting token-free PAPER funding heartbeat"

heartbeat_args=(scripts/desk_heartbeat.py --state-dir live_state)
if [[ -n "${SCHEDULE_SLOT}" ]]; then
    heartbeat_args+=(--schedule-slot "${SCHEDULE_SLOT}")
fi
set +e
(
    cd "${DESK_ROOT}"
    "${TIMEOUT_BIN}" --signal=TERM --kill-after=30s 10m \
        "${UV_BIN}" run python "${heartbeat_args[@]}"
)
status=$?
set -e

if [[ "${status}" -eq 0 ]]; then
    echo "$(timestamp) token-free PAPER funding heartbeat finished successfully"
else
    echo "$(timestamp) token-free PAPER funding heartbeat failed with exit=${status}; prior position quantities stand"
    if [[ -n "${SCHEDULE_SLOT}" ]]; then
        if schedule_gate --release; then
            echo "$(timestamp) released failed heartbeat slot for retry within grace window"
        else
            echo "$(timestamp) heartbeat slot could not be released; inspect claims manually" >&2
        fi
    fi
fi
exit "${status}"
