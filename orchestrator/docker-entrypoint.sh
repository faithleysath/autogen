#!/usr/bin/env bash
set -euo pipefail

app_user="${APP_USER:-appuser}"
docker_socket="${DOCKER_SOCKET:-/var/run/docker.sock}"

if [[ -S "${docker_socket}" ]]; then
    socket_gid="$(stat -c '%g' "${docker_socket}")"
    socket_group="$(getent group "${socket_gid}" | cut -d: -f1 || true)"

    if [[ -z "${socket_group}" ]]; then
        socket_group="docker-host"
        groupadd --gid "${socket_gid}" "${socket_group}"
    fi

    usermod --append --groups "${socket_group}" "${app_user}"
fi

exec gosu "${app_user}" "$@"
