# wgkex Devcontainer

## Features & Usage

- Python 3, Bazel, Ruff linter pre-installed
- Docker-in-Docker (DinD) enabled (Moby disabled due to no support on Debian Trixie)
- `/root/.cache/pip` and `/var/lib/docker` are volumes, for caching between restarts
- NetBox instance auto-starts on port 8000 (forwarded)
- MQTT service started automatically
- `wg-welt` and `wg-nodes` interfaces created on startup (TODO: generate LL IPv6 address based on pubkey)

## NetBox (for Parker)

Go to http://localhost:8000, for credentials see `netbox_docker-compose.override.yml`.

Create an IPv6 prefix, add the id, tag or role to the `netbox_filter` in `wgkex.yaml`.

## Known issues

- Needs to be tested on "non-rootless" host setups (default Docker installation), there might be file permission issues when creating files within the container as root.
  Specifying a `containerUser` and/or `remoteUser` did not work with rootless Podman on the host, the workspace files where still owned by `root` inside the container.
