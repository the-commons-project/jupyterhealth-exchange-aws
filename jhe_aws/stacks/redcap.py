"""The REDCap Data Entry Trigger endpoint: one Lambda behind a function URL.

Deployed only when `redcap_integration` is set in the configuration (see build_stacks); without
it there is no such stack and nothing here exists.

    REDCap DET --POST--> function URL (?token=...) --> Lambda --> JHE (https, public)
                                                          └─────> REDCap API (https)

The function URL is public by nature (REDCap has to reach it from outside); what protects it is
the trigger token in the URL, compared in constant time against the secret's TRIGGER_TOKEN, and
that the function only ever acts on a REDCap record by reading it back through REDCap's own
API. The function runs outside the VPC: both services it calls are on the public internet, and
a VPC would only add a NAT hop.

Secret `jhe/<env>/redcap` (not a resource of this stack, like the service and mcp secrets:
created by `jhe-deploy secrets init`, filled by `jhe-deploy secrets set --secret redcap`):
JHE_API_KEY (the integration account's key), REDCAP_API_TOKEN, TRIGGER_TOKEN (generated).
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as sm
from cdk_nag import NagSuppressions
from constructs import Construct

from jhe_aws.config import EnvironmentConfig

from .data import DataStack
from .network import _retention

FUNCTION_DIR = Path(__file__).resolve().parents[2] / "functions" / "redcap_det"


class RedcapStack(cdk.Stack):
    def __init__(
        self, scope: Construct, construct_id: str, cfg: EnvironmentConfig, data: DataStack, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        redcap = cfg.redcap_integration
        assert redcap is not None, "RedcapStack is built only when redcap_integration is set"
        self.cfg = cfg
        secret_name = f"jhe/{cfg.name}/redcap"
        secret = sm.Secret.from_secret_name_v2(self, "RedcapSecret", secret_name)

        self.log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/jhe/{cfg.name}/redcap",
            retention=_retention(cfg.log_retention_days),
            encryption_key=None,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        role = iam.Role(
            self,
            "Role",
            role_name=f"jhe-{cfg.name}-redcap-fn",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=f"JHE {cfg.name} REDCap trigger: its own log group and its own secret, nothing else",
        )
        self.log_group.grant_write(role)
        secret.grant_read(role)
        if data.key is not None:
            data.key.grant_decrypt(role)

        self.function = lambda_.Function(
            self,
            "Function",
            function_name=f"jhe-{cfg.name}-redcap",
            description="REDCap data entry trigger: JHE patient and invitation link, written back",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(FUNCTION_DIR), exclude=["__pycache__", "*.pyc"]),
            role=role,
            memory_size=redcap.memory_mb,
            timeout=cdk.Duration.seconds(redcap.timeout_seconds),
            log_group=self.log_group,
            environment={
                "SECRET_ID": secret_name,
                "JHE_BASE_URL": f"https://{cfg.compute.domain_name}",
                "REDCAP_API_URL": redcap.redcap_api_url,
                "EMAIL_FIELD": redcap.email_field,
                "LINK_FIELD": redcap.link_field,
                "RECORD_ID_FIELD": redcap.record_id_field,
                "ORGANIZATION_ID": str(redcap.organization_id),
                "JHE_CLIENT_NAME": redcap.jhe_client_name,
            },
        )
        self.url = self.function.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)

        NagSuppressions.add_resource_suppressions(
            role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Secrets Manager appends a random six-character suffix to a secret's ARN, "
                    "so the grant names the secret as jhe/<env>/redcap-??????; the log group's "
                    "streams are created at run time under the group's ARN.",
                }
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            self.function,
            [
                {
                    "id": "AwsSolutions-L1",
                    "reason": "Python 3.13 on arm64, the newest runtime this CDK version pins; moved up "
                    "with the library.",
                }
            ],
        )

        cdk.CfnOutput(self, "RedcapTriggerUrl", value=self.url.url, description="Add ?token=<TRIGGER_TOKEN>")
        cdk.CfnOutput(self, "RedcapFunctionName", value=self.function.function_name)
        cdk.CfnOutput(self, "RedcapSecretName", value=secret_name)
