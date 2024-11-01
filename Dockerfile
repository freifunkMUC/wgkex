FROM gcr.io/bazel-public/bazel:7.2.1 AS builder

WORKDIR /wgkex

COPY BUILD WORKSPACE requirements.txt ./
COPY wgkex ./wgkex

RUN ["bazel", "build", "//wgkex/broker:app"]
RUN ["bazel", "build", "//wgkex/worker:app"]
RUN ["cp", "-rL", "bazel-bin", "bazel"]


FROM python:3.13.0-slim-bookworm
WORKDIR /wgkex

COPY --from=builder /wgkex/bazel /wgkex/

COPY entrypoint /entrypoint

EXPOSE 5000

ENTRYPOINT ["/entrypoint"]
CMD ["broker"]
