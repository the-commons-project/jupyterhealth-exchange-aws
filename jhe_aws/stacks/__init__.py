"""The stacks and the order they depend on each other.

    network ──> shared (ECR, cluster)
       │           │
       └──> data ──┴──> ops (the operator's task) ──> compute (ALB, web, mcp, cron)
                                                                   │
                                                                   └──> monitoring
    redcap (a Lambda for REDCap's data entry trigger; only when `redcap_integration` is set;
            needs only the data stack's key)
    access (the deployer policy; depends on nothing, deployed by `jhe-deploy access` only)

Network, shared and data hold everything stateful or slow to replace and carry termination
protection; ops, compute and monitoring can be destroyed and redeployed freely, which is
what `jhe-deploy down` and `up` do. Access is outside both lists: `deploy` never touches it.
"""

from __future__ import annotations

from dataclasses import dataclass

import aws_cdk as cdk

from jhe_aws.config import EnvironmentConfig
from jhe_aws.images import Images

from .access import AccessStack
from .compute import ComputeStack
from .data import DataStack
from .monitoring import MonitoringStack
from .network import NetworkStack
from .ops import OpsStack
from .redcap import RedcapStack
from .shared import SharedStack

FOUNDATION = ("network", "shared", "data")
REPLACEABLE = ("ops", "compute", "monitoring", "redcap")
ACCESS = "access"


@dataclass(frozen=True)
class Stacks:
    access: AccessStack
    network: NetworkStack
    shared: SharedStack
    data: DataStack
    ops: OpsStack
    compute: ComputeStack
    monitoring: MonitoringStack | None
    redcap: RedcapStack | None = None


def build_stacks(app: cdk.App, cfg: EnvironmentConfig, images: Images) -> Stacks:
    env = cdk.Environment(account=cfg.account_id, region=cfg.region)
    tags = {"jhe:environment": cfg.name, "jhe:managed-by": "jhe-aws-cdk"}

    def props(part: str, protected: bool) -> dict:
        return {
            "stack_name": cfg.stack_name(part),
            "env": env,
            "synthesizer": cdk.DefaultStackSynthesizer(qualifier=cfg.bootstrap_qualifier),
            "termination_protection": protected and cfg.termination_protection,
            "tags": tags,
        }

    access = AccessStack(app, cfg.stack_name(ACCESS), cfg, **props(ACCESS, False))
    network = NetworkStack(app, cfg.stack_name("network"), cfg, **props("network", True))
    shared = SharedStack(app, cfg.stack_name("shared"), cfg, network, **props("shared", True))
    data = DataStack(app, cfg.stack_name("data"), cfg, network, **props("data", True))
    ops = OpsStack(app, cfg.stack_name("ops"), cfg, images, network, shared, data, **props("ops", False))
    compute = ComputeStack(
        app,
        cfg.stack_name("compute"),
        cfg,
        images,
        network,
        shared,
        data,
        ops,
        **props("compute", False),
    )
    monitoring = (
        MonitoringStack(
            app,
            cfg.stack_name("monitoring"),
            cfg,
            shared,
            data,
            ops,
            compute,
            **props("monitoring", False),
        )
        if cfg.monitoring.enabled
        else None
    )
    redcap = (
        RedcapStack(app, cfg.stack_name("redcap"), cfg, data, **props("redcap", False))
        if cfg.redcap_enabled
        else None
    )
    return Stacks(access, network, shared, data, ops, compute, monitoring, redcap)
