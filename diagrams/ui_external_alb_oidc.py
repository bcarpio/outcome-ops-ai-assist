# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
OutcomeOps AI Assist - External ALB with Azure AD OIDC
ui_alb_internal = false

Public ALB secured by Azure AD authentication:
- Users access via public internet
- ALB redirects to Azure AD for authentication
- After auth, ALB forwards request with user identity headers
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import ECS, Fargate
from diagrams.aws.network import ALB, VPC, Route53, CF
from diagrams.aws.security import ACM, IAM
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.saas.identity import Auth0
from diagrams.onprem.client import Users
from diagrams.generic.blank import Blank

graph_attr = {
    "fontsize": "24",
    "bgcolor": "white",
    "pad": "0.5",
    "splines": "ortho",
}

with Diagram(
    "OutcomeOps AI Assist - Public ALB with Azure AD OIDC",
    filename="ui_external_alb_oidc",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    users = Users("Users")

    with Cluster("Azure AD"):
        azure_ad = Auth0("Azure AD\nOIDC Provider")

    with Cluster("AWS"):
        dns = Route53("Route53\noutcomeops.example.com")
        cert = ACM("ACM\nCertificate")

        with Cluster("VPC"):
            alb = ALB("Public ALB\nHTTPS :443")

            with Cluster("Private Subnets"):
                with Cluster("ECS Cluster"):
                    fargate = Fargate("Fargate Spot\nChat UI")

                lambda_fn = Lambda("Chat Streaming\nLambda")

                dynamo = Dynamodb("DynamoDB\nConversations")

    # Authentication flow
    users >> Edge(label="1. HTTPS") >> dns
    dns >> alb
    alb >> Edge(label="2. Redirect\nto login", style="dashed") >> azure_ad
    azure_ad >> Edge(label="3. Auth callback\nwith token", style="dashed") >> alb

    # After authentication
    alb >> Edge(label="4. Forward with\nx-amzn-oidc-* headers") >> fargate
    fargate >> Edge(label="IAM Auth") >> lambda_fn
    lambda_fn >> dynamo

    # TLS
    cert >> Edge(style="dotted") >> alb
