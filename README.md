[![Coverage Status](https://coveralls.io/repos/github/freifunkMUC/wgkex/badge.svg?branch=main)](https://coveralls.io/github/freifunkMUC/wgkex?branch=main)
[![pylint](https://github.com/freifunkMUC/wgkex/actions/workflows/pylint.yml/badge.svg)](https://github.com/freifunkMUC/wgkex/actions/workflows/pylint.yml)
[![Lint](https://github.com/freifunkMUC/wgkex/actions/workflows/black.yml/badge.svg)](https://github.com/freifunkMUC/wgkex/actions/workflows/black.yml)
[![Bazel tests](https://github.com/freifunkMUC/wgkex/actions/workflows/bazel.yml/badge.svg)](https://github.com/freifunkMUC/wgkex/actions/workflows/bazel.yml)

- [WireGuard Key Exchange](#wireguard-key-exchange)
  - [Overview](#overview)
    - [Frontend broker](#frontend-broker)
      - [POST /api/v1/wg/key/exchange](#post-apiv1wgkeyexchange)
    - [Backend worker](#backend-worker)
  - [Installation](#installation)
  - [Configuration](#configuration)
  - [Running the broker and worker](#running-the-broker-and-worker)
    - [Build using Bazel](#build-using-bazel)
    - [Run using Python](#run-using-python)
  - [Client usage](#client-usage)
    - [Worker](#worker)
  - [Contact](#contact)


# WireGuard Key Exchange

wgkex is a WireGuard key exchange and management tool designed and run by FFMUC.

## Overview

WireGuard Key Exchange is a tool consisting of two parts: a frontend (broker) and a backend (worker). These components
communicate to each other via MQTT - a messaging bus.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="Docs/architecture-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="Docs/architecture.png">
  <img src="Docs/architecture.png" alt="Architectural Diagram">
</picture>

### Frontend broker

The frontend broker is where the client can push (register) its key before connecting. These keys are then pushed into
an MQTT bus for all workers to consume.

The frontend broker exposes the following API endpoints for use:

```
/api/v1/wg/key/exchange
```

The listen address and port for the Flask server can be configured in `wgkex.yaml` under the `broker_listen` key:

```yaml
broker_listen:
  # host defaults to 127.0.0.1 if unspecified
  host: 0.0.0.0
  # port defaults to 5000 if unspecified
  port: 5000
```

#### POST /api/v1/wg/key/exchange

JSON POST'd to this endpoint should be in this format:

```json
{
  "domain": "CONFIGURED_DOMAIN",
  "public_key": "PUBLIC_KEY"
}
```

The broker will validate the domain and public key, and if valid, will push the key onto the MQTT bus.

### Backend worker

The backend (worker) waits for new keys to appear on the MQTT message bus. Once a new key appears, the worker performs
validation task on the key, then injects those keys into a WireGuard instance(While also updating the VxLAN FDB).
It reports metrics like number of connected peers and instance data like local address, WG listening port and
external domain name (configured in config.yml) back to the broker.
Each worker must run on a machine with a unique hostname, as it is used for separation of metrics.

This tool is intended to facilitate running BATMAN over VXLAN over WireGuard as a means to create encrypted
high-performance mesh links.

For further information, please see this [presentation on the architecture](https://www.slideshare.net/AnnikaWickert/ffmuc-goes-wild-infrastructure-recap-2020-rc3)

## Installation

* TBA

## Configuration

* Configuration file

The `wgkex` configuration file defaults to `/etc/wgkex.yaml` ([Sample configuration file](wgkex.yaml.example)), however
can also be overwritten by setting the environment variable `WGKEX_CONFIG_FILE`.

## Running the broker and worker

### Build using [Bazel](https://bazel.build)

Worker:

```sh
# defaults to /etc/wgkex.yaml if not set
export WGKEX_CONFIG_FILE=/opt/wgkex/wgkex.yaml
bazel build //wgkex/worker:app
# Artifact will now be placed into ./bazel-bin/wgkex/worker/app
./bazel-bin/wgkex/worker/app
```

Broker:

```sh
# defaults to /etc/wgkex.yaml if not set
export WGKEX_CONFIG_FILE=/opt/wgkex/wgkex.yaml
bazel build //wgkex/broker:app
# Artifact will now be placed into ./bazel-bin/wgkex/broker/app
./bazel-bin/wgkex/broker/app
```

### Run using Python

Broker:
(Using Flask development server)

```sh
FLASK_ENV=development FLASK_DEBUG=1 FLASK_APP=wgkex/broker/app.py python3 -m flask run
```

Worker:

```sh
python3 -c 'from wgkex.worker.app import main; main()'
```

## Client usage

The client can be used via CLI:
```
$ wget -q  -O- --post-data='{"domain": "ffmuc_welt","public_key": "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="}'   --header='Content-Type:application/json'   'http://127.0.0.1:5000/api/v1/wg/key/exchange'
{
  "Message": "OK"
}
```

Or via python:
```python
import requests
key_data = {"domain": "ffmuc_welt","public_key": "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="}
broker_url = "http://127.0.0.1:5000"
push_key = requests.get(f'{broker_url}/api/v1/wg/key/exchange', json=key_data)
print(f'Key push was: {push_key.json().get("Message")]}')
```

### Worker

You can set up dummy interfaces for the worker using this script:

```sh
interface_linklocal() {
  # We generate a predictable v6 address
  local macaddr="$(echo $1 | wg pubkey |md5sum|sed 's/^\(..\)\(..\)\(..\)\(..\)\(..\).*$/02:\1:\2:\3:\4:\5/')"
  local oldIFS="$IFS"; IFS=':'; set -- $macaddr; IFS="$oldIFS"
  echo "fe80::$1$2:$3ff:fe$4:$5$6"
}

sudo ip link add wg-welt type wireguard
wg genkey | sudo wg set wg-welt private-key /dev/stdin
sudo wg set wg-welt listen-port 51820
addr=$(interface_linklocal $(sudo wg show wg-welt private-key))
sudo ip addr add $addr dev wg-welt
sudo ip link add vx-welt type vxlan id 99 dstport 0 local $addr dev wg-welt
sudo ip addr add fe80::1/64 dev vx-welt
sudo ip link set wg-welt up
sudo ip link set vx-welt up
```


## Contact

[Freifunk Munich Mattermost](https://chat.ffmuc.net)
