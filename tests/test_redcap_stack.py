"""The REDCap integration is opt-in: with `redcap_integration` unset nothing about it exists (no stack,
no secret, no CLI behavior); set, one Lambda behind a function URL with a role that reaches only its own log
group and secret."""

from __future__ import annotations

import json

import pytest
from aws_cdk import assertions
from pydantic import ValidationError

from jhe_aws.cli import main as cli
from jhe_aws.config import EnvironmentConfig
from jhe_aws.stacks import REPLACEABLE

from .conftest import sample_config
from .test_synth import nag_errors, synth

REDCAP = {
    "redcap_api_url": "https://redcap.example.org/api/",
    "email_field": "email",
    "link_field": "jhe_link",
    "organization_id": 1,
}


@pytest.fixture(scope="module")
def enabled():
    return synth(sample_config(redcap_integration=REDCAP))


def test_off_by_default_and_in_the_committed_example():
    cfg = sample_config()
    assert cfg.redcap_integration is None and cfg.redcap_enabled is False
    example = json.loads((cli.ROOT / "config" / "example.json").read_text())
    assert example["redcap_integration"] is None


def test_nothing_is_deployed_without_it():
    app, stacks = synth(sample_config())
    assert stacks.redcap is None
    assert app.synth().try_get_artifact("jhe-test-redcap") is None
    cfg = sample_config()
    assert cli.stack_names(cfg, REPLACEABLE) == ["jhe-test-ops", "jhe-test-compute", "jhe-test-monitoring"]
    assert cli.secret_names(cfg) == ("service", "mcp")


def test_the_stack_is_replaceable_and_listed_when_set(enabled):
    _, stacks = enabled
    cfg = sample_config(redcap_integration=REDCAP)
    assert stacks.redcap is not None and stacks.redcap.stack_name == "jhe-test-redcap"
    assert stacks.redcap.termination_protection is False
    assert "jhe-test-redcap" in cli.stack_names(cfg, REPLACEABLE)
    assert cli.secret_names(cfg) == ("service", "mcp", "redcap")


def test_no_nag_errors(enabled):
    _, stacks = enabled
    assert nag_errors(stacks.redcap) == []


def test_one_function_with_its_settings_and_a_public_url_guarded_by_the_token(enabled):
    _, stacks = enabled
    template = assertions.Template.from_stack(stacks.redcap)
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.13",
            "Architectures": ["arm64"],
            "Handler": "handler.handler",
            "Timeout": 900,
            "Environment": {
                "Variables": {
                    "SECRET_ID": "jhe/test/redcap",
                    "JHE_BASE_URL": "https://jhe.example.org",
                    "REDCAP_API_URL": "https://redcap.example.org/api/",
                    "EMAIL_FIELD": "email",
                    "LINK_FIELD": "jhe_link",
                    "RECORD_ID_FIELD": "record_id",
                    "ORGANIZATION_ID": "1",
                    "JHE_CLIENT_NAME": "USCDI Patient Access",
                }
            },
        },
    )
    template.has_resource_properties("AWS::Lambda::Url", {"AuthType": "NONE"})
    template.has_resource_properties("AWS::Logs::LogGroup", {"LogGroupName": "/jhe/test/redcap"})
    # no VPC, no secret value in the template
    assert "VpcConfig" not in json.dumps(template.to_json())
    assert "REDCAP_API_TOKEN" not in json.dumps(template.to_json())


def test_the_role_reaches_only_its_logs_and_its_secret(enabled):
    _, stacks = enabled
    template = assertions.Template.from_stack(stacks.redcap)
    template.resource_count_is("AWS::IAM::ManagedPolicy", 0)  # no AWS-managed or shared policy
    policy = next(iter(template.find_resources("AWS::IAM::Policy").values()))["Properties"]["PolicyDocument"]
    actions = sorted(a for st in policy["Statement"] for a in st["Action"] if isinstance(st["Action"], list))
    assert set(actions) <= {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
        "kms:Decrypt",
        "kms:DescribeKey",
    }
    secret_statement = next(
        st for st in policy["Statement"] if "secretsmanager:GetSecretValue" in st["Action"]
    )
    assert "jhe/test/redcap" in json.dumps(secret_statement["Resource"])


def test_config_is_strict():
    with pytest.raises(ValidationError):
        sample_config(redcap_integration={**REDCAP, "redcap_api_url": "http://redcap.example.org/api/"})
    with pytest.raises(ValidationError):
        sample_config(redcap_integration={k: v for k, v in REDCAP.items() if k != "link_field"})
    with pytest.raises(ValidationError):
        sample_config(redcap_integration={**REDCAP, "unknown_key": 1})
    assert isinstance(sample_config(redcap_integration=REDCAP), EnvironmentConfig)
