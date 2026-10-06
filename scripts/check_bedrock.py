#!/usr/bin/env python3
"""Can this machine call Claude on AWS Bedrock yet? Tries each model with a tiny call and says why not.

Uses the AWS login you already configured (`aws configure`), makes a 20-token call per model,
and never prints a credential. Classifies a failure so you know whom to ask:

  marketplace   the account is not subscribed: needs aws-marketplace:ViewSubscriptions / :Subscribe
                (or an admin enabling the model once). Not something your own settings can fix.
  iam           your user's policy does not allow InvokeModel on that model/profile.
  other         anything else; the first line of AWS's message is shown.

  uv run python scripts/check_bedrock.py            # one check
  uv run python scripts/check_bedrock.py --watch    # recheck every 60 s until a model works

When one works, the P2 settings to switch over are printed (the keys themselves go into .env by hand).
"""

from __future__ import annotations

import argparse
import json
import sys
import time

REGION = "us-east-2"
ACCOUNT = "619042036275"
MODELS = {
    "Claude Sonnet 4": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "Claude Sonnet 4.6": "global.anthropic.claude-sonnet-4-6",
}
BODY = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 20,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}]}

WORKS, MARKETPLACE, IAM, OTHER = "works", "marketplace", "iam", "other"


def classify(message: str) -> str:
    """Why a call failed, from AWS's own error text."""
    lowered = message.lower()
    if "aws-marketplace" in lowered or "marketplace subscription" in lowered:
        return MARKETPLACE
    if "not authorized to perform: bedrock:invokemodel" in lowered or "no identity-based policy allows" in lowered:
        return IAM
    return OTHER


ADVICE = {
    MARKETPLACE: "the account is not subscribed to this model: ask an AWS admin for aws-marketplace:ViewSubscriptions and "
                 "aws-marketplace:Subscribe (Resource *), or to enable the model once in the Bedrock console",
    IAM: "your user's policy does not allow InvokeModel on this model: ask an AWS admin to allow it",
    OTHER: "unexpected error: see the message",
}


def try_model(client, model_id: str) -> tuple[str, str]:
    try:
        response = client.invoke_model(modelId=model_id, body=json.dumps(BODY))
        text = json.loads(response["body"].read()).get("content", [{}])[0].get("text", "")
        return WORKS, text.strip()[:40]
    except Exception as exc:  # noqa: BLE001 - the point is to report what AWS said
        message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return classify(message), message[:200]


def check(client) -> bool:
    any_works = False
    for name, model_id in MODELS.items():
        status, detail = try_model(client, model_id)
        any_works |= status == WORKS
        print(f"  {name:<18} {status.upper():<12} " + (f'replied "{detail}"' if status == WORKS else ADVICE[status]))
        if status == OTHER:
            print(f"      {detail}")
    return any_works


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--watch", action="store_true", help="recheck every 60 seconds until a model works")
    args = parser.parse_args(argv)

    import boto3

    client = boto3.client("bedrock-runtime", region_name=REGION)
    while True:
        print(time.strftime("%H:%M:%S"), f"checking Bedrock ({REGION})")
        if check(client):
            print("\nA model works. To switch P2 over, set in .env (keys by hand, never pasted into chat):")
            print("  LLM_PROVIDER=bedrock")
            print(f"  AWS_REGION={REGION}")
            print(f"  BEDROCK_MODEL_ID=arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/<the working model id above>")
            print("  AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...")
            return 0
        if not args.watch:
            return 1
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
