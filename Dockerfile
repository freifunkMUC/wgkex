FROM gcr.io/cloud-marketplace-containers/google/bazel:3.5.0 AS builder

WORKDIR /wgkex

COPY . ./

RUN ["bazel", "build", "//wgkex/broker:app"]
RUN ["bazel", "build", "//wgkex/worker:app"]
RUN ["cp", "-rL", "bazel-bin", "bazel"]

FROM python:3.10.6-bullseye
WORKDIR /wgkex

COPY --from=builder /wgkex/bazel /wgkex/

COPY entrypoint /entrypoint

EXPOSE 5000

ENTRYPOINT ["/entrypoint"]
CMD ["broker"]
