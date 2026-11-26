#!/bin/sh
set -eu

image="${1:?image}"
client_image="${2:-python:3.12-slim-bookworm}"
sockets=$(docker volume create)
keys=
service=
cleanup() {
    if [ -n "$service" ]; then docker rm -f "$service" >/dev/null 2>&1 || true; fi
    if [ -n "$keys" ]; then docker volume rm "$keys" >/dev/null 2>&1 || true; fi
    docker volume rm "$sockets" >/dev/null 2>&1 || true
}
trap cleanup EXIT
keys=$(docker volume create)
service=$(docker run --detach --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --user 0:0 \
    -v /etc/machine-id:/etc/machine-id:ro \
    -v "$sockets:/run/oubi-tpm" -v "$keys:/var/lib/oubi-tpm" "$image")

for attempt in 1 2 3 4 5; do
    if docker exec "$service" test -S /run/oubi-tpm/service.sock; then break; fi
    sleep 1
done
if ! docker exec "$service" test -S /run/oubi-tpm/service.sock; then
    docker logs "$service" >&2
    exit 1
fi

docker run --rm --network none --user 1000:1000 --entrypoint python \
    -v "$sockets:/run/oubi-tpm:ro" -i "$client_image" - <<'PY'
import json
import socket

with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(5)
    connection.connect("/run/oubi-tpm/service.sock")
    connection.sendall(b'{"operation":"ping"}')
    connection.shutdown(socket.SHUT_WR)
    assert json.load(connection.makefile("rb")) == {"error": "unavailable"}
PY

docker run --rm --network none --user 1001:1001 --entrypoint python \
    -v "$sockets:/run/oubi-tpm:ro" -i "$client_image" - <<'PY'
import socket

with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(5)
    connection.connect("/run/oubi-tpm/service.sock")
    try:
        connection.sendall(b'{"operation":"ping"}')
        connection.shutdown(socket.SHUT_WR)
        assert connection.recv(512) == b""
    except (BrokenPipeError, ConnectionResetError):
        pass
PY
