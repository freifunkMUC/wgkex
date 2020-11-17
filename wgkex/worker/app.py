#!/usr/bin/env python3
import pika
import platform
import threading
import time
import json

import structlog

from wgkex.config import load_config

config = load_config()
logger = structlog.get_logger()


class Worker:
    def __init__(self):
        self.name = platform.node()

        self._connection = pika.SelectConnection(pika.ConnectionParameters("localhost"), on_open_callback=self.on_open)

        try:
            self._connection.ioloop.start()
            logger.info("started")
        except KeyboardInterrupt:
            connection.ioloop.stop()
        connection.close()

    def on_open(self, connection):
        self._channel = self._connection.channel()
        self._channel.confirm_delivery()

        # queue to register workers and update metrics
        self._channel.queue_declare(queue="from_workers")
        self._channel.queue_declare(queue="to_workers")

        self._channel.basic_consume(
            queue=self.queue_name,
            on_message_callback=self.execute
        )

        #threading.Thread(target=self.update).start()

    @property
    def queue_name(self):
        return f"to_{self.name}"


    def update(self):
        time.sleep(2)
        while True:
            logger.info("update")
            self._channel.basic_publish(
                exchange="from_workers",
                routing_key="update",
                body=json.dumps({"fqdn": self.name})
            )
            time.sleep(30)

    def execute(self, channel, method, properties, body):
        print(body)
        channel.basic_ack(method.delivery_tag)


def main():
    Worker()


if __name__ == '__main__':
    main()
