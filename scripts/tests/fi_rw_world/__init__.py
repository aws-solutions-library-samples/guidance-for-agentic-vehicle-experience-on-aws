"""A shared fake of AWS for the FI recall/warranty 5.5 live-check script's tests.

Every response comes from the real code the script talks to, run in-process:

- DynamoDB, S3, Lambda configuration, Scheduler, CloudWatch alarms, SNS, CloudWatch
  Logs, SSM, CloudFormation and IAM: moto (real botocore request/response shapes and
  real DynamoDB semantics such as Limit-before-FilterExpression).
- The AVX REST API (findings, chat) and the DMS REST API: a local HTTP server that
  runs the real Lambda handlers behind an API Gateway emulation (Cognito authorizer
  claims as REST API Gateway passes them; unknown paths answer 403).
- The health-agent Lambda: the real ``agents.tier2.runner.lambda_handler`` with the
  function's live-shaped environment, reading Athena views that DuckDB serves.
- The DMS stand-in Lambdas: the real ``source.simulation.fi_stand_ins`` handlers.
- DMS -> AVX events: the real DMS publisher's PutEvents entries delivered to the real
  AVX consumer when they match the live rule's pattern.

What is not real code, and why, is listed in ``SOURCES.md`` next to this file.
"""
from scripts.tests.fi_rw_world.world import World

__all__ = ["World"]
