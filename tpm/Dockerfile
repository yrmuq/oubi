# syntax=docker/dockerfile:1.7
FROM alpine:3.24.4@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf AS runtime
FROM golang:1.27.4-alpine3.24@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS build

WORKDIR /src
RUN apk add --no-cache swtpm
COPY tpm/go.mod tpm/go.sum ./
RUN go mod download
COPY tpm/*.go tpm/check.sh ./
RUN sh check.sh \
    && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/oubi-tpm . \
    && cp "$(go list -m -f '{{.Dir}}' github.com/google/go-tpm)/LICENSE" /out/LICENSE.go-tpm \
    && cp "$(go list -m -f '{{.Dir}}' golang.org/x/sys)/LICENSE" /out/LICENSE.x-sys \
    && cp "$(go env GOROOT)/LICENSE" /out/LICENSE.go

FROM runtime
ENV GOMAXPROCS=1
RUN --mount=type=bind,source=dependencies/os-refresh.txt,target=/tmp/os-refresh.txt apk upgrade --no-cache \
    && install -d -m 0755 /run/oubi-tpm && install -d -m 0700 /var/lib/oubi-tpm
COPY --from=build /out/oubi-tpm /usr/local/bin/oubi-tpm
COPY --from=build /out/LICENSE.* /usr/share/licenses/oubi-tpm/
COPY LICENSE /usr/share/licenses/oubi-tpm/LICENSE
ENTRYPOINT ["/usr/local/bin/oubi-tpm"]
