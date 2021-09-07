FROM l.gcr.io/google/bazel:latest AS builder

WORKDIR /wgkex

COPY . ./

RUN ["bazel", "build", "//wgkex/broker:app"]
RUN ["bazel", "build", "//wgkex/worker:app"]
RUN ["cp", "-rL", "bazel-bin", "bazel"]

FROM python:3
WORKDIR /wgkex

COPY --from=builder /wgkex/bazel /wgkex/

COPY wgkex.yaml.example /etc/wgkex.yaml
RUN sed -i "s/broker_url:.*/broker_url: mqtt/g; s/username:.*/username:/g; s/password:.*/password:/g" /etc/wgkex.yaml

EXPOSE 5000
CMD ["./wgkex/broker/app"]
