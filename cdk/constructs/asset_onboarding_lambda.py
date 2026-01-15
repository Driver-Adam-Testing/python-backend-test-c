from dataclasses import dataclass
from pathlib import Path

from aws_cdk import (
    Duration,
    aws_cloudwatch,
    aws_cloudwatch_actions,
    aws_ec2,
    aws_ecr_assets,
    aws_iam,
    aws_lambda,
    aws_lambda_event_sources,
    aws_s3,
    aws_s3_notifications,
    aws_secretsmanager,
    aws_sns,
    aws_ssm,
)
from constructs import Construct

from cdk.settings import settings

# Workspace root (where uv.lock and pyproject.toml live)
WORKSPACE_ROOT = Path(__file__).parent.parent.parent


@dataclass
class AssetOnboardingLambdaParams:
    environment: str
    api_url: str
    auth0_url: str
    auth0_audience: str
    dropzone_bucket: aws_s3.Bucket
    use_legacy_dropzone: bool
    vpc: aws_ec2.IVpc
    is_private_deploy: bool


class AssetOnboardingLambda(Construct):
    def __init__(
        self, scope: Construct, id: str, params: AssetOnboardingLambdaParams
    ) -> None:
        super().__init__(scope, id)

        deployment_secrets = aws_secretsmanager.Secret.from_secret_name_v2(
            self, "deployment_secrets", secret_name=settings.SECRECTS_NAME
        )

        lambda_function = aws_lambda.DockerImageFunction(
            scope,
            "AssetOnboardingLambdaPy",
            code=aws_lambda.DockerImageCode.from_image_asset(
                directory=str(WORKSPACE_ROOT),
                file="lambdas/onboarding_event_handler/Dockerfile.lambda",
                platform=aws_ecr_assets.Platform.LINUX_AMD64,
            ),
            architecture=aws_lambda.Architecture.X86_64,
            environment={
                "ENVIRONMENT": params.environment,
                "LOG_LEVEL": "INFO",
                "CLIENT_ID_SECRET": settings.ONBOARDING_LAMDBA_CLIENT_ID,
                "CLIENT_SECRET_SECRET": deployment_secrets.secret_name,
                "API_URL": params.api_url,
                "AUTH0_AUDIENCE": params.auth0_audience,
                "AUTH0_URL": params.auth0_url,
                "AWS_S3_CODE_BUCKET_SUFFIX": "codebase-dropzone",
                "USE_LEGACY_DROPZONE": str(params.use_legacy_dropzone),
                # Optional settings need fixed since they seem to get set
                # in all the envs and cause problems. Disabling for now.
                # "SENTRY_DSN": "FIXME"
                # if params.is_private_deploy
                # else settings.SENTRY_DSN,
                "IS_PRIVATE_DEPLOY": str(params.is_private_deploy),
            },
            timeout=Duration.seconds(15),
            vpc=params.vpc,
            vpc_subnets=aws_ec2.SubnetSelection(subnet_group_name="Private"),
        )
        deployment_secrets.grant_read(lambda_function)

        if params.is_private_deploy:
            firewall_cert_secret = aws_secretsmanager.Secret.from_secret_name_v2(
                self,
                "FirewallCertSecret",
                secret_name="/network-firewall/ca-certificate",
            )
            firewall_cert_secret.grant_read(lambda_function)

        params.dropzone_bucket.grant_read(lambda_function)

        sns_topic = aws_sns.Topic(scope, "CodeOnboardingTopic")
        lambda_function.add_event_source(
            aws_lambda_event_sources.SnsEventSource(sns_topic)
        )

        # TODO: We should find a way to scope down these privileges.
        # Because we need to create arbitrary buckets per org,
        # it's not clear how to do so without breaking existing
        # functionality.
        lambda_function.role.add_managed_policy(
            aws_iam.ManagedPolicy.from_aws_managed_policy_name("AmazonS3FullAccess")
        )
        params.dropzone_bucket.add_event_notification(
            aws_s3.EventType.OBJECT_TAGGING_PUT,
            aws_s3_notifications.SnsDestination(sns_topic),
            aws_s3.NotificationKeyFilter(prefix="assets/"),
        )
        params.dropzone_bucket.grant_read(lambda_function)

        # Lambda Error Rate Alarm
        alarm_topic_arn = aws_ssm.StringParameter.value_for_string_parameter(
            self, "/infrastructure/alarms/topic-arn"
        )
        alarm_topic = aws_sns.Topic.from_topic_arn(
            self, "InfrastructureAlarmsTopic", alarm_topic_arn
        )
        alarm_action = aws_cloudwatch_actions.SnsAction(alarm_topic)

        error_rate_alarm = aws_cloudwatch.Alarm(
            self,
            "AssetOnboardingLambdaErrorAlarm",
            alarm_description=f"[{params.environment}] Asset Onboarding Lambda errors > 5 in 5 minutes",
            metric=lambda_function.metric_errors(
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=5,
            evaluation_periods=1,
            comparison_operator=aws_cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=aws_cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        error_rate_alarm.add_alarm_action(alarm_action)
