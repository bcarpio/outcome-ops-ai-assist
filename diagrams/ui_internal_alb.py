# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
OutcomeOps AI Assist - Internal ALB Architecture
ui_alb_internal = true (default)

Enterprise customers access via their existing VPC connectivity:
- Direct Connect
- Transit Gateway
- Site-to-Site VPN
- Zscaler/ZTNA
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import ECS, Fargate
from diagrams.aws.network import ALB, VPC, TransitGateway, DirectConnect, VpnGateway
from diagrams.aws.security import IAM
from diagrams.aws.integration import Eventbridge
from diagrams.aws.compute import Lambda
from diagrams.onprem.network import Internet
from diagrams.onprem.client import Users

graph_attr = {
    "fontsize": "24",
    "bgcolor": "white",
    "pad": "0.5",
    "splines": "ortho",
}

with Diagram(
    "OutcomeOps AI Assist - Internal ALB",
    filename="ui_internal_alb",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    users = Users("Enterprise\nUsers")

    with Cluster("Corporate Network"):
        corp_access = [
            DirectConnect("Direct\nConnect"),
            TransitGateway("Transit\nGateway"),
            VpnGateway("Site-to-Site\nVPN"),
        ]

    with Cluster("Customer AWS Account"):
        with Cluster("VPC"):
            alb = ALB("Internal ALB\n(Port 80/443)")

            with Cluster("Private Subnets"):
                with Cluster("ECS Cluster"):
                    fargate = Fargate("Fargate Spot\nChat UI")

                lambda_fn = Lambda("Chat Streaming\nLambda")

    # Connections
    users >> Edge(label="Corporate\nNetwork") >> corp_access
    corp_access >> Edge(label="Private\nConnectivity") >> alb
    alb >> Edge(label="HTTP :3000") >> fargate
    fargate >> Edge(label="IAM Auth\nSigV4") >> lambda_fn
