"""Self-heal the SonarQube MCP Fargate service on I/O-reactor death.

The third-party SonarQube MCP Java server shuts down its Apache HttpClient async
I/O reactor during idle (`IOReactorShutdownException`) and never recreates it, so
every SonarCloud call fast-fails. The container stays RUNNING and the ALB `/`
health check returns 200, so neither ECS nor the ALB detects it -- it just runs
broken until a human restarts it. This is a workaround, not a fix (report
upstream to SonarSource); see the ADR/ops notes.

Trigger: a CloudWatch Logs subscription filter on the sonar MCP log group
matching `IOReactorShutdownException` invokes this Lambda. The invocation itself
is the signal; the log payload isn't parsed. We force a new ECS deployment
(replaces the task with a fresh one), debounced so the burst of error lines a
single death produces doesn't cause a restart storm.
"""

import datetime
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ecs = boto3.client("ecs")

CLUSTER = os.environ["CLUSTER"]
SERVICE = os.environ["SERVICE"]
# Don't restart if the running deployment is younger than this -- covers both
# "a restart is already in flight" and "we just restarted it seconds ago".
DEBOUNCE_SECONDS = int(os.environ.get("DEBOUNCE_SECONDS", "600"))


def handler(event, context):
    resp = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])
    services = resp.get("services", [])
    if not services:
        logger.error(f"[sonar-selfheal] service {SERVICE} not found in {CLUSTER}")
        return {"restarted": False, "reason": "service_not_found"}

    primary = next(
        (d for d in services[0].get("deployments", []) if d.get("status") == "PRIMARY"),
        None,
    )

    if primary and primary.get("rolloutState") == "IN_PROGRESS":
        logger.info("[sonar-selfheal] a deployment is already IN_PROGRESS; skipping")
        return {"restarted": False, "reason": "rollout_in_progress"}

    if primary and primary.get("createdAt"):
        age = (
            datetime.datetime.now(datetime.timezone.utc) - primary["createdAt"]
        ).total_seconds()
        if age < DEBOUNCE_SECONDS:
            logger.info(
                f"[sonar-selfheal] last deployment {int(age)}s ago "
                f"(< {DEBOUNCE_SECONDS}s debounce); skipping restart"
            )
            return {"restarted": False, "reason": "debounced", "age_seconds": int(age)}

    ecs.update_service(cluster=CLUSTER, service=SERVICE, forceNewDeployment=True)
    logger.warning(
        f"[sonar-selfheal] IOReactorShutdown detected -> forced new deployment "
        f"of {SERVICE} in {CLUSTER}"
    )
    return {"restarted": True}
