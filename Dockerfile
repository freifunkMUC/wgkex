FROM python:3.11.8-bullseye AS builder

RUN apt-get update && apt-get install -y apt-transport-https curl gnupg \
    && curl -fsSL https://bazel.build/bazel-release.pub.gpg | gpg --dearmor >/usr/share/keyrings/bazel-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/bazel-archive-keyring.gpg] https://storage.googleapis.com/bazel-apt stable jdk1.8" | tee /etc/apt/sources.list.d/bazel.list \
    && apt-get update && apt-get install -y bazel \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wgkex

COPY BUILD WORKSPACE requirements.txt ./
COPY wgkex ./wgkex

RUN ["bazel", "build", "//wgkex/broker:app"]
RUN ["bazel", "build", "//wgkex/worker:app"]
RUN ["cp", "-rL", "bazel-bin", "bazel"]

FROM python:3.11.8-bullseye
WORKDIR /wgkex

COPY --from=builder /wgkex/bazel /wgkex/

COPY entrypoint /entrypoint

EXPOSE 5000

ENTRYPOINT ["/entrypoint"]
CMD ["broker"]
