# Local development

How to run broker and worker on a single Linux machine for testing. Build, test and lint commands are in the
[README](../README.md#development).

## 1. Create dummy interfaces

The worker needs a WireGuard interface `wg-{domain}` and a VXLAN interface `vx-{domain}` per domain. For the domain
`ffmuc_welt`:

```sh
interface_linklocal() {
  # derive a predictable IPv6 link-local address from the private key
  local macaddr="$(echo $1 | wg pubkey | md5sum | sed 's/^\(..\)\(..\)\(..\)\(..\)\(..\).*$/02:\1:\2:\3:\4:\5/')"
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

## 2. Create a configuration

Keep only the `ffmuc_welt` domain and the `ffmuc_` prefix from the example:

```sh
sed -E '/(ffmuc_muc|ffwert|ffdon)/d' wgkex.yaml.example > wgkex.yaml
export WGKEX_CONFIG_FILE=$PWD/wgkex.yaml
```

> [!TIP]
> The example points `mqtt.broker_url` to a public test broker. For local tests, run Mosquitto, e.g.
> `docker run --network host eclipse-mosquitto` (its default configuration only accepts anonymous clients on
> localhost), and set `mqtt.broker_url: localhost`.

## 3. Run broker and worker

With Bazel (after `bazel build //wgkex/broker:app //wgkex/worker:app`):

```sh
./bazel-bin/wgkex/broker/app
# second terminal; the worker needs root (or CAP_NET_ADMIN) to manage the interfaces
sudo -E ./bazel-bin/wgkex/worker/app
```

Or with Python, from the repository root with the dependencies from `requirements_lock.txt` installed:

```sh
python3 -m wgkex.broker.app
sudo -E "$(command -v python3)" -m wgkex.worker.app
```

> [!NOTE]
> For quick API experiments, `flask --app wgkex/broker/app.py run --debug` starts the Flask development server on
> `127.0.0.1:5000`. It ignores `broker_listen` and does not start the rebalancing loop.

## 4. Exchange a key

```sh
curl -s -X POST http://127.0.0.1:5000/api/v2/wg/key/exchange \
  -d '{"domain": "ffmuc_welt", "public_key": "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="}'
```

Gluon nodes use `wget`, which works the same way:

```sh
wget -q -O- --post-data='{"domain": "ffmuc_welt", "public_key": "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="}' \
  http://127.0.0.1:5000/api/v2/wg/key/exchange
```

Or from Python:

```python
import requests

key_data = {"domain": "ffmuc_welt", "public_key": "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="}
resp = requests.post("http://127.0.0.1:5000/api/v2/wg/key/exchange", json=key_data, timeout=10)
resp.raise_for_status()
print(resp.json()["Endpoint"])
```

The response contains the chosen gateway, see [`openapi.yaml`](openapi.yaml):

```json
{
  "Endpoint": {
    "Address": "gw04.ext.ffmuc.net",
    "AllowedIPs": ["fe80::27c:16ff:fec0:6c74"],
    "Port": "40011",
    "PublicKey": "TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g="
  }
}
```
