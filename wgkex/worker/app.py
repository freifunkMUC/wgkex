#!/usr/bin/env python3


from wgkex.config import load_config
from wgkex.worker.mqtt import connect as mqtt

config = load_config()


def main():

    mqtt(config.get("domains"))


if __name__ == "__main__":
    main()
