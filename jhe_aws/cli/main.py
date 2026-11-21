"""`jhe-deploy`: the deployment, in the order it has to happen.

    bootstrap             cdk bootstrap the account and region in the configuration (once)
    access                the JheDeployer policy, attached to the configured users and roles (once)
    build                 build both images for arm64 from a JHE checkout, push, print the digests
    deploy                foundation stacks -> secrets check -> ops stack -> migrate and bootstrap
                          -> compute (and monitoring); a failed migration stops before compute
    run -- <command>      a one-off management command as the ops task, its log and exit code
    secrets init|set|show|restore-previous
                          the service and mcp secrets (and redcap, when configured), without a value
                          ever crossing the terminal unless you type it
    admin create          the first admin, password in a secret, never in a shell or CloudTrail
    admin forget          replace that secret's value once the password is changed (never delete it)
    restart               fresh web and mcp tasks, same image: after a secret or an admin change
                          the running processes cached
    down / up             take the replaceable stacks down and stop the database; bring them back
    logs web|ops|mcp|redcap
                          the task log, --since 30m, --filter 'pattern'
    status                what is deployed

Every subcommand takes --config, the environment's configuration file; the environment's name
comes from it. The AWS calls are in `aws.py`; `cdk` and `docker` run as subprocesses with their
output shown.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import secrets as pysecrets
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from botocore.exceptions import ProfileNotFound

from jhe_aws.config import ConfigError, EnvironmentConfig, load_config
from jhe_aws.images import DIGEST, PLACEHOLDER_DIGEST, Images
from jhe_aws.stacks import ACCESS, FOUNDATION, REPLACEABLE

from .aws import Aws

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SERVICE_FIELDS = (
    "SECRET_KEY",
    "OIDC_RSA_PRIVATE_KEY",
    "PATIENT_ACCESS_ENCRYPTION_KEYS",
)
REQUIRED_MCP_FIELDS = ("JHE_CLIENT_ID", "JHE_CLIENT_SECRET", "MCP_BROKER_KEY")
# Every field each secret holds. The secrets are created by `secrets init`, never by a stack,
# because CloudFormation regenerates a secret's value on any change to how it is generated,
# which would empty what an operator filled. New fields are added here and by `secrets init`.
SECRET_FIELDS = {
    "service": (
        "SECRET_KEY",
        "OIDC_RSA_PRIVATE_KEY",
        "PATIENT_ACCESS_ENCRYPTION_KEYS",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "OW_S3_ACCESS_KEY",
        "OW_S3_SECRET_KEY",
    ),
    "mcp": ("JHE_CLIENT_ID", "JHE_CLIENT_SECRET", "MCP_BROKER_KEY"),
    # Only for a deployment with `redcap_integration` set (see secret_names).
    "redcap": ("JHE_API_KEY", "REDCAP_API_TOKEN", "TRIGGER_TOKEN"),
}
GENERATED_FIELDS = {
    "service": REQUIRED_SERVICE_FIELDS,
    "mcp": ("MCP_BROKER_KEY",),
    "redcap": ("TRIGGER_TOKEN",),
}
REQUIRED_REDCAP_FIELDS = ("JHE_API_KEY", "REDCAP_API_TOKEN", "TRIGGER_TOKEN")
IMAGES_PARAMETER = "/jhe/{env}/images"


class Fail(Exception):
    pass


# --- helpers ----------------------------------------------------------------------------------


def say(message: str) -> None:
    print(f"==> {message}", flush=True)


def sh(
    args: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, capture: bool = False
) -> str:
    say(" ".join(args))
    result = subprocess.run(
        list(args), cwd=cwd, env={**os.environ, **(env or {})}, text=True, capture_output=capture, check=False
    )
    if result.returncode != 0:
        if capture:
            sys.stderr.write(result.stderr)
        raise Fail(f"{args[0]} exited {result.returncode}")
    return result.stdout if capture else ""


def cdk(
    verb: str,
    stacks: Sequence[str],
    cfg: EnvironmentConfig,
    config_path: Path,
    *,
    images: Images | None = None,
    mcp_enabled: bool = True,
    profile: str | None = None,
    extra: Sequence[str] = (),
) -> None:
    args = ["cdk", verb, *stacks, "-c", f"config={config_path.resolve()}"]
    if verb != "bootstrap":
        args.append("--exclusively")
    if images is not None:
        args += ["-c", f"web_image={images.web}", "-c", f"mcp_image={images.mcp}"]
    if not mcp_enabled:
        args += ["-c", "mcp_enabled=false"]
    if profile:
        args += ["--profile", profile]
    if verb == "deploy":
        args += ["--require-approval", "never"]
    if verb == "destroy":
        args += ["--force"]
    sh([*args, *extra], cwd=ROOT, env={"JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1"})


def stack_names(cfg: EnvironmentConfig, parts: Sequence[str]) -> list[str]:
    parts = [
        p
        for p in parts
        if not (p == "monitoring" and not cfg.monitoring.enabled)
        and not (p == "redcap" and not cfg.redcap_enabled)
    ]
    return [cfg.stack_name(p) for p in parts]


TASK_OUTPUTS = (
    "ClusterName",
    "TaskDefinitionFamily",
    "ContainerName",
    "SubnetIds",
    "SecurityGroupId",
    "LogGroupName",
)


def task_outputs(aws: Aws, cfg: EnvironmentConfig, stack: str) -> dict[str, str]:
    """The RunTask facts the ops stack publishes for its task definition."""
    name = cfg.stack_name(stack)
    outputs = aws.stack_outputs(name)
    for key in TASK_OUTPUTS:
        if key not in outputs:
            raise Fail(f"{name} has no output {key}; deploy it first")
    return outputs


def run_task(aws: Aws, cfg: EnvironmentConfig, stack: str, command: list[str] | None, label: str) -> int:
    """Run the task definition the named stack publishes with `command` (None: its default),
    stream its log after it stops, return the container's exit code."""
    out = task_outputs(aws, cfg, stack)
    task_arn = aws.run_task(
        cluster=out["ClusterName"],
        family=out["TaskDefinitionFamily"],
        subnets=out["SubnetIds"].split(","),
        security_group=out["SecurityGroupId"],
        container=out["ContainerName"],
        command=command,
        started_by=f"jhe-deploy {label}",
    )
    say(f"task {task_arn.rsplit('/', 1)[-1]} started; waiting")
    outcome = aws.wait_task(out["ClusterName"], task_arn, out["ContainerName"])
    # the awslogs driver names streams <prefix>/<container>/<task id>; the prefix is the log
    # group's last path element (ops)
    stream = f"{out['LogGroupName'].rsplit('/', 1)[-1]}/{out['ContainerName']}/{outcome.task_id}"
    for line in aws.log_lines(out["LogGroupName"], stream):
        print(f"    {line}")
    if outcome.exit_code is None:
        say(f"task stopped without an exit code: {outcome.stopped_reason}")
        return 1
    say(f"task exited {outcome.exit_code}")
    return outcome.exit_code


def remember_images(aws: Aws, cfg: EnvironmentConfig, images: Images) -> None:
    aws.put_parameter(
        IMAGES_PARAMETER.format(env=cfg.name), json.dumps({"web": images.web, "mcp": images.mcp})
    )


def remembered_images(aws: Aws, cfg: EnvironmentConfig) -> Images | None:
    raw = aws.get_parameter(IMAGES_PARAMETER.format(env=cfg.name))
    return Images(**json.loads(raw)) if raw else None


def describe_images(aws: Aws, cfg: EnvironmentConfig, images: Images) -> list[str]:
    """One line per image: the digest and the tag it was pushed under, which names the commit
    and the build time."""
    lines = []
    for name, digest in (("web", images.web), ("mcp", images.mcp)):
        if digest == PLACEHOLDER_DIGEST:
            continue
        tags = aws.image_tags(f"jhe/{cfg.name}/{name}", digest)
        lines.append(f"{name:<4} {digest}  {', '.join(tags) if tags else '(no tag in ECR)'}")
    return lines


# --- secrets ----------------------------------------------------------------------------------


def generate_service_value(field: str) -> str:
    if field == "SECRET_KEY":
        return pysecrets.token_urlsafe(48)
    if field in ("MCP_BROKER_KEY", "TRIGGER_TOKEN"):
        return pysecrets.token_urlsafe(36)
    if field == "PATIENT_ACCESS_ENCRYPTION_KEYS":
        return base64.urlsafe_b64encode(os.urandom(32)).decode()  # one Fernet key; rotate by prepending
    if field == "OIDC_RSA_PRIVATE_KEY":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode()
    if field == "MCP_BROKER_KEY":
        return pysecrets.token_urlsafe(36)
    raise Fail(f"no generator for {field}; set it with `secrets set`")


GENERATED = (
    "SECRET_KEY",
    "PATIENT_ACCESS_ENCRYPTION_KEYS",
    "OIDC_RSA_PRIVATE_KEY",
    "MCP_BROKER_KEY",
)


def secret_names(cfg: EnvironmentConfig) -> tuple[str, ...]:
    """The secrets this deployment has: the redcap one only when `redcap_integration` is set."""
    return tuple(w for w in SECRET_FIELDS if w != "redcap" or cfg.redcap_enabled)


def secret_json(aws: Aws, cfg: EnvironmentConfig, which: str) -> dict[str, str]:
    name = f"jhe/{cfg.name}/{which}"
    if not aws.secret_exists(name):
        raise Fail(f"{name} does not exist yet: run `jhe-deploy secrets init`")
    return aws.get_secret_json(name)


def secrets_init(aws: Aws, cfg: EnvironmentConfig) -> None:
    """Create each secret if it does not exist (every field, empty, under the data key), then
    fill every generatable field that is still empty. Never overwrites a value."""
    kms_key = f"alias/jhe-{cfg.name}-data" if cfg.data.customer_managed_keys else None
    tags = {"jhe:environment": cfg.name, "jhe:managed-by": "jhe-deploy"}
    for which in secret_names(cfg):
        fields = SECRET_FIELDS[which]
        name = f"jhe/{cfg.name}/{which}"
        if not aws.secret_exists(name):
            aws.create_secret_json(name, {f: "" for f in fields}, kms_key, tags)
            say(f"{name}: created")
        current = aws.get_secret_json(name)
        for field in fields:
            current.setdefault(field, "")
        missing = [f for f in GENERATED_FIELDS[which] if not current.get(f)]
        if not missing:
            say(f"{name}: nothing to fill")
            continue
        for field in missing:
            current[field] = generate_service_value(field)
        aws.put_secret_json(name, current)
        say(f"{name}: filled {', '.join(missing)}")


def secrets_set(aws: Aws, cfg: EnvironmentConfig, which: str, pairs: Sequence[str]) -> None:
    name = f"jhe/{cfg.name}/{which}"
    current = secret_json(aws, cfg, which)
    for pair in pairs:
        if "=" not in pair:
            raise Fail(f"expected KEY=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        if key not in current:
            raise Fail(f"{name} has no field {key}; it has {', '.join(sorted(current))}")
        current[key] = value
    aws.put_secret_json(name, current)
    say(f"{name}: set {', '.join(p.split('=', 1)[0] for p in pairs)}")


def secrets_show(aws: Aws, cfg: EnvironmentConfig) -> None:
    for which in secret_names(cfg):
        name = f"jhe/{cfg.name}/{which}"
        current = secret_json(aws, cfg, which)
        print(name)
        for key in sorted(current):
            print(f"    {key:<32} {'set' if current[key] else 'EMPTY'}")


def unfilled(aws: Aws, cfg: EnvironmentConfig) -> tuple[list[str], list[str]]:
    """(service fields still empty, mcp fields still empty)."""
    service = secret_json(aws, cfg, "service")
    mcp = secret_json(aws, cfg, "mcp")
    return [f for f in REQUIRED_SERVICE_FIELDS if not service.get(f)], [
        f for f in REQUIRED_MCP_FIELDS if not mcp.get(f)
    ]


# --- build ------------------------------------------------------------------------------------


def rds_ca_bundle_url(region: str) -> str:
    """AWS publishes the RDS certificate authorities as plain PEM bundles, one per region."""
    return f"https://truststore.pki.rds.amazonaws.com/{region}/{region}-bundle.pem"


def build(aws: Aws, cfg: EnvironmentConfig, jhe_path: Path) -> Images:
    if not (jhe_path / "manage.py").exists() or not (jhe_path / "mcp_server" / "Dockerfile").exists():
        raise Fail(f"{jhe_path} is not a JupyterHealth Exchange checkout")
    shared = aws.stack_outputs(cfg.stack_name("shared"))
    user, password, registry = aws.ecr_login()
    subprocess.run(
        ["docker", "login", "--username", user, "--password-stdin", registry],
        input=password,
        text=True,
        check=True,
    )
    sha = sh(["git", "-C", str(jhe_path), "rev-parse", "--short=12", "HEAD"], capture=True).strip()
    dirty = bool(sh(["git", "-C", str(jhe_path), "status", "--porcelain"], capture=True).strip())
    created = dt.datetime.now(dt.UTC)
    tag = f"{sha}{'-dirty' if dirty else ''}-{created:%Y%m%dT%H%M%SZ}"
    full_sha = sh(["git", "-C", str(jhe_path), "rev-parse", "HEAD"], capture=True).strip()
    try:
        source = sh(["git", "-C", str(jhe_path), "remote", "get-url", "origin"], capture=True).strip()
    except Fail:
        source = "(no origin remote)"
    # OCI labels: the image carries its own origin, whatever happens to its tag.
    labels = [
        f"org.opencontainers.image.revision={full_sha}{'-dirty' if dirty else ''}",
        f"org.opencontainers.image.created={created:%Y-%m-%dT%H:%M:%SZ}",
        f"org.opencontainers.image.source={source}",
    ]
    digests = {}
    # The RDS CA bundle for the region, so the JHE image can verify the server's certificate.
    build_args = {"web": ["--build-arg", f"DB_CA_BUNDLE_URL={rds_ca_bundle_url(cfg.region)}"], "mcp": []}
    for name, context, uri in (
        ("web", jhe_path, shared["WebRepositoryUri"]),
        ("mcp", jhe_path / "mcp_server", shared["McpRepositoryUri"]),
    ):
        sh(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/arm64",
                "--provenance=false",
                *build_args[name],
                *(arg for label in labels for arg in ("--label", label)),
                "-t",
                f"{uri}:{tag}",
                "--push",
                str(context),
            ]
        )
        repository = uri.split("/", 1)[1]
        digests[name] = aws.image_digest(repository, tag)
        say(f"{name}: {uri}:{tag} = {digests[name]}")
    return Images(**digests)


# --- commands ---------------------------------------------------------------------------------


def cmd_bootstrap(args: argparse.Namespace, aws: Aws, cfg: EnvironmentConfig, config_path: Path) -> int:
    """`cdk bootstrap` for the account and region in the configuration. The CDK CLI synthesizes
    the app even to bootstrap, so the configuration has to be passed here too."""
    extra = ["--qualifier", cfg.bootstrap_qualifier]
    if args.execution_policies:
        extra += ["--cloudformation-execution-policies", args.execution_policies]
    cdk(
        "bootstrap",
        [f"aws://{cfg.account_id}/{cfg.region}"],
        cfg,
        config_path,
        profile=args.profile,
        extra=extra,
    )
    return 0


def cmd_access(args: argparse.Namespace, aws: Aws, cfg: EnvironmentConfig, config_path: Path) -> int:
    """The access stack: the deployer policy and who holds it. Run by an administrator, once,
    and again when the `access` section changes; `deploy` never touches it."""
    cdk("deploy", stack_names(cfg, (ACCESS,)), cfg, config_path, profile=args.profile)
    outputs = aws.stack_outputs(cfg.stack_name(ACCESS))
    say(f"deployer policy: {outputs.get('DeployerPolicyArn', '?')}")
    holders = [*cfg.access.deployer_users, *cfg.access.deployer_roles]
    say(f"attached to: {', '.join(holders) if holders else 'nobody (the access section is empty)'}")
    return 0


def cmd_build(args: argparse.Namespace, aws: Aws, cfg: EnvironmentConfig, config_path: Path) -> int:
    images = build(aws, cfg, Path(args.jhe_path).resolve())
    print(json.dumps({"web": images.web, "mcp": images.mcp}))
    return 0


def cmd_deploy(args: argparse.Namespace, aws: Aws, cfg: EnvironmentConfig, config_path: Path) -> int:
    say(f"1/5 foundation: {', '.join(stack_names(cfg, FOUNDATION))}")
    cdk("deploy", stack_names(cfg, FOUNDATION), cfg, config_path, profile=args.profile)

    say("2/5 images")
    if args.web_image and args.mcp_image:
        images = Images(web=args.web_image, mcp=args.mcp_image)
    elif args.jhe_path:
        images = build(aws, cfg, Path(args.jhe_path).resolve())
    else:
        raise Fail("pass --jhe-path to build, or --web-image and --mcp-image digests")

    say("3/5 secrets")
    service_missing, mcp_missing = unfilled(aws, cfg)
    if service_missing:
        raise Fail(
            f"jhe/{cfg.name}/service still has empty fields: {', '.join(service_missing)}. "
            "Run `jhe-deploy secrets init` (generates them) and try again."
        )
    mcp_enabled = cfg.mcp_enabled and not mcp_missing
    if cfg.mcp_enabled and mcp_missing:
        say(
            f"MCP stays off this deploy: jhe/{cfg.name}/mcp has empty {', '.join(mcp_missing)}. "
            "Register the MCP client in JHE, `jhe-deploy secrets set --secret mcp JHE_CLIENT_ID=... "
            "JHE_CLIENT_SECRET=...`, and deploy again."
        )

    if cfg.redcap_enabled:
        redcap_name = f"jhe/{cfg.name}/redcap"
        if not aws.secret_exists(redcap_name):
            raise Fail(f"{redcap_name} does not exist yet: run `jhe-deploy secrets init`")
        redcap_missing = [f for f in REQUIRED_REDCAP_FIELDS if not secret_json(aws, cfg, "redcap").get(f)]
        if redcap_missing:
            say(
                f"REDCap deploys, but {redcap_name} has empty {', '.join(redcap_missing)}, so the trigger "
                f"answers 503 until `jhe-deploy secrets set --secret redcap ...` fills them."
            )

    say(f"4/5 ops stack and migrate: {cfg.stack_name('ops')}")
    cdk(
        "deploy",
        [cfg.stack_name("ops")],
        cfg,
        config_path,
        images=images,
        mcp_enabled=mcp_enabled,
        profile=args.profile,
    )
    if not args.skip_migrate:
        code = run_task(aws, cfg, "ops", None, "migrate")
        if code != 0:
            raise Fail(
                f"migrate exited {code}; the compute stack was not touched and keeps serving the previous "
                "image"
            )
        # The reference data and the web UI's OAuth client a fresh database lacks; idempotent,
        # so it runs after every migrate.
        code = run_task(aws, cfg, "ops", ["python", "manage.py", "bootstrap"], "bootstrap")
        if code != 0:
            raise Fail(f"bootstrap exited {code}; the compute stack was not touched")

    say(f"5/5 compute: {', '.join(stack_names(cfg, ('compute', 'monitoring', 'redcap')))}")
    cdk(
        "deploy",
        stack_names(cfg, ("compute", "monitoring", "redcap")),
        cfg,
        config_path,
        images=images,
        mcp_enabled=mcp_enabled,
        profile=args.profile,
    )
    remember_images(aws, cfg, images)
    outputs = aws.stack_outputs(cfg.stack_name("compute"))
    say(
        f"deployed: {outputs.get('WebUrl', '?')}"
        + (f" and {outputs['McpUrl']}" if "McpUrl" in outputs else "")
    )
    if cfg.redcap_enabled:
        redcap_outputs = aws.stack_outputs(cfg.stack_name("redcap"))
        say(f"REDCap trigger: {redcap_outputs.get('RedcapTriggerUrl', '?')}?token=<TRIGGER_TOKEN>")
    for line in describe_images(aws, cfg, images):
        say(line)
    return 0


def cmd_run(args: argparse.Namespace, aws: Aws, cfg: EnvironmentConfig, config_path: Path) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise Fail(
            "give the command after `--`, e.g. `jhe-deploy run -- python manage.py patient_access_check`"
        )
    return run_task(aws, cfg, "ops", command, " ".join(command)[:24])


def secrets_restore_previous(aws: Aws, cfg: EnvironmentConfig, which: str) -> None:
    """Put the version before the current one back, field by field: a value the previous version
    had wins, a field only the current version has is kept. For when a stack update regenerated
    the secret (CloudFormation does that on any change to GenerateSecretString) and emptied
    what `secrets init` and `set` had filled. Nothing is printed but field names."""
    name = f"jhe/{cfg.name}/{which}"
    current = secret_json(aws, cfg, which)
    previous = aws.get_secret_json(name, version_stage="AWSPREVIOUS")
    restored = {**current, **{k: v for k, v in previous.items() if v}}
    changed = sorted(k for k in restored if restored[k] != current.get(k))
    if not changed:
        say(f"{name}: the previous version adds nothing; unchanged")
        return
    aws.put_secret_json(name, restored)
    say(f"{name}: restored {', '.join(changed)} from the previous version")


def cmd_secrets(args: argparse.Namespace, aws: Aws, cfg: EnvironmentConfig, config_path: Path) -> int:
    if args.action == "init":
        secrets_init(aws, cfg)
    elif args.action == "set":
        secrets_set(aws, cfg, args.secret, args.pairs)
    elif args.action == "restore-previous":
        secrets_restore_previous(aws, cfg, args.secret)
    else:
        secrets_show(aws, cfg)
    return 0


def cmd_admin(args: argparse.Namespace, aws: Aws, cfg: EnvironmentConfig, config_path: Path) -> int:
    name = f"jhe/{cfg.name}/bootstrap-admin"
    if args.action == "forget":
        # The ops task definition reads this secret at every start, so it is never deleted: its
        # value is replaced, and the password that was in it stops being anywhere.
        aws.put_secret_string(name, pysecrets.token_urlsafe(18))
        say(f"{name}: replaced with a value nobody has read")
        return 0
    if not args.email:
        raise Fail("admin create needs --email")
    aws.put_secret_string(name, pysecrets.token_urlsafe(18))
    code = run_task(
        aws,
        cfg,
        "ops",
        ["python", "manage.py", "createsuperuser", "--no-input", "--email", args.email],
        "admin",
    )
    if code == 0:
        say(
            f"admin {args.email} created. The password is the value of the secret {name}: read it with "
            f"`aws secretsmanager get-secret-value --secret-id {name} --query SecretString --output text`, "
            "log in, change it, then `jhe-deploy admin forget` (never delete the secret: the ops task "
            "reads it at every start)."
        )
    return code


def cmd_down(args: argparse.Namespace, aws: Aws, cfg: EnvironmentConfig, config_path: Path) -> int:
    images = Images(web=aws.task_definition_image(f"jhe-{cfg.name}-ops").rsplit("@", 1)[-1])
    try:
        images = remembered_images(aws, cfg) or images
    except Exception:  # noqa: BLE001 - the parameter is a convenience
        pass
    remember_images(aws, cfg, images)
    cdk(
        "destroy",
        stack_names(cfg, ("redcap", "monitoring", "compute")),
        cfg,
        config_path,
        images=images,
        profile=args.profile,
    )
    identifier = f"jhe-{cfg.name}-db"
    if aws.db_status(identifier) == "available":
        aws.stop_db(identifier)
        say(f"{identifier} stopping; AWS starts a stopped instance again after seven days")
    say("down: network, shared, data and ops stay; `jhe-deploy up` brings compute back")
    return 0


def cmd_up(args: argparse.Namespace, aws: Aws, cfg: EnvironmentConfig, config_path: Path) -> int:
    images = remembered_images(aws, cfg)
    if images is None:
        raise Fail("no remembered images; run `jhe-deploy deploy` instead")
    identifier = f"jhe-{cfg.name}-db"
    if aws.db_status(identifier) == "stopped":
        aws.start_db(identifier)
    say(f"waiting for {identifier} to be available")
    aws.wait_db(identifier, "available")
    _, mcp_missing = unfilled(aws, cfg)
    mcp_enabled = cfg.mcp_enabled and not mcp_missing
    cdk(
        "deploy",
        stack_names(cfg, REPLACEABLE),
        cfg,
        config_path,
        images=images,
        mcp_enabled=mcp_enabled,
        profile=args.profile,
    )
    return 0


def cmd_restart(args: argparse.Namespace, aws: Aws, cfg: EnvironmentConfig, config_path: Path) -> int:
    cluster = f"jhe-{cfg.name}"
    for part in ("web", "mcp", "cron"):
        service = f"jhe-{cfg.name}-{part}"
        if aws.restart_service(cluster, service):
            say(f"{service}: new deployment started; the old tasks drain once the new ones are healthy")
        else:
            say(f"{service}: not deployed")
    return 0


SINCE = re.compile(r"^(\d+)([smhd])$")
SINCE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def cmd_logs(args: argparse.Namespace, aws: Aws, cfg: EnvironmentConfig, config_path: Path) -> int:
    match = SINCE.match(args.since)
    if not match:
        raise Fail(f"--since takes a number and a unit (30m, 2h, 1d), not {args.since!r}")
    seconds = int(match.group(1)) * SINCE_UNITS[match.group(2)]
    group = f"/jhe/{cfg.name}/{args.group}"
    events = aws.log_events(group, seconds, args.filter)
    for timestamp, message in events:
        stamp = dt.datetime.fromtimestamp(timestamp / 1000, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"{stamp} {message.rstrip()}")
    if not events:
        say(
            f"{group}: nothing in the last {args.since}"
            + (f" matching {args.filter!r}" if args.filter else "")
        )
    return 0


def cmd_status(args: argparse.Namespace, aws: Aws, cfg: EnvironmentConfig, config_path: Path) -> int:
    for part in (ACCESS, *FOUNDATION, *REPLACEABLE):
        if part == "redcap" and not cfg.redcap_enabled:
            continue
        name = cfg.stack_name(part)
        print(f"{name:<32} {aws.stack_status(name) or 'not deployed'}")
    identifier = f"jhe-{cfg.name}-db"
    try:
        print(f"{identifier:<32} {aws.db_status(identifier)}")
    except Exception:  # noqa: BLE001
        print(f"{identifier:<32} not found")
    if aws.stack_status(cfg.stack_name("data")):
        data = aws.stack_outputs(cfg.stack_name("data"))
        for key, label in (("BundleBucketName", "bundle bucket"), ("DatabaseEndpoint", "database endpoint")):
            if key in data:
                print(f"{label:<32} {data[key]}")
    images = remembered_images(aws, cfg)
    if images:
        for line in describe_images(aws, cfg, images):
            print(f"{'image':<32} {line}")
    if aws.stack_status(cfg.stack_name("compute")):
        outputs = aws.stack_outputs(cfg.stack_name("compute"))
        for key in ("WebUrl", "McpUrl", "AlbDnsName"):
            if key in outputs:
                print(f"{key:<32} {outputs[key]}")
    return 0


# --- entry ------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jhe-deploy", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("-c", "--config", required=True, help="the environment's configuration file")
    p.add_argument("--profile", help="AWS CLI profile")
    sub = p.add_subparsers(dest="command", required=True)

    boot = sub.add_parser("bootstrap", help="cdk bootstrap the account and region in the configuration")
    boot.add_argument(
        "--execution-policies",
        help="comma-separated policy ARNs CloudFormation deploys with (default: administrator)",
    )
    boot.set_defaults(fn=cmd_bootstrap)

    sub.add_parser(
        "access", help="the JheDeployer policy, attached to the configured users and roles"
    ).set_defaults(fn=cmd_access)

    b = sub.add_parser("build", help="build and push both images, print digests")
    b.add_argument("--jhe-path", required=True, help="a JupyterHealth Exchange checkout")
    b.set_defaults(fn=cmd_build)

    d = sub.add_parser("deploy", help="the whole sequence")
    d.add_argument("--jhe-path", help="build from this checkout")
    d.add_argument("--web-image", help="digest of an image already pushed")
    d.add_argument("--mcp-image", help="digest of an image already pushed")
    d.add_argument(
        "--skip-migrate", action="store_true", help="deploy without migrate and bootstrap (not for upgrades)"
    )
    d.set_defaults(fn=cmd_deploy)

    r = sub.add_parser("run", help="a management command as the ops task")
    r.add_argument("command", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("secrets", help="fill or inspect the service, mcp (and redcap) secrets")
    s.add_argument("action", choices=("init", "set", "show", "restore-previous"))
    s.add_argument(
        "--secret",
        choices=("service", "mcp", "redcap"),
        default="service",
        help="for set, restore-previous (redcap only with redcap_integration set)",
    )
    s.add_argument("pairs", nargs="*", help="KEY=VALUE for set")
    s.set_defaults(fn=cmd_secrets)

    a = sub.add_parser("admin", help="create the first admin; forget its bootstrap password")
    a.add_argument("action", choices=("create", "forget"))
    a.add_argument("--email", help="for create")
    a.set_defaults(fn=cmd_admin)

    sub.add_parser("down", help="destroy compute and monitoring, stop the database").set_defaults(fn=cmd_down)
    sub.add_parser("up", help="start the database, redeploy from the remembered images").set_defaults(
        fn=cmd_up
    )
    sub.add_parser("restart", help="fresh web, mcp and cron tasks from the same image").set_defaults(
        fn=cmd_restart
    )
    lg = sub.add_parser("logs", help="a task log group, oldest first")
    lg.add_argument("group", choices=("web", "cron", "ops", "mcp", "redcap"))
    lg.add_argument("--since", default="30m", help="how far back: 30m, 2h, 1d (default 30m)")
    lg.add_argument("--filter", help="a CloudWatch Logs filter pattern, e.g. 'job' or '?error ?Traceback'")
    lg.set_defaults(fn=cmd_logs)

    sub.add_parser("status", help="what is deployed").set_defaults(fn=cmd_status)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config_path = Path(args.config)
    try:
        cfg = load_config(config_path)
        for digest in (getattr(args, "web_image", None), getattr(args, "mcp_image", None)):
            if digest and not DIGEST.match(digest):
                raise Fail(f"{digest!r} is not an image digest (sha256:<64 hex>)")
        try:
            aws = Aws(cfg.region, args.profile)
        except ProfileNotFound as e:
            raise Fail(f"{e} (the names in ~/.aws/config, or omit --profile for the default)") from e
        return args.fn(args, aws, cfg, config_path)
    except (ConfigError, Fail) as e:
        print(f"jhe-deploy: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
