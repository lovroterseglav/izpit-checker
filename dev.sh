#!/usr/bin/env bash

set -Eeuo pipefail

readonly repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    python_bin="${VIRTUAL_ENV}/bin/python"
elif [[ -x "${repo_dir}/.venv/bin/python" ]]; then
    python_bin="${repo_dir}/.venv/bin/python"
elif [[ -x "${repo_dir}/venv/bin/python" ]]; then
    python_bin="${repo_dir}/venv/bin/python"
else
    echo "Python virtual environment not found." >&2
    echo "Create one with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
readonly python_bin

export IZPIT_ALERT_CONFIG="${IZPIT_ALERT_CONFIG:-${repo_dir}/config.json}"
export IZPIT_ALERT_WEB_SECRET="${IZPIT_ALERT_WEB_SECRET:-development-only-secret}"
readonly dev_host="${DEV_HOST:-127.0.0.1}"
readonly dev_port="${PORT:-8000}"

if [[ ! -f "${IZPIT_ALERT_CONFIG}" ]]; then
    echo "Config file not found: ${IZPIT_ALERT_CONFIG}" >&2
    echo "Create it with: cp config.json.template config.json" >&2
    exit 1
fi

cd "${repo_dir}"
exec "${python_bin}" -m flask --app web:create_app run --debug --host "${dev_host}" --port "${dev_port}"
