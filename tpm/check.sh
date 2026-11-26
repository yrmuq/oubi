#!/bin/sh
set -eu

state=$(mktemp -d)
emulator=
cleanup() {
    if [ -n "$emulator" ]; then
        kill "$emulator" 2>/dev/null || true
        wait "$emulator" 2>/dev/null || true
    fi
    rm -r "$state"
}
trap cleanup EXIT
swtpm socket --tpm2 --tpmstate "dir=$state" \
    --server type=tcp,port=18871,bindaddr=127.0.0.1 \
    --ctrl type=tcp,port=18872,bindaddr=127.0.0.1 --flags startup-clear &
emulator=$!
sleep 1
kill -0 "$emulator"
OUBI_TEST_TPM=127.0.0.1:18871 CGO_ENABLED=0 go test -count=1 ./...
