- [WireGuard Key Exchange](#wireguard-key-exchange)
  * [Overview](#overview)
    + [Frontend broker](#frontend-broker)
      - [POST /api/v1/wg/key/exchange](#post--api-v1-wg-key-exchange)
    + [Backend worker](#backend-worker)
  * [Installation](#installation)
  * [Configuration](#configuration)
  * [Running the broker](#running-the-broker)
  * [Client usage](#client-usage)
  * [Contact](#contact)


# WireGuard Key Exchange

wgkex is a WireGuard key exchange and management tool designed and run by FFMUC.

## Overview

WireGuard Key Exchange is a tool consisting of two parts: a frontend (broker) and a backend (worker). These components 
communicate to each other via MQTT - a messaging bus.

![](Docs/architecture.png)

### Frontend broker

The frontend broker is where the client can push (register) its key before connecting. These keys are then pushed into
an MQTT bus for all workers to consume.

The frontend broker exposes the following API endpoints for use:

```
/api/v1/wg/key/exchange
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

This tool is intended to facilitate running BATMAN over VXLAN over WireGuard as a means to create encrypted 
high-performance mesh links.

For further information, please see this [presentation on the architecture](https://www.slideshare.net/AnnikaWickert/ffmuc-goes-wild-infrastructure-recap-2020-rc3)

## Installation

* TBA

## Configuration

* Configuration file

The `wgkex` configuration file defaults to `/etc/wgkex.yaml` ([Sample configuration file](wgkex.yaml.example)), however
can also be overwritten by setting the environment variable `WGKEX_CONFIG_FILE`.

## Running the broker

* The broker web frontend can be started directly from a Git checkout:

```
# defaults to /etc/wgkex.yaml if not set
export WGKEX_CONFIG_FILE=/opt/wgkex/wgkex.yaml
poetry run wgkex-broker
```

* The broker can also be built and run via [bazel](https://bazel.build):

```shell
bazel build //wgkex/broker:app
# Artifact will now be placed into ./bazel-bin/wgkex/broker/app
./bazel-bin/wgkex/broker/app
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

## Contact

[wgkex - IRCNet](ircs://irc.ircnet.net:6697/wgkex)
