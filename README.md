# WireGuard Tools

WireGuard Tools consists of both a frontend and a backend to dynamically supply WireGuard keys to a FreiFunk Gateway offering BATMAN over VXLAN over WireGuard.

## Installation

* TBA

## Configuration

* TBA

## Client usage

```
$ wget -q  -O- --post-data='{"segment": "ffmuc_welt","public_key": "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="}'   --header='Content-Type:application/json'   'http://127.0.0.1:5000/api/v1/wg/key/exchange'
{
  "Message": "OK"
}
```
