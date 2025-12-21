FROM mcr.microsoft.com/devcontainers/python:3

RUN git clone --single-branch --branch release https://github.com/netbox-community/netbox-docker.git /workspaces/netbox-docker && \
    ln -s /workspaces/wgkex/.devcontainer/netbox_docker-compose.override.yml /workspaces/netbox-docker/docker-compose.override.yml

RUN apt-get update && \
    apt-get install -yq --no-install-recommends \
    wireguard-go \
    wireguard-tools && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*


COPY .devcontainer/docker-entrypoint.sh /
RUN chmod +x /docker-entrypoint.sh

WORKDIR /workspaces/wgkex

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD [ "sleep", "infinity" ]
