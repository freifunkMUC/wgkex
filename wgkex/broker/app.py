#!/usr/bin/env python3
import re
import json
import time
import random

from voluptuous import Invalid, MultipleInvalid, Required, Schema
import bottle
import pika
import structlog

from wgkex.config import load_config

logger = structlog.get_logger()
config = load_config()

app.config["MQTT_BROKER_URL"] = config.get("mqtt", {}).get("broker_url")
app.config["MQTT_BROKER_PORT"] = config.get("mqtt", {}).get("broker_port")
app.config["MQTT_USERNAME"] = config.get("mqtt", {}).get("username")
app.config["MQTT_PASSWORD"] = config.get("mqtt", {}).get("password")
app.config["MQTT_KEEPALIVE"] = config.get("mqtt", {}).get("keepalive")
app.config["MQTT_TLS_ENABLED"] = config.get("mqtt", {}).get("tls")

WG_PUBKEY_PATTERN = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$")


def is_valid_wg_pubkey(pubkey):
    if WG_PUBKEY_PATTERN.match(pubkey) is None:
        raise Invalid("Not a valid Wireguard public key")
    return pubkey


def is_valid_domain(domain):
    if domain not in config.get("domains"):
        raise Invalid("Not a valid domain")
    return domain


WG_KEY_EXCHANGE_SCHEMA_V1 = Schema(
    {Required("public_key"): is_valid_wg_pubkey, Required("domain"): is_valid_domain}
)


class WorkerRegistry:
    WORKER_UPDATE_SCHEMA = Schema({"fqdn": str})

    def __init__(self):
        """
        Initialize the Worker registry

        Offers a queue, where workers can register,
        allocate clients to workers and maintains a
        list of active workers
        """
        self._workers = dict()

        self._connection = pika.BlockingConnection(
            pika.ConnectionParameters("localhost")
        )
        self._channel = self._connection.channel()
        self._channel.confirm_delivery()

        # queue to register workers and update metrics
        self._channel.queue_declare(queue="from_workers")
        self._channel.basic_consume(
            queue="from_workers", on_message_callback=self.update
        )

    def get_or_create(self, fqdn):
        """
        Get a worker by name if it exists, else create it
        """

        if fqdn in self._workers.keys():
            return self._workers.get(fqdn)

        worker = Worker(fqdn, self._channel)
        self._workers.update({fqdn: worker})
        logger.info("Registered new worker", fqdn=fqdn)

        return worker

    def get_best_worker(self):
        """
        Select best worker according to the latest metrics
        """

        self.prune()
        return random.choice(self._workers.values())

    def submit(self, body):
        try:
            worker = self.get_best_worker()
        except IndexError:
            logger.error("No workers to handle client request")
            return False

        logger.info("Selected worker for request", fqdn=worker.name)

        result = worker.push(body)

        if not result:
            logger.error("Worker did not accept job", fqdn=worker.name, msg=body)
            return False

        return True

    def update(self, channel, method, properties, body):
        """
        Handle updates from workers, register them if they don't exist, update metrics

        This is the on message callback for the from_workers queue
        """

        try:
            msg = WorkerRegistry.WORKER_UPDATE_SCHEMA(body)
        except MultipleInvalid:
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        worker = self.workers.get_or_create(msg["fqdn"], self._channel)
        worker.update(msg)
        logger.info("update from worker", fqdn=worker.name)

        channel.basic_ack(delivery_tag=method.delivery_tag)

    def prune(self):
        """
        Workers need to update their status regularly or they are dropped
        """

        now = time.time()
        timeout = 120

        for worker in self._workers.values():
            if now >= worker.last_update + timeout:
                logger.info("Worker timed out and pruned", fqdn=worker.name)
                worker.delete()
                self._workers.pop(worker.name)


class Worker:
    def __init__(self, name, channel):
        """
        Initialize a worker

        Creates a queue to allocate clients and holds worker metrics
        """

        self.name = name
        self.last_update = time.time()
        self._channel = channel

        channel.queue_declare(self.queue_name)

    @property
    def queue_name(self):
        return f"to_{self.name}"

    def push(self, msg):
        try:
            self._channel.basic_publish(
                exchange=self.queue_name, routing_key=self.name, body=msg
            )
            return True
        except pika.exceptions.UnroutableError:
            return False

    def update(self, msg):
        """
        Update worker metrics
        """

        self.last_update = time.time()

    def delete(self):
        """
        Clean up allocated resources, like the amqp queue
        """
        self._channel.queue_delete(self.queue_name)


class Broker(bottle.Bottle):
    def __init__(self):
        super(Broker, self).__init__()

        self.workers = WorkerRegistry()

        self.route("/", method="GET", callback=self.http_get_index_handler)
        self.route(
            "/api/v1/wg/key/exchange",
            method="POST",
            callback=self.http_post_v1_wg_key_exchange_handler,
        )

    def http_get_index_handler(self):
        return "<pre>This is the wgkex-broker HTTP endpoint.</pre>"

    def http_post_v1_wg_key_exchange_handler(self):
        try:
            data = WG_KEY_EXCHANGE_SCHEMA_V1(bottle.request.json)
        except MultipleInvalid as ex:
            raise bottle.HTTPResponse(
                body=json.dumps({"error": {"message": str(ex)}}),
                status=400,
                headers={"Content-Type": "application/json"},
            )

        pubkey, domain = data["public_key"], data["domain"]
        logger.info("New client request", pubkey=pubkey, domain=domain)

        result = self.workers.submit(data)
        print(result)

        return bottle.HTTPResponse(
            body=json.dumps({"Message": "OK"}),
            status=200,
            headers={"Content-Type": "application/json"},
        )


def main():
    broker = Broker()
    broker.run()


if __name__ == "__main__":
    main()

