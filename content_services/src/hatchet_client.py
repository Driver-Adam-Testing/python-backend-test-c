import logging

from hatchet_sdk import ClientConfig, Hatchet

hatchet = Hatchet(
    config=ClientConfig(logger=logging.getLogger()),
)
