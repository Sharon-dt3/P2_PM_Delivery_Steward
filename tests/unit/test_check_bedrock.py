"""scripts/check_bedrock.py: tells a missing Marketplace subscription from an IAM refusal, never prints a credential."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest


@pytest.fixture()
def script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_bedrock.py"
    spec = importlib.util.spec_from_file_location("check_bedrock_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MARKETPLACE = ("An error occurred (AccessDeniedException) when calling the InvokeModel operation: Model access is denied due to IAM user "
               "or service role is not authorized to perform the required AWS Marketplace actions (aws-marketplace:ViewSubscriptions, "
               "aws-marketplace:Subscribe)")
IAM = ("An error occurred (AccessDeniedException) when calling the InvokeModel operation: User: arn:aws:iam::1:user/u is not authorized "
       "to perform: bedrock:InvokeModel on resource: arn:aws:bedrock:us-east-2:1:inference-profile/x because no identity-based policy allows")


class Client:
    def __init__(self, outcomes):
        self.outcomes = outcomes

    def invoke_model(self, modelId, body):
        outcome = self.outcomes[modelId]
        if isinstance(outcome, Exception):
            raise outcome
        return {"body": io.BytesIO(json.dumps({"content": [{"text": outcome}]}).encode())}


def test_it_tells_the_marketplace_error_from_an_iam_refusal(script):
    assert script.classify(MARKETPLACE) == script.MARKETPLACE
    assert script.classify(IAM) == script.IAM
    assert script.classify("ThrottlingException: slow down") == script.OTHER


def test_a_working_model_is_reported_and_the_run_succeeds(script, capsys):
    client = Client({m: Exception(IAM) for m in script.MODELS.values()})
    client.outcomes[script.MODELS["Claude Sonnet 4"]] = "ok"

    assert script.check(client) is True

    out = capsys.readouterr().out
    assert "WORKS" in out and "IAM" in out


def test_nothing_working_says_whom_to_ask(script, capsys):
    client = Client({m: Exception(MARKETPLACE) for m in script.MODELS.values()})

    assert script.check(client) is False

    out = capsys.readouterr().out
    assert "MARKETPLACE" in out and "aws-marketplace:Subscribe" in out and "admin" in out


def test_a_credential_in_an_error_is_never_echoed_as_a_secret(script, capsys):
    """The output is AWS's one-line message only; nothing reads or prints a key."""
    source = Path(script.__file__).read_text()

    assert "AWS_SECRET_ACCESS_KEY=..." in source  # the hint shows a placeholder only
    assert "aws_secret" not in source.lower().replace("aws_secret_access_key=...", "")
