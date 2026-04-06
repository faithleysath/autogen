#!/usr/bin/env bash
set -euo pipefail

ssh_source_dir="${SSH_SOURCE_DIR:-/run/host-ssh}"
ssh_target_dir="${SSH_TARGET_DIR:-/root/.ssh}"
global_known_hosts="/etc/ssh/ssh_known_hosts"

mkdir -p "${ssh_target_dir}"
chmod 700 "${ssh_target_dir}"

if [[ -d "${ssh_source_dir}" ]]; then
    shopt -s dotglob nullglob
    ssh_files=("${ssh_source_dir}"/*)
    shopt -u dotglob

    if (( ${#ssh_files[@]} > 0 )); then
        cp -R "${ssh_source_dir}/." "${ssh_target_dir}/"
        find "${ssh_target_dir}" -type d -exec chmod 700 {} \;
        find "${ssh_target_dir}" -type f -exec chmod 600 {} \;
    fi
fi

has_github_host_key() {
    local file_path="$1"
    [[ -f "${file_path}" ]] && grep -q "github.com" "${file_path}"
}

if ! has_github_host_key "${ssh_target_dir}/known_hosts" && ! has_github_host_key "${global_known_hosts}"; then
    github_keys="$(ssh-keyscan github.com 2>/dev/null || true)"

    if [[ -n "${github_keys}" ]]; then
        printf '%s\n' "${github_keys}" >> "${ssh_target_dir}/known_hosts"
        chmod 600 "${ssh_target_dir}/known_hosts"
    fi
fi

exec "$@"
