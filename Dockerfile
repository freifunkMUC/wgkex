FROM gcr.io/bazel-public/bazel:8.1.1 AS builder
# Make sure .bazelversion and the version of image are identical

WORKDIR /wgkex

COPY .bazelrc BUILD MODULE.bazel MODULE.bazel.lock requirements_lock.txt ./
COPY wgkex ./wgkex

RUN ["bazel", "build", "//wgkex/broker:app"]
RUN ["bazel", "build", "//wgkex/worker:app"]
RUN ["cp", "-rL", "bazel-bin", "bazel"]


FROM python:3.13.2-slim-bookworm
WORKDIR /wgkex

COPY --from=builder /wgkex/bazel /wgkex/

COPY entrypoint /entrypoint

EXPOSE 5000

ENTRYPOINT ["/entrypoint"]
CMD ["broker"]
