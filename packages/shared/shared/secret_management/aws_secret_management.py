import json
import logging

import boto3
from botocore.exceptions import ClientError
from shared.interfaces.aws_client_config import AWSClientConfig

logger = logging.getLogger(__name__)


class AWSSecretManagementStrategy:
    def __init__(self, config: AWSClientConfig) -> None:
        self.config = config
        session = boto3.session.Session()
        self.client = session.client(
            service_name="secretsmanager", region_name=self.config.region_name
        )

    def write_secret(self, secret_name: str, secret_value: str) -> None:
        value = self.read_secret(secret_name)
        if value:
            response = self.client.update_secret(
                SecretId=secret_name, SecretString=secret_value
            )
        else:
            response = self.client.create_secret(
                Name=secret_name, SecretString=secret_value
            )

        if not response:
            logger.error(f"Failed to write secret {secret_name}.")
            raise Exception(f"Failed to write secret {secret_name}.")

    def read_secret(self, secret_name: str) -> dict | None:
        try:
            response = self.client.get_secret_value(SecretId=secret_name)
            if not response:
                logger.error(f"Secret {secret_name} does not exist.")
                return None
            secret_str = response["SecretString"]
            if isinstance(secret_str, str):
                secret_value = json.loads(secret_str)
            else:
                secret_value = secret_str

            return secret_value
        except ClientError:
            logger.exception(f"Secret {secret_name} does not exist.")
            return None

    def delete_secret(self, secret_name: str) -> None:
        try:
            self.client.delete_secret(SecretId=secret_name, RecoveryWindowInDays=7)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                logger.info(f"Secret {secret_name} not found, skipping deletion.")
                return
            raise


def format_secret_name(prefix: str, suffix: str) -> str:
    return f"{prefix}/{suffix}"
