from pathlib import Path

from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_cloudwatch,
    aws_cloudwatch_actions,
    aws_ec2,
    aws_ecr_assets,
    aws_lambda,
    aws_logs,
    aws_secretsmanager,
    aws_sns,
    aws_ssm,
)
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_events_targets as targets,
)
from constructs import Construct
from cdk.settings import settings

# Workspace root (where uv.lock and pyproject.toml live)
WORKSPACE_ROOT = Path(__file__).parent.parent.parent


class Auth0EventLambda(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        environment: str,
        cloudwatch_alarm_arn: str | None = None,
        is_private_deploy: bool = False,
    ) -> None:
        self.is_private_deploy = is_private_deploy
        super().__init__(scope, id)

        auth0_eventbridge_bus_name = aws_ssm.StringParameter.value_from_lookup(
            scope, parameter_name="/baseline/infra/v2/eventbridge/auth0_bus_name"
        )
        event_bus = events.EventBus.from_event_bus_name(
            self,
            "Auth0EventBus",
            event_bus_name=auth0_eventbridge_bus_name,
        )
        vpc_id = aws_ssm.StringParameter.value_from_lookup(
            scope, parameter_name="/baseline/infra/v2/vpc/id"
        )

        vpc = aws_ec2.Vpc.from_lookup(self, id="BaselineVPC_Auth0Events", vpc_id=vpc_id)

        deployment_secrets = aws_secretsmanager.Secret.from_secret_name_v2(
            self, "deployment_secrets", secret_name=settings.SECRECTS_NAME
        )

        hatchet_token_secret = aws_secretsmanager.Secret.from_secret_name_v2(
            self, "hatchet_secret", secret_name="hatchet/appliance/credentials"
        )

        hosted_zone_name = aws_ssm.StringParameter.value_from_lookup(
            scope, parameter_name="/baseline/infra/v2/route53/hostedZoneName"
        )

        lambda_concurrent_executions = None
        if settings.IS_PRODUCTION_ACCOUNT == "true":
            lambda_concurrent_executions = 10

        # Create Lambda function
        self.lambda_function = aws_lambda.DockerImageFunction(
            scope,
            "Auth0EventProcessorLambda",
            code=aws_lambda.DockerImageCode.from_image_asset(
                directory=str(WORKSPACE_ROOT),
                file="lambdas/auth0_event_processor/Dockerfile.lambda",
                platform=aws_ecr_assets.Platform.LINUX_AMD64,
            ),
            architecture=aws_lambda.Architecture.X86_64,
            vpc=vpc,
            vpc_subnets=aws_ec2.SubnetSelection(
                subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            environment={
                "LOG_LEVEL": "INFO",
                "MODAL_TOKEN_ID": "FIXME",
                "MODAL_ENVIRONMENT": "FIXME",
                "MODAL_SECRET_NAME": deployment_secrets.secret_name,
                "IS_PRIVATE_DEPLOY": "true" if self.is_private_deploy else "false",
                "HATCHET_CLIENT_HOST_PORT": f"hatchet.private.{hosted_zone_name}:7077",
                "HATCHET_CLIENT_TOKEN_SECRET_NAME": hatchet_token_secret.secret_name,
                "HATCHET_CLIENT_TLS_STRATEGY": "none",
            },
            reserved_concurrent_executions=lambda_concurrent_executions,
            timeout=Duration.seconds(60),
        )

        # Grant Lambda permissions to read secrets
        deployment_secrets.grant_read(self.lambda_function)
        hatchet_token_secret.grant_read(self.lambda_function)

        # Grant permission to read firewall certificate for private deployments
        if self.is_private_deploy:
            firewall_cert_secret = aws_secretsmanager.Secret.from_secret_name_v2(
                self, "FirewallCertSecret", secret_name="/network-firewall/ca-certificate"
            )
            firewall_cert_secret.grant_read(self.lambda_function)

        # Configure CloudWatch Logs with retention
        aws_logs.LogGroup(
            self,
            "Auth0EventProcessorLogGroup",
            log_group_name=f"/aws/lambda/{self.lambda_function.function_name}",
            retention=aws_logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Create EventBridge rule with event pattern
        rule = events.Rule(
            self,
            "Auth0EventRule",
            event_bus=event_bus,
            rule_name="auth0-security-events-rule",
            description="Route specific Auth0 events to Lambda",
            event_pattern=events.EventPattern(
                detail={
                    "data": {
                        "type": [
                            "ss",
                            "s",
                            "sdu",
                            "sapi",
                            "organization_member_added",
                            "organization_member_removed",
                        ]
                    }
                }
            ),
        )

        # Add Lambda as target with DLQ configuration
        rule.add_target(
            targets.LambdaFunction(
                self.lambda_function,
            )
        )

        self.lambda_error_rate_alarm = aws_cloudwatch.Alarm(
            self,
            "Auth0EventsLambdaErrorAlarm",
            alarm_description=f"[{environment}] Auth0 Events Lambda Errors > 5 over last 5 minutes",
            metric=self.lambda_function.metric_errors(),
            threshold=5,
            evaluation_periods=1,
            comparison_operator=aws_cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=aws_cloudwatch.TreatMissingData.IGNORE,
        )

        self.lambda_throttle_alarm = aws_cloudwatch.Alarm(
            self,
            "Auth0EventsLambdaThrottleAlarm",
            alarm_description=f"[{environment}] Auth0 Events Lambda Throttled",
            metric=self.lambda_function.metric_throttles(),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=aws_cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=aws_cloudwatch.TreatMissingData.IGNORE,
        )

        if cloudwatch_alarm_arn:
            notification_action = aws_cloudwatch_actions.SnsAction(
                aws_sns.Topic.from_topic_arn(
                    id="Auth0EventsNotifySupportTopic",
                    topic_arn=cloudwatch_alarm_arn,
                    scope=self,
                )
            )
            self.lambda_error_rate_alarm.add_alarm_action(notification_action)
            self.lambda_throttle_alarm.add_alarm_action(notification_action)
        else:
            print(
                f"*** NO CW ALARM CONFIGURED FOR Auth0EventLambda in {environment} ***"
            )
