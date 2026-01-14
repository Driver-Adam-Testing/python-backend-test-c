from aws_cdk import (
    Duration,
    Stack,
    aws_ec2,
    aws_ecr,
    aws_ecs,
    aws_events,
    aws_iam,
    aws_logs,
    aws_route53,
    aws_s3,
    aws_secretsmanager,
    aws_ssm,
)
from constructs import Construct

from cdk.settings import settings


class HatchetWorkerParams:
    def __init__(
        self,
        environment: str,
        aws_region: str,
        aws_account: str,
        metrics_bus: aws_events.EventBus,
        is_private_deploy: bool,
        dropzone_bucket: aws_s3.Bucket,
        cpu_size: int,
        mem_size: int,
        min_instance: int,
        workflow_set_name: str,
        stop_timeout_seconds: int = 120,
    ) -> None:
        self.environment = environment
        self.aws_region = aws_region
        self.aws_account = aws_account
        self.metrics_bus = metrics_bus
        self.is_private_deploy = is_private_deploy
        self.dropzone_bucket = dropzone_bucket
        self.cpu_size = cpu_size
        self.mem_size = mem_size
        self.min_instance = min_instance
        self.workflow_set_name = workflow_set_name
        self.stop_timeout_seconds = stop_timeout_seconds


class HatchetWorker(Construct):
    def __init__(self, scope: Construct, id: str, params: HatchetWorkerParams) -> None:
        super().__init__(scope, id)

        # --- Baseline lookups / shared infra ---
        vpc_id = aws_ssm.StringParameter.value_from_lookup(
            scope, parameter_name="/baseline/infra/v2/vpc/id"
        )
        vpc = aws_ec2.Vpc.from_lookup(self, id="BaselineVPC", vpc_id=vpc_id)

        cluster_name = aws_ssm.StringParameter.value_from_lookup(
            scope, parameter_name="/baseline/infra/v2/ecs/cluster/name"
        )
        cluster = aws_ecs.Cluster.from_cluster_attributes(
            self, id="BaselineCluster", cluster_name=cluster_name, vpc=vpc
        )

        hosted_zone_id = aws_ssm.StringParameter.value_from_lookup(
            scope, parameter_name="/baseline/infra/v2/route53/hostedZoneId"
        )
        hosted_zone_name = aws_ssm.StringParameter.value_from_lookup(
            scope, parameter_name="/baseline/infra/v2/route53/hostedZoneName"
        )
        hosted_zone = aws_route53.HostedZone.from_hosted_zone_attributes(
            self,
            id="BaselineHostedZone",
            zone_name=hosted_zone_name,
            hosted_zone_id=hosted_zone_id,
        )

        openai_url = None
        if params.is_private_deploy:
            openai_url = aws_ssm.StringParameter.value_from_lookup(
                scope, parameter_name="/baseline/infra/v2/azure/openai/url"
            )

        inspector_bucket_name = aws_ssm.StringParameter.value_from_lookup(
            scope, parameter_name="/baseline/infra/v2/inspector/stateBucketName"
        )

        base_env = {
            "PROJECT_NAME": "DriverAI Hatchet Worker",
            "ENVIRONMENT": params.environment,
            "AWS_REGION": params.aws_region,
            "HATCHET_CLIENT_HOST_PORT": f"hatchet.private.{hosted_zone.zone_name}:7077",
            "INSPECTOR_BUCKET_NAME": inspector_bucket_name,
            "DROPZONE_BUCKET_NAME": params.dropzone_bucket.bucket_name,
            "HATCHET_CLIENT_GRPC_MAX_RECV_MESSAGE_LENGTH": "100000000",
            "HATCHET_CLIENT_GRPC_MAX_SEND_MESSAGE_LENGTH": "100000000",
            "WORKFLOW_SET_NAME": params.workflow_set_name,
        }

        if params.is_private_deploy:
            base_env["IS_PRIVATE_DEPLOY"] = "true"

        if openai_url is not None:
            base_env["AZURE_OPENAI_BASE_URL"] = openai_url

        deployment_secrets = aws_secretsmanager.Secret.from_secret_name_v2(
            self, "deployment_secrets", secret_name=settings.SECRECTS_NAME
        )

        hatchet_token_secrect = aws_secretsmanager.Secret.from_secret_name_v2(
            self, "hatchet_secret", secret_name="hatchet/appliance/credentials"
        )

        secret_fields = [key.strip() for key in settings.SECRECTS_KEYS.split(",")]

        secrets_map = {
            k: aws_ecs.Secret.from_secrets_manager(deployment_secrets, field=k)
            for k in secret_fields
        }
        secrets_map["HATCHET_CLIENT_TOKEN"] = aws_ecs.Secret.from_secrets_manager(
            hatchet_token_secrect
        )

        base_env.update(settings.to_dict())

        worker_task_def = aws_ecs.FargateTaskDefinition(
            self,
            "HatchetWorkerTaskDef",
            cpu=params.cpu_size,
            memory_limit_mib=params.mem_size,
            runtime_platform=aws_ecs.RuntimePlatform(
                cpu_architecture=aws_ecs.CpuArchitecture.X86_64
            ),
        )

        _worker_container = worker_task_def.add_container(
            "HatchetWorkerContainer",
            image=aws_ecs.ContainerImage.from_ecr_repository(
                aws_ecr.Repository.from_repository_name(
                    self, "HatchetWorkerRepo", "hatchet-worker"
                ),
                tag="latest",
            ),
            environment=base_env,
            secrets=secrets_map,
            logging=aws_ecs.LogDrivers.aws_logs(
                stream_prefix="python-worker",
                log_retention=aws_logs.RetentionDays.ONE_YEAR,
            ),
            stop_timeout=Duration.seconds(params.stop_timeout_seconds),
        )
        # Optional: a port for metrics/debugging
        # worker_container.add_port_mappings(aws_ecs.PortMapping(container_port=9000))

        # Inline policy example: customer-scoped Secrets Manager access (match main service)
        worker_task_def.task_role.attach_inline_policy(
            aws_iam.Policy(
                self,
                "CustomerSecretsRWWorker",
                document=aws_iam.PolicyDocument(
                    statements=[
                        aws_iam.PolicyStatement(
                            effect=aws_iam.Effect.ALLOW,
                            actions=[
                                "secretsmanager:CreateSecret",
                                "secretsmanager:ListSecrets",
                                "secretsmanager:DescribeSecret",
                            ],
                            resources=[
                                f"arn:aws:secretsmanager:{Stack.of(self).region}:{Stack.of(self).account}:secret:DRIVER_AI_CUSTOMER/*"
                            ],
                        )
                    ]
                ),
            )
        )

        # --- Fargate Service (Internal only, no ALB) ---
        self.worker_service = aws_ecs.FargateService(
            self,
            "HatchetWorkerSvc",
            cluster=cluster,
            task_definition=worker_task_def,
            desired_count=params.min_instance,
            assign_public_ip=False,
            vpc_subnets=aws_ec2.SubnetSelection(subnet_group_name="Private"),
            circuit_breaker=aws_ecs.DeploymentCircuitBreaker(
                enable=True, rollback=True
            ),
            min_healthy_percent=100,
            max_healthy_percent=200,
        )

        # --- CPU-based autoscaling ---
        scalable = self.worker_service.auto_scale_task_count(
            min_capacity=params.min_instance,  # keep at least 2 running
            max_capacity=10,  # adjust as needed
        )
        scalable.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=50,  # aim to keep avg CPU around 50%
            scale_in_cooldown=Duration.seconds(300),
            scale_out_cooldown=Duration.seconds(60),
        )
        scalable.scale_on_memory_utilization(
            "MemoryScaling",
            target_utilization_percent=70,  # scale when memory hits ~70%
            scale_in_cooldown=Duration.seconds(300),
            scale_out_cooldown=Duration.seconds(60),
        )

        # service discovery: uncomment for dns-based discovery for this worker
        # self.worker_service.enable_cloud_map(name="worker")

        # Allow the worker to emit metrics/events
        params.metrics_bus.grant_all_put_events(worker_task_def.task_role)

        # Grant full S3 admin access. TODO: Scope this down?
        worker_task_def.task_role.add_managed_policy(
            aws_iam.ManagedPolicy.from_aws_managed_policy_name("AmazonS3FullAccess")
        )
        worker_task_def.task_role.add_managed_policy(
            aws_iam.ManagedPolicy.from_aws_managed_policy_name(
                "SecretsManagerReadWrite"
            )
        )
        # Add explicit perms for dropzone bucket in the event we scope down S3 full access
        params.dropzone_bucket.grant_read_write(worker_task_def.task_role)

        if settings.IS_PRIVATE_DEPLOY == "true":
            firewall_cert_secret = aws_secretsmanager.Secret.from_secret_name_v2(
                self,
                "FirewallCertSecret",
                secret_name="/network-firewall/ca-certificate",
            )
            firewall_cert_secret.grant_read(
                self.worker_service.task_definition.task_role
            )
