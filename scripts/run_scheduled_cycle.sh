#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly USER_LOCAL_BIN="${XDG_BIN_HOME:-${HOME:-}/.local/bin}"
export PATH="${USER_LOCAL_BIN}:/usr/local/bin:/usr/bin:/bin${PATH:+:${PATH}}"

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly DESK_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly CODEX_BIN="${CODEX_BIN_OVERRIDE:-$(command -v codex || true)}"
readonly UV_BIN="${UV_BIN_OVERRIDE:-$(command -v uv || true)}"
readonly PYTHON_BIN="${PYTHON_BIN_OVERRIDE:-$(command -v python3 || true)}"
readonly VENV_PYTHON="${DESK_ROOT}/.venv/bin/python"
readonly FLOCK_BIN="/usr/bin/flock"
readonly TIMEOUT_BIN="/usr/bin/timeout"
readonly DATE_BIN="/usr/bin/date"
readonly PROMPT_FILE="${DESK_ROOT}/ops/desk-cycle-prompt.md"
readonly LOG_DIR="${DESK_ROOT}/logs"
readonly LOCK_FILE="${LOG_DIR}/desk-cycle.lock"
readonly CLAIMS_FILE="${LOG_DIR}/desk-schedule-claims.json"
readonly SCHEDULE_GATE="${SCRIPT_DIR}/desk_schedule_gate.py"
readonly DATA_PREFLIGHT="${SCRIPT_DIR}/desk_data_preflight.py"
readonly PROXY_ENSURE="${SCRIPT_DIR}/ensure_binance_proxy.py"
readonly OUTCOME_ATTEST="${SCRIPT_DIR}/desk_cycle_outcome.py"
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
    test -n "${PYTHON_BIN}"
    test -x "${CODEX_BIN}"
    test -x "${UV_BIN}"
    test -x "${PYTHON_BIN}"
    test -x "${FLOCK_BIN}"
    test -x "${TIMEOUT_BIN}"
    test -s "${PROMPT_FILE}"
    test -s "${SCHEDULE_GATE}"
    test -s "${DATA_PREFLIGHT}"
    test -s "${PROXY_ENSURE}"
    test -s "${OUTCOME_ATTEST}"
    "${CODEX_BIN}" login status
    (
        cd "${DESK_ROOT}"
        "${UV_BIN}" lock --check
        case "${BINANCE_PROXY_EXTERNAL_MANAGER:-}" in
            "")
                "${UV_BIN}" run python scripts/ensure_binance_proxy.py
                ;;
            systemd)
                proxy_ready=false
                for _attempt in {1..30}; do
                    if "${UV_BIN}" run python scripts/ensure_binance_proxy.py --probe-only; then
                        proxy_ready=true
                        break
                    fi
                    sleep 1
                done
                if [[ "${proxy_ready}" != true ]]; then
                    echo "systemd-owned Binance proxy did not become ready" >&2
                    return 1
                fi
                ;;
            *)
                echo "unsupported BINANCE_PROXY_EXTERNAL_MANAGER" >&2
                return 1
                ;;
        esac
        "${UV_BIN}" run python scripts/desk_data_preflight.py
    )
    echo "READY model=${MODEL} effort=${EFFORT} design=weekly-top50/daily-weights schedule=00:07Z"
}

probe_preflight() {
    test -n "${CODEX_BIN}"
    test -n "${UV_BIN}"
    test -n "${PYTHON_BIN}"
    test -x "${CODEX_BIN}"
    test -x "${UV_BIN}"
    test -x "${PYTHON_BIN}"
    test -x "${VENV_PYTHON}"
    test -x "${FLOCK_BIN}"
    test -x "${TIMEOUT_BIN}"
    test -s "${PROMPT_FILE}"
    test -s "${SCHEDULE_GATE}"
    test -s "${DATA_PREFLIGHT}"
    test -s "${PROXY_ENSURE}"
    test -s "${OUTCOME_ATTEST}"
    test -s "${DESK_ROOT}/uv.lock"
    "${CODEX_BIN}" login status
    (
        cd "${DESK_ROOT}"
        export PYTHONDONTWRITEBYTECODE=1
        "${VENV_PYTHON}" scripts/ensure_binance_proxy.py --probe-only
    )
    echo "PROBE_OK read_only=true model=${MODEL} effort=${EFFORT} design=weekly-top50/daily-weights"
}

if [[ "${1:-}" == "--check" || "${1:-}" == "--probe-only" ]]; then
    probe_preflight
    exit 0
fi

readonly SCHEDULED="${1:-}"
if [[ "${SCHEDULED}" == "--scheduled" ]]; then
    :
elif [[ -n "${1:-}" ]]; then
    echo "usage: $0 [--check|--probe-only|--scheduled]" >&2
    exit 2
fi

schedule_gate() {
    local action="$1"
    local output status
    set +e
    output="$("${PYTHON_BIN}" "${SCHEDULE_GATE}" --kind full \
        --claims "${CLAIMS_FILE}" "${action}" 2>&1)"
    status=$?
    set -e
    if [[ "${status}" -eq 3 ]]; then
        return 1
    fi
    if [[ "${status}" -ne 0 ]]; then
        echo "$(timestamp) full-cycle schedule gate failed: ${output}" >&2
        exit "${status}"
    fi
    echo "${output}"
}

if [[ "${SCHEDULED}" == "--scheduled" ]] && ! schedule_gate --check; then
    exit 0
fi

mkdir -p "${LOG_DIR}" "${DESK_ROOT}/live_state" "${DESK_ROOT}/live_memory"
exec 9>"${LOCK_FILE}"
if ! "${FLOCK_BIN}" -n 9; then
    echo "$(timestamp) overlap guard: another v2 desk cycle owns ${LOCK_FILE}; stand down"
    exit 0
fi

if [[ "${SCHEDULED}" == "--scheduled" ]] && ! schedule_gate --check; then
    exit 0
fi

preflight
if [[ "${SCHEDULED}" == "--scheduled" ]] && ! schedule_gate --claim; then
    exit 0
fi
echo "$(timestamp) starting GPT desk cycle model=${MODEL} effort=${EFFORT}"
before_cycle="$("${VENV_PYTHON}" "${OUTCOME_ATTEST}" \
    --state-dir "${DESK_ROOT}/live_state" --snapshot)"
readonly before_cycle

set +e
(
    cd "${DESK_ROOT}"
    "${TIMEOUT_BIN}" --signal=TERM --kill-after=5m "${MAX_RUNTIME}" \
        "${CODEX_BIN}" \
        --enable multi_agent \
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
    set +e
    outcome="$("${VENV_PYTHON}" "${OUTCOME_ATTEST}" \
        --state-dir "${DESK_ROOT}/live_state" --log-dir "${LOG_DIR}" \
        --attest --before-cycle "${before_cycle}" 2>&1)"
    outcome_status=$?
    set -e
    if [[ "${outcome_status}" -eq 0 ]]; then
        echo "$(timestamp) GPT desk invocation attested: ${outcome}"
    else
        echo "$(timestamp) GPT desk exit=0 rejected by outcome attestation: ${outcome}" >&2
        status=1
    fi
fi
exit "${status}"
