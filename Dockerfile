FROM l.gcr.io/google/bazel:latest AS builder

WORKDIR /srv/wgkex

COPY . ./

RUN ["bazel", "build", "//wgkex/broker:app"]
RUN ["cp", "-rL", "bazel-bin", "bazel"]

FROM python:3.8
WORKDIR /srv/wgkex

COPY --from=builder /srv/wgkex/bazel /srv/wgkex/

EXPOSE 5000
CMD ["./wgkex/broker/app"]
