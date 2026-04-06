#!/usr/bin/env bash
set -euo pipefail

agent_user="${AGENT_USER:-autogen}"
host_uid="${HOST_UID:-}"
host_gid="${HOST_GID:-}"
ssh_source_dir="${SSH_SOURCE_DIR:-/run/host-ssh}"
ssh_target_dir="${SSH_TARGET_DIR:-/home/${agent_user}/.ssh}"
global_known_hosts="/etc/ssh/ssh_known_hosts"

ensure_agent_user() {
    local group_name="${agent_user}"
    local useradd_args

    if [[ -n "${host_gid}" ]]; then
        group_name="$(getent group "${host_gid}" | cut -d: -f1 || true)"
        if [[ -z "${group_name}" ]]; then
            group_name="${agent_user}"
            groupadd --gid "${host_gid}" "${group_name}"
        fi
    elif ! getent group "${group_name}" >/dev/null; then
        groupadd "${group_name}"
    fi

    if id -u "${agent_user}" >/dev/null 2>&1; then
        if [[ -n "${host_uid}" ]]; then
            usermod --uid "${host_uid}" "${agent_user}"
        fi
        usermod --gid "${group_name}" "${agent_user}"
    else
        useradd_args=(--create-home --shell /bin/bash)
        if [[ -n "${host_uid}" ]]; then
            useradd_args+=(--uid "${host_uid}")
        fi
        useradd_args+=(--gid "${group_name}" "${agent_user}")
        useradd "${useradd_args[@]}"
    fi
}

ensure_agent_user

mkdir -p "${ssh_target_dir}"
chown -R "${agent_user}:$(id -gn "${agent_user}")" "/home/${agent_user}"
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

chown -R "${agent_user}:$(id -gn "${agent_user}")" "${ssh_target_dir}"

has_github_host_key() {
    local file_path="$1"
    [[ -f "${file_path}" ]] && grep -q "github.com" "${file_path}"
}

if ! has_github_host_key "${ssh_target_dir}/known_hosts" && ! has_github_host_key "${global_known_hosts}"; then
    github_keys="$(ssh-keyscan github.com 2>/dev/null || true)"

    if [[ -n "${github_keys}" ]]; then
        printf '%s\n' "${github_keys}" >> "${ssh_target_dir}/known_hosts"
        chmod 600 "${ssh_target_dir}/known_hosts"
        chown "${agent_user}:$(id -gn "${agent_user}")" "${ssh_target_dir}/known_hosts"
    fi
fi

exec gosu "${agent_user}" "$@"
