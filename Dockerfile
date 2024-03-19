FROM python:3.11.8-bookworm AS builder

RUN apt-get update && apt-get install -y apt-transport-https curl gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wgkex

COPY BUILD WORKSPACE requirements.txt ./
COPY wgkex ./wgkex

RUN wget https://github.com/bazelbuild/bazelisk/releases/download/v1.19.0/bazelisk-linux-amd64 && chmod +x bazelisk-linux-amd64
ENV BAZELISK_CLEAN=true
ENV USE_BAZEL_VERSION=7.1.1rc2

RUN ["./bazelisk-linux-amd64", "build", "//wgkex/broker:app"]
RUN ["./bazelisk-linux-amd64", "build", "//wgkex/worker:app"]

FROM python:3.11.8-slim-bookworm
WORKDIR /wgkex

COPY --from=builder /wgkex/bazel-bin /wgkex/

COPY entrypoint /entrypoint

EXPOSE 5000

ENTRYPOINT ["/entrypoint"]
CMD ["broker"]
