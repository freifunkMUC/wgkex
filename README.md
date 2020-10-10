# WireGuard Key Exchange

WireGuard Key Exchange is a tool consisting of two parts: a frontend (broker) and a backend (worker). The frontend (broker) is where the client can push (register) its key before connecting. The backend (worker) is injecting those keys into a WireGuard instance.
This tool is intended to facilitate running BATMAN over VXLAN over WireGuard as a means to create encrypted high-performance mesh links.

## Installation

* TBA

## Configuration

* TBA

## Running the broker

* The broker web frontend can be started like this using Flask directly:
```
export FLASK_APP=wgkex.broker.app
# defaults to /etc/wgkex.yaml if not set
export WGKEX_CONFIG_FILE=/opt/wgkex/wgkex.yaml
flask run
```

## Client usage

```
$ wget -q  -O- --post-data='{"segment": "ffmuc_welt","public_key": "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="}'   --header='Content-Type:application/json'   'http://127.0.0.1:5000/api/v1/wg/key/exchange'
{
  "Message": "OK"
}
```
