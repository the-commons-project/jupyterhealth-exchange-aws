"""Every stack synthesizes from the sample configuration, with the cdk-nag AWS Solutions checks
raising no error, and the templates say what the design says."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from cdk_nag import AwsSolutionsChecks

from jhe_aws.images import Images
from jhe_aws.stacks import Stacks, build_stacks

from .conftest import sample_config

WEB = "sha256:" + "a" * 64
MCP = "sha256:" + "b" * 64


def synth(cfg) -> tuple[cdk.App, Stacks]:
    app = cdk.App()
    stacks = build_stacks(app, cfg, Images(web=WEB, mcp=MCP))
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
    app.synth()
    return app, stacks


@pytest.fixture(scope="module")
def default() -> tuple[cdk.App, Stacks]:
    return synth(sample_config())


def nag_errors(stack: cdk.Stack) -> list[str]:
    found = assertions.Annotations.from_stack(stack).find_error(
        "*", assertions.Match.string_like_regexp("AwsSolutions-.*")
    )
    return [str(a.entry.data) for a in found]


def test_no_nag_errors(default):
    _, stacks = default
    for stack in (
        stacks.access,
        stacks.network,
        stacks.shared,
        stacks.data,
        stacks.ops,
        stacks.compute,
        stacks.monitoring,
    ):
        assert stack is not None
        assert nag_errors(stack) == [], stack.stack_name


def test_stack_names_and_protection(default):
    _, stacks = default
    assert stacks.network.stack_name == "jhe-test-network"
    assert stacks.compute.stack_name == "jhe-test-compute"
    assert stacks.data.termination_protection is True
    assert stacks.compute.termination_protection is False


def images_of(stack: cdk.Stack) -> dict[str, str]:
    """family -> the image reference as a string (the URI is an Fn::Join of the repository token
    and the digest)."""
    template = assertions.Template.from_stack(stack)
    out = {}
    for resource in template.find_resources("AWS::ECS::TaskDefinition").values():
        container = resource["Properties"]["ContainerDefinitions"][0]
        out[resource["Properties"]["Family"]] = json.dumps(container["Image"])
        assert resource["Properties"]["RuntimePlatform"] == {
            "CpuArchitecture": "ARM64",
            "OperatingSystemFamily": "LINUX",
        }
    return out


def test_tasks_run_the_digests(default):
    _, stacks = default
    ops, compute = images_of(stacks.ops), images_of(stacks.compute)
    assert f"@{WEB}" in ops["jhe-test-ops"]
    assert f"@{WEB}" in compute["jhe-test-web"]
    assert f"@{MCP}" in compute["jhe-test-mcp"]
    assert f"@{WEB}" not in compute["jhe-test-mcp"]


def test_secrets_are_container_secrets_not_environment(default):
    _, stacks = default
    template = assertions.Template.from_stack(stacks.compute)
    task = next(
        r
        for r in template.find_resources("AWS::ECS::TaskDefinition").values()
        if r["Properties"]["Family"] == "jhe-test-web"
    )
    container = task["Properties"]["ContainerDefinitions"][0]
    env = {e["Name"] for e in container["Environment"]}
    secrets = {s["Name"] for s in container["Secrets"]}
    assert {"SITE_URL", "ALLOWED_HOSTS", "DB_HOST", "JHE_TLS", "SECURE_HSTS_SECONDS"} <= env
    assert {
        "DB_USER",
        "DB_PASSWORD",
        "SECRET_KEY",
        "OIDC_RSA_PRIVATE_KEY",
        "PATIENT_ACCESS_ENCRYPTION_KEYS",
    } <= secrets
    assert not (env & secrets)
    assert "DJANGO_SUPERUSER_PASSWORD" not in secrets, "only the ops task reads the bootstrap password"
    site = next(e["Value"] for e in container["Environment"] if e["Name"] == "SITE_URL")
    assert site == "https://jhe.example.org"
    hosts = next(e["Value"] for e in container["Environment"] if e["Name"] == "ALLOWED_HOSTS")
    assert hosts == "jhe.example.org,mcp.jhe.example.org"


def test_ops_task_migrates_by_default_and_holds_the_operator_privileges(default):
    _, stacks = default
    ops = assertions.Template.from_stack(stacks.ops)
    ops.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "Family": "jhe-test-ops",
            "ContainerDefinitions": [
                assertions.Match.object_like(
                    {
                        "Command": ["python", "manage.py", "migrate", "--no-input"],
                        "Secrets": assertions.Match.array_with(
                            [assertions.Match.object_like({"Name": "DJANGO_SUPERUSER_PASSWORD"})]
                        ),
                    }
                )
            ],
        },
    )
    assert "s3:PutObject" in json.dumps(ops.find_resources("AWS::IAM::Policy")), (
        "the operator's task has the bucket"
    )
    compute = assertions.Template.from_stack(stacks.compute)
    families = {
        r["Properties"]["Family"] for r in compute.find_resources("AWS::ECS::TaskDefinition").values()
    }
    assert families == {"jhe-test-web", "jhe-test-mcp", "jhe-test-cron"}


def test_tls_to_tasks_by_default(default):
    _, stacks = default
    template = assertions.Template.from_stack(stacks.compute)
    groups = template.find_resources("AWS::ElasticLoadBalancingV2::TargetGroup")
    assert len(groups) == 2
    for group in groups.values():
        assert group["Properties"]["Protocol"] == "HTTPS"
        assert group["Properties"]["HealthCheckProtocol"] == "HTTPS"
        assert group["Properties"]["HealthCheckPath"] == "/health"
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {"Port": 443, "Protocol": "HTTPS", "SslPolicy": "ELBSecurityPolicy-TLS13-1-2-2021-06"},
    )
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener", {"Port": 80, "Protocol": "HTTP"}
    )


def test_only_our_hosts_reach_a_container(default):
    _, stacks = default
    template = assertions.Template.from_stack(stacks.compute)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
            "Port": 443,
            "DefaultActions": [
                {
                    "Type": "fixed-response",
                    "FixedResponseConfig": assertions.Match.object_like({"StatusCode": "404"}),
                }
            ],
        },
    )
    rules = template.find_resources("AWS::ElasticLoadBalancingV2::ListenerRule")
    hosts = {
        tuple(c["HostHeaderConfig"]["Values"])
        for r in rules.values()
        for c in r["Properties"]["Conditions"]
        if c["Field"] == "host-header"
    }
    assert hosts == {("jhe.example.org",), ("mcp.jhe.example.org",)}


def test_plain_http_to_tasks_when_switched_off():
    _, stacks = synth(sample_config(compute={"tls_to_tasks": {"enabled": False}}))
    template = assertions.Template.from_stack(stacks.compute)
    for group in template.find_resources("AWS::ElasticLoadBalancingV2::TargetGroup").values():
        assert group["Properties"]["Protocol"] == "HTTP"
    task = next(
        r
        for r in template.find_resources("AWS::ECS::TaskDefinition").values()
        if r["Properties"]["Family"] == "jhe-test-web"
    )
    assert "JHE_TLS" not in {e["Name"] for e in task["Properties"]["ContainerDefinitions"][0]["Environment"]}


def test_database_is_isolated_encrypted_and_tls_only(default):
    _, stacks = default
    template = assertions.Template.from_stack(stacks.data)
    template.has_resource_properties(
        "AWS::RDS::DBInstance",
        {
            "DBInstanceIdentifier": "jhe-test-db",
            "Engine": "postgres",
            "StorageEncrypted": True,
            "PubliclyAccessible": False,
            "DeletionProtection": True,
            "BackupRetentionPeriod": 7,
        },
    )
    template.has_resource_properties("AWS::RDS::DBParameterGroup", {"Parameters": {"rds.force_ssl": "1"}})
    template.has_resource("AWS::RDS::DBInstance", {"DeletionPolicy": "Snapshot"})
    template.has_resource("AWS::KMS::Key", {"DeletionPolicy": "Retain"})
    for name in ("jhe/test/bootstrap-admin", "jhe/test/database"):
        template.has_resource_properties("AWS::SecretsManager::Secret", {"Name": name})


def test_cron_runs_the_images_crontab_with_the_web_roles(default):
    _, stacks = default
    template = assertions.Template.from_stack(stacks.compute)
    assert template.find_resources("AWS::Events::Rule") == {}, "the schedule is JHE's crontab, not ours"
    tasks = {
        r["Properties"]["Family"]: r["Properties"]
        for r in template.find_resources("AWS::ECS::TaskDefinition").values()
    }
    web, cron = tasks["jhe-test-web"], tasks["jhe-test-cron"]
    container = cron["ContainerDefinitions"][0]
    assert container["Command"] == ["supercronic", "/code/deploy/crontab"]
    assert f"@{WEB}" in json.dumps(container["Image"])
    assert "PortMappings" not in container
    assert cron["TaskRoleArn"] == web["TaskRoleArn"], "the application's privileges, no more"
    assert cron["ExecutionRoleArn"] == web["ExecutionRoleArn"]
    assert cron["EphemeralStorage"] == {"SizeInGiB": 50}, "the sweep refetches dropped pulls"
    template.has_resource_properties(
        "AWS::ECS::Service",
        {
            "ServiceName": "jhe-test-cron",
            "DesiredCount": 1,
            "DeploymentConfiguration": assertions.Match.object_like(
                {"MinimumHealthyPercent": 0, "MaximumPercent": 100}
            ),
        },
    )


def test_cron_failures_and_silence_alarm_from_its_log(default):
    _, stacks = default
    template = assertions.Template.from_stack(stacks.monitoring)
    patterns = {
        r["Properties"]["FilterPattern"] for r in template.find_resources("AWS::Logs::MetricFilter").values()
    }
    assert patterns == {'"error running command"', '"job succeeded"'}
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"AlarmName": "jhe-test-cron-silent", "TreatMissingData": "breaching", "EvaluationPeriods": 3},
    )
    template.has_resource_properties("AWS::CloudWatch::Alarm", {"AlarmName": "jhe-test-cron-job-failed"})
    rule = next(iter(template.find_resources("AWS::Events::Rule").values()))
    assert "cron" not in json.dumps(rule["Properties"]["EventPattern"]), "supercronic outlives a failed job"


def test_a_failed_ops_task_is_a_readable_message(default):
    _, stacks = default
    template = assertions.Template.from_stack(stacks.monitoring)
    rule = next(iter(template.find_resources("AWS::Events::Rule").values()))
    transformer = rule["Properties"]["Targets"][0]["InputTransformer"]
    assert set(transformer["InputPathsMap"].values()) >= {
        "$.detail.startedBy",
        "$.detail.containers[0].exitCode",
        "$.detail.taskArn",
    }
    lines = transformer["InputTemplate"].splitlines()
    assert len(lines) > 10 and all(json.loads(line) is not None for line in lines), "a string per line"
    text = json.dumps(transformer["InputTemplate"])
    assert "JHE test: an ops task exited non-zero." in text
    assert "log-group/$252Fjhe$252Ftest$252Fops" in text
    assert "/ecs/v2/clusters/jhe-test/tasks" in text
    assert "Fn::" not in text and "Ref" not in text, "plain text: no deploy-time references"


def test_cron_off_leaves_no_task_and_no_alarms():
    _, stacks = synth(sample_config(compute={"cron": {"enabled": False}}))
    compute = assertions.Template.from_stack(stacks.compute)
    families = {
        r["Properties"]["Family"] for r in compute.find_resources("AWS::ECS::TaskDefinition").values()
    }
    assert "jhe-test-cron" not in families
    assert assertions.Template.from_stack(stacks.monitoring).find_resources("AWS::Logs::MetricFilter") == {}


def test_dns_records_for_both_hosts(default):
    _, stacks = default
    template = assertions.Template.from_stack(stacks.compute)
    names = {r["Properties"]["Name"] for r in template.find_resources("AWS::Route53::RecordSet").values()}
    assert names == {"jhe.example.org.", "mcp.jhe.example.org."}
    template.has_resource_properties(
        "AWS::CertificateManager::Certificate",
        {"DomainName": "jhe.example.org", "SubjectAlternativeNames": ["mcp.jhe.example.org"]},
    )


def test_external_dns_has_no_records_and_imports_a_certificate():
    cfg = sample_config(
        compute={
            "dns": {
                "mode": "external",
                "hosted_zone_id": None,
                "hosted_zone_name": None,
                "certificate_arn": "arn:aws:acm:us-east-2:123456789012:certificate/abc",
            }
        }
    )
    _, stacks = synth(cfg)
    template = assertions.Template.from_stack(stacks.compute)
    assert template.find_resources("AWS::Route53::RecordSet") == {}
    assert template.find_resources("AWS::CertificateManager::Certificate") == {}


def test_mcp_off_leaves_web_and_cron():
    _, stacks = synth(sample_config(compute={"mcp": {"enabled": False}, "mcp_domain_name": None}))
    template = assertions.Template.from_stack(stacks.compute)
    assert len(template.find_resources("AWS::ECS::Service")) == 2
    assert len(template.find_resources("AWS::ElasticLoadBalancingV2::TargetGroup")) == 1
    assert len(template.find_resources("AWS::ECS::TaskDefinition")) == 2, "web and cron"


def test_nat_instance_and_imported_vpc():
    _, stacks = synth(sample_config(network={"nat": "instance"}))
    template = assertions.Template.from_stack(stacks.network)
    assert template.find_resources("AWS::EC2::NatGateway") == {}
    template.resource_count_is("AWS::EC2::Instance", 1)

    imported = sample_config(
        network={
            "mode": "import",
            "existing": {
                "vpc_id": "vpc-0123",
                "availability_zones": ["us-east-2a", "us-east-2b"],
                "public_subnet_ids": ["subnet-pub-a", "subnet-pub-b"],
                "private_subnet_ids": ["subnet-priv-a", "subnet-priv-b"],
                "isolated_subnet_ids": ["subnet-iso-a", "subnet-iso-b"],
            },
        }
    )
    _, stacks = synth(imported)
    template = assertions.Template.from_stack(stacks.network)
    assert template.find_resources("AWS::EC2::VPC") == {}
    assert nag_errors(stacks.compute) == []


def test_bootstrap_qualifier_reaches_the_synthesizer_and_the_deployer_policy():
    _, stacks = synth(sample_config(bootstrap_qualifier="jhesb"))
    template = assertions.Template.from_stack(stacks.access).to_json()
    assert "/cdk-bootstrap/jhesb/version" in json.dumps(template["Parameters"]), "the synthesizer"
    assert "role/cdk-jhesb-deploy-role-" in json.dumps(template["Resources"]), "the deployer policy"


def test_access_stack_holds_the_policy_alone_and_attaches_it_to_the_configured_principals(default):
    _, stacks = default
    assert stacks.access.stack_name == "jhe-test-access"
    assert stacks.access.termination_protection is False
    assert stacks.access.dependencies == [], "depends on nothing: it is deployed first"
    for other in (stacks.shared, stacks.data, stacks.ops, stacks.compute):
        assert assertions.Template.from_stack(other).find_resources("AWS::IAM::ManagedPolicy") == {}
    policy = next(
        iter(assertions.Template.from_stack(stacks.access).find_resources("AWS::IAM::ManagedPolicy").values())
    )["Properties"]
    assert policy["ManagedPolicyName"] == "JheDeployer-test"
    assert "Users" not in policy and "Roles" not in policy

    _, stacks = synth(
        sample_config(access={"deployer_users": ["alice"], "deployer_roles": ["github-deploy"]})
    )
    assertions.Template.from_stack(stacks.access).has_resource_properties(
        "AWS::IAM::ManagedPolicy", {"Users": ["alice"], "Roles": ["github-deploy"]}
    )


def test_sandbox_shaped_config_passes_the_nag_checks_too():
    """The sandbox turns the guards off and runs a NAT instance; the checks still pass, with
    the reasons recorded, and the NAT instance's volume is encrypted."""
    _, stacks = synth(
        sample_config(
            termination_protection=False,
            network={"nat": "instance"},
            data={"multi_az": False, "deletion_protection": False},
        )
    )
    for stack in (stacks.network, stacks.shared, stacks.data, stacks.ops, stacks.compute):
        assert nag_errors(stack) == [], stack.stack_name
    network = assertions.Template.from_stack(stacks.network)
    nat_sg = next(
        v["Properties"] for k, v in network.find_resources("AWS::EC2::SecurityGroup").items() if "Nat" in k
    )
    ingress = nat_sg["SecurityGroupIngress"]
    assert len(ingress) == 1 and ingress[0]["IpProtocol"] == "-1", (
        "the private subnets must be able to reach the NAT instance"
    )
    assert "CidrBlock" in json.dumps(ingress[0]["CidrIp"]), "from the VPC's own range"
    assert "0.0.0.0/0" not in json.dumps(ingress), "and nothing outside the VPC may"
    nat_id, nat = next(iter(network.find_resources("AWS::EC2::Instance").items()))
    other = synth(sample_config(network={"nat": "instance", "nat_instance_type": "t4g.small"}))[1]
    other_id = next(iter(assertions.Template.from_stack(other.network).find_resources("AWS::EC2::Instance")))
    assert nat_id != other_id, "a different type or boot script replaces the instance"
    assert nat["CreationPolicy"] == {"ResourceSignal": {"Count": 1, "Timeout": "PT10M"}}, (
        "the stack waits for the boot script"
    )
    assert nat["Properties"]["InstanceType"] == "t4g.micro"
    script = json.dumps(nat["Properties"]["UserData"])
    for needle in (
        "set -euo pipefail",
        "MASQUERADE",
        "iptables-save",
        "cfn-signal",
        "ip -o -4 route show to default",
    ):
        assert needle in script, needle
    network.has_resource_properties(
        "AWS::EC2::Instance",
        {
            "BlockDeviceMappings": [
                {"DeviceName": "/dev/xvda", "Ebs": {"Encrypted": True, "VolumeType": "gp3"}}
            ]
        },
    )


def web_container(stacks) -> dict:
    template = assertions.Template.from_stack(stacks.compute)
    task = next(
        r
        for r in template.find_resources("AWS::ECS::TaskDefinition").values()
        if r["Properties"]["Family"] == "jhe-test-web"
    )
    return task["Properties"]["ContainerDefinitions"][0]


def test_the_database_certificate_is_verified_by_default(default):
    _, stacks = default
    env = {e["Name"]: e["Value"] for e in web_container(stacks)["Environment"]}
    assert env["DB_SSLMODE"] == "verify-full"
    assert env["DB_SSLROOTCERT"] == "/etc/ssl/certs/db-ca-bundle.pem"
    _, off = synth(sample_config(data={"verify_server_certificate": False}))
    assert "DB_SSLMODE" not in {e["Name"] for e in web_container(off)["Environment"]}


def test_no_invite_code_secret_and_a_stop_timeout_for_the_pull(default):
    _, stacks = default
    container = web_container(stacks)
    assert "REGISTRATION_INVITE_CODE" not in {s["Name"] for s in container["Secrets"]}, (
        "the invite code is a Settings row in JHE; the environment variable was inert"
    )
    assert container["StopTimeout"] == 120


def test_bundle_bucket_expires_old_versions(default):
    _, stacks = default
    template = assertions.Template.from_stack(stacks.data)
    bucket = next(
        v["Properties"]
        for k, v in template.find_resources("AWS::S3::Bucket").items()
        if k.startswith("BundleBucket")
    )
    assert bucket["VersioningConfiguration"] == {"Status": "Enabled"}
    rules = bucket["LifecycleConfiguration"]["Rules"]
    assert any(r.get("NoncurrentVersionExpiration") == {"NoncurrentDays": 90} for r in rules)
    assert any(r.get("AbortIncompleteMultipartUpload") == {"DaysAfterInitiation": 1} for r in rules)
    _, forever = synth(sample_config(data={"bundle_noncurrent_version_days": None}))
    kept = next(
        v["Properties"]
        for k, v in assertions.Template.from_stack(forever.data).find_resources("AWS::S3::Bucket").items()
        if k.startswith("BundleBucket")
    )
    # no version expiry, but the incomplete-upload cleanup stays
    kept_rules = kept["LifecycleConfiguration"]["Rules"]
    assert not any("NoncurrentVersionExpiration" in r for r in kept_rules)
    assert any(r.get("AbortIncompleteMultipartUpload") == {"DaysAfterInitiation": 1} for r in kept_rules)


def test_operator_secrets_are_not_stack_resources(default):
    """jhe/<env>/service and jhe/<env>/mcp are created by `jhe-deploy secrets init`, never by a
    stack: CloudFormation regenerates a secret's value on any change to how it is generated,
    which would empty what an operator filled. The stacks reference them by name and grant
    the execution role a read on the name plus Secrets Manager's suffix."""
    _, stacks = default
    data = assertions.Template.from_stack(stacks.data)
    names = {r["Properties"].get("Name") for r in data.find_resources("AWS::SecretsManager::Secret").values()}
    assert names == {"jhe/test/database", "jhe/test/bootstrap-admin"}
    container = web_container(stacks)
    value_from = {s["Name"]: json.dumps(s["ValueFrom"]) for s in container["Secrets"]}
    assert "secret:jhe/test/service:SECRET_KEY::" in value_from["SECRET_KEY"]
    policies = json.dumps(assertions.Template.from_stack(stacks.compute).find_resources("AWS::IAM::Policy"))
    assert "secret:jhe/test/service-??????" in policies
    assert "secret:jhe/test/mcp-??????" in policies
