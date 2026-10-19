"""Checks that Docs/openapi.yaml is a structurally valid OpenAPI document and matches the broker.

The real responses of the broker are validated against the schemas in the spec. The validator only supports the
subset of OpenAPI schema keywords used in the spec, and fails on any other keyword, so it can't silently pass.
"""

import ipaddress
import json
import os
import re
import unittest
from unittest import mock

import yaml

from wgkex.broker.app_test import _DOMAIN, _PUBKEY, BrokerTestCase
from wgkex.common.mqtt import CONNECTED_PEERS_METRIC
from wgkex.config import config

_SPEC_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "Docs", "openapi.yaml"
)
_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_SCHEMA_KEYWORDS = {
    "type",
    "required",
    "properties",
    "items",
    "enum",
    "pattern",
    "minItems",
    "maxItems",
    "description",
    "example",
}
_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
}


def _parker_config() -> config.Config:
    return config.Config.from_dict(
        {
            "parker": {
                "enabled": True,
                "464xlat": True,
                "ipam": "json",
                "prefixes": {
                    "ipv4": {"clat_subnet": "10.80.96.0/22"},
                    "ipv6": {"length": 63},
                },
            },
            "broker_signing_key": "signing-key",
            "domains": [_DOMAIN],
            "domain_prefixes": ["ffmuc_"],
            "workers": {"gw1": {"id": 1}},
            "mqtt": {"broker_url": "", "username": "", "password": ""},
        }
    )


def load_spec() -> dict:
    with open(_SPEC_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class SpecValidationError(Exception):
    pass


class Validator:
    def __init__(self, spec: dict) -> None:
        self.spec = spec

    def resolve(self, obj: dict) -> dict:
        while isinstance(obj, dict) and "$ref" in obj:
            ref = obj["$ref"]
            if not ref.startswith("#/"):
                raise SpecValidationError(f"Only local references are supported: {ref}")
            target = self.spec
            for part in ref[2:].split("/"):
                if not isinstance(target, dict) or part not in target:
                    raise SpecValidationError(f"Unresolvable reference: {ref}")
                target = target[part]
            obj = target
        return obj

    def validate(self, value, schema: dict, path: str = "$") -> None:
        schema = self.resolve(schema)
        unknown = set(schema) - _SCHEMA_KEYWORDS
        if unknown:
            raise SpecValidationError(
                f"Unsupported schema keywords {unknown} at {path}"
            )
        if "type" in schema:
            expected = _TYPES[schema["type"]]
            if not isinstance(value, expected) or (
                isinstance(value, bool) and schema["type"] != "boolean"
            ):
                raise SpecValidationError(
                    f"{path}: expected {schema['type']}, got {value!r}"
                )
        if "enum" in schema and value not in schema["enum"]:
            raise SpecValidationError(f"{path}: {value!r} not in {schema['enum']}")
        pattern = schema.get("pattern")
        if pattern and isinstance(value, str) and not re.search(pattern, value):
            raise SpecValidationError(f"{path}: {value!r} doesn't match {pattern}")
        if isinstance(value, dict):
            for name in schema.get("required", []):
                if name not in value:
                    raise SpecValidationError(f"{path}: missing required {name}")
            for name, sub in schema.get("properties", {}).items():
                if name in value:
                    self.validate(value[name], sub, f"{path}.{name}")
        if isinstance(value, list):
            if len(value) < schema.get("minItems", 0):
                raise SpecValidationError(f"{path}: too few items")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                raise SpecValidationError(f"{path}: too many items")
            if "items" in schema:
                for i, item in enumerate(value):
                    self.validate(item, schema["items"], f"{path}[{i}]")


class OpenAPISpecTest(unittest.TestCase):
    """Structural checks of the OpenAPI document."""

    def setUp(self) -> None:
        self.spec = load_spec()
        self.validator = Validator(self.spec)

    def test_header(self):
        self.assertRegex(self.spec["openapi"], r"^3\.0\.\d+$")
        self.assertTrue(self.spec["info"]["title"])
        self.assertIsInstance(self.spec["info"]["version"], str)
        self.assertIsInstance(self.spec["paths"], dict)

    def test_all_references_resolve(self):
        def walk(node, path):
            if isinstance(node, dict):
                if "$ref" in node:
                    self.validator.resolve(node)
                for key, child in node.items():
                    walk(child, f"{path}/{key}")
            elif isinstance(node, list):
                for i, child in enumerate(node):
                    walk(child, f"{path}/{i}")

        walk(self.spec, "#")

    def test_operations(self):
        operation_ids = set()
        for path, item in self.spec["paths"].items():
            self.assertTrue(path.startswith("/"), path)
            for method, op in item.items():
                self.assertIn(method, _HTTP_METHODS, f"{path}: {method}")
                self.assertNotIn(op["operationId"], operation_ids)
                operation_ids.add(op["operationId"])
                self.assertTrue(op["responses"], f"{method} {path}")
                for code, response in op["responses"].items():
                    self.assertRegex(code, r"^[1-5]\d\d$", f"{method} {path}")
                    self.assertIn("description", self.validator.resolve(response))
                if "requestBody" in op:
                    self.assertIn("content", self.validator.resolve(op["requestBody"]))

    def test_schemas_and_examples(self):
        """All schemas only use supported keywords and their examples are valid."""

        def check(schema, path):
            schema = self.validator.resolve(schema)
            if "example" in schema:
                self.validator.validate(schema["example"], schema, path)
            for name, sub in schema.get("properties", {}).items():
                check(sub, f"{path}.{name}")
            if "items" in schema:
                check(schema["items"], f"{path}[]")

        for name, schema in self.spec["components"]["schemas"].items():
            check(schema, name)

        def media_types(node):
            if isinstance(node, dict):
                if "schema" in node:
                    yield node
                for child in node.values():
                    yield from media_types(child)

        for media in media_types(self.spec["paths"]):
            media = self.validator.resolve(media)
            examples = [e["value"] for e in media.get("examples", {}).values()]
            if "example" in media:
                examples.append(media["example"])
            for example in examples:
                self.validator.validate(example, media["schema"])


class OpenAPIResponseTest(BrokerTestCase):
    """Validates real broker responses against the spec."""

    def setUp(self) -> None:
        super().setUp()
        self.spec = load_spec()
        self.validator = Validator(self.spec)

    def assertMatchesSpec(self, path: str, method: str, resp) -> None:
        responses = self.spec["paths"][path][method]["responses"]
        code = str(resp.status_code)
        self.assertIn(code, responses, f"{method} {path} returned undocumented {code}")
        response = self.validator.resolve(responses[code])
        content = response.get("content")
        if content is None:
            return
        mimetype = resp.mimetype
        self.assertIn(mimetype, content, f"{method} {path} {code}")
        schema = content[mimetype]["schema"]
        body = (
            resp.get_json() if mimetype == "application/json" else resp.get_data(True)
        )
        self.validator.validate(body, schema)

    def exchange(self, version: str, body):
        path = f"/api/{version}/wg/key/exchange"
        resp = self.post(version, body)
        self.assertMatchesSpec(path, "post", resp)
        return resp

    def test_v1(self):
        resp = self.exchange("v1", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.assertEqual(resp.status_code, 200)

    def test_v2(self):
        self.add_worker("gw1", 10)
        body = {"domain": _DOMAIN, "public_key": _PUBKEY, "location": "muc"}
        resp = self.exchange("v2", body)
        self.assertEqual(resp.status_code, 200)

    def test_rate_limited(self):
        self.add_worker("gw1", 10)
        self.app.rate_limiter = self.app.RateLimiter(1, 60, 0, 60)
        for version in ("v1", "v2", "v2"):
            resp = self.exchange(version, {"domain": _DOMAIN, "public_key": _PUBKEY})
            self.assertEqual(resp.status_code, 200)

    def test_invalid_requests(self):
        bodies = [
            {"domain": "unknown", "public_key": _PUBKEY},
            {"domain": _DOMAIN, "public_key": "invalid"},
            {"domain": _DOMAIN},
            ["not", "an", "object"],
        ]
        for version in ("v1", "v2"):
            for body in bodies:
                resp = self.exchange(version, body)
                self.assertEqual(resp.status_code, 400, body)
            resp = self.client.post(f"/api/{version}/wg/key/exchange", data="{")
            self.assertEqual(resp.status_code, 400)
            self.assertMatchesSpec(f"/api/{version}/wg/key/exchange", "post", resp)

    def test_blocked(self):
        self.add_worker("gw1", 10)
        self.app.blocklist = self.app.KeyList("blocklist", keys=[_PUBKEY])
        for version in ("v1", "v2"):
            resp = self.exchange(version, {"domain": _DOMAIN, "public_key": _PUBKEY})
            self.assertEqual(resp.status_code, 403)

    def test_v2_no_gateway(self):
        resp = self.exchange("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.assertEqual(resp.status_code, 400)

    def test_v2_no_gateway_data(self):
        self.add_worker("gw1", 10)
        with mock.patch.object(
            self.app.worker_metrics,
            "get_best_worker",
            return_value=("gw2", 0.0, 0),
        ):
            resp = self.exchange("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.assertEqual(resp.status_code, 500)

    def test_metrics(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertMatchesSpec("/metrics", "get", resp)
        with mock.patch.object(config.get_config().metrics, "broker_endpoint", False):
            resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 404)
        self.assertMatchesSpec("/metrics", "get", resp)

    def test_index(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertMatchesSpec("/", "get", resp)

    def parker(self, path: str, query: dict, cfg: config.Config | None = None):
        cfg = cfg or _parker_config()
        ipam = mock.MagicMock()
        ipam.get_or_allocate_prefix.return_value = (
            None,
            ipaddress.IPv6Network("2001:db8:42::/63"),
            [],
        )
        with (
            mock.patch.object(self.app.config, "get_config", return_value=cfg),
            mock.patch.object(self.app, "ipam", ipam),
            mock.patch.object(self.app, "sign_response", return_value=b"c2lnbg=="),
        ):
            resp = self.client.get(path, query_string=query)
        self.assertMatchesSpec(path, "get", resp)
        return resp

    def test_v3(self):
        self.app.parker_worker_metrics.data.clear()
        self.app.parker_worker_metrics._pending.clear()
        self.app.parker_worker_data.clear()
        cfg = _parker_config()
        query = {"pubkey": _PUBKEY, "nonce": "n", "v6mtu": "1400"}
        try:
            for path in ("/api/v3/wg/key/exchange", "/api/v3/config", "/config"):
                resp = self.parker(path, query, cfg)
                self.assertEqual(resp.status_code, 400)

            self.app.parker_worker_metrics.update(
                "gw1", "parker", CONNECTED_PEERS_METRIC, 0
            )
            self.app.parker_worker_metrics.set_online("gw1")
            self.app.parker_worker_data["gw1"] = {
                "ExternalAddress": "2001:db8::1",
                "Port": 40000,
                "PublicKey": "WORKERPUBKEY=",
                "LinkAddress": "fe80::1",
            }
            for path in ("/api/v3/wg/key/exchange", "/api/v3/config", "/config"):
                resp = self.parker(path, query, cfg)
                self.assertEqual(resp.status_code, 200)
                line, _ = resp.get_data(True).split("\n")
                body = json.loads(line)
                self.validator.validate(
                    body, {"$ref": "#/components/schemas/ParkerConfig"}
                )
                self.assertEqual(
                    body["concentrators"][0]["endpoint"], "[2001:db8::1]:40000"
                )

            self.assertEqual(
                self.parker("/config", {"pubkey": "invalid"}, cfg).status_code, 400
            )
            self.app.blocklist = self.app.KeyList("blocklist", keys=[_PUBKEY])
            self.assertEqual(self.parker("/config", query, cfg).status_code, 403)
            self.app.blocklist = self.app.KeyList("blocklist")
            self.app.rate_limiter = self.app.RateLimiter(1, 60, 0, 60)
            self.app.rate_limiter.allow("127.0.0.1")
            self.assertEqual(self.parker("/config", query, cfg).status_code, 429)
        finally:
            self.app.parker_worker_metrics.data.clear()
            self.app.parker_worker_metrics._pending.clear()
            self.app.parker_worker_data.clear()

    def test_v3_disabled(self):
        resp = self.client.get("/config", query_string={"pubkey": _PUBKEY})
        self.assertEqual(resp.status_code, 400)
        self.assertMatchesSpec("/config", "get", resp)

    def test_paths_match_flask_routes(self):
        """Every route of the broker is documented, with the same methods, and vice versa."""
        routes = {}
        for rule in self.app.app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            routes[rule.rule] = {m.lower() for m in rule.methods} - {"head", "options"}
        documented = {path: set(item) for path, item in self.spec["paths"].items()}
        self.assertDictEqual(routes, documented)

    def test_validator_rejects_wrong_shapes(self):
        """Sanity check that the validator catches the changes clients would break on."""
        endpoint = {
            "Address": "gw1",
            "Port": "51820",
            "PublicKey": "x",
            "AllowedIPs": ["fe80::1"],
        }
        schema = {"$ref": "#/components/schemas/V2Response"}
        self.validator.validate({"Endpoint": endpoint}, schema)
        for broken in (
            {**endpoint, "Port": 51820},
            {**endpoint, "AllowedIPs": "fe80::1"},
            {k: v for k, v in endpoint.items() if k != "PublicKey"},
        ):
            with self.assertRaises(SpecValidationError):
                self.validator.validate({"Endpoint": broken}, schema)
        with self.assertRaises(SpecValidationError):
            self.validator.validate(
                {"Message": "NOK"}, {"$ref": "#/components/schemas/V1Response"}
            )


if __name__ == "__main__":
    unittest.main()
