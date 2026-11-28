# funambridge (Go). The Python version 0.x lives in legacy/python/.
FROM --platform=$BUILDPLATFORM golang:1.27-alpine AS build
ARG TARGETOS
ARG TARGETARCH
ARG VERSION=dev
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
RUN V="${VERSION#v}"; CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath \
      -ldflags "-s -w -X github.com/atallo/funambridge/internal/version.Version=${V}" \
      -o /out/funambridge ./cmd/funambridge

# distroless/static: CA certificates and time zones, no shell (~2 MB + binary)
FROM gcr.io/distroless/static-debian12
COPY --from=build /out/funambridge /funambridge
# config.yaml, the log and the TLS certs live in a volume mounted on /data
VOLUME ["/data"]
EXPOSE 9000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/funambridge", "--config", "/data/config.yaml", "health"]
ENTRYPOINT ["/funambridge", "--config", "/data/config.yaml"]
CMD ["serve"]
