#!/usr/bin/env bash

set -Eeuo pipefail

readonly repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly service_name="${1:-izpit-checker-web.service}"

echo "Pulling the latest code in ${repo_dir}..."
git -C "${repo_dir}" pull --ff-only

echo "Restarting ${service_name}..."
sudo systemctl restart "${service_name}"
sudo systemctl --no-pager --full status "${service_name}"
