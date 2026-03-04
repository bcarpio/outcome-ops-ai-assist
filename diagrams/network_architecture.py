"""
OutcomeOps AI Assist - Network Architecture
ARCH-005: Network topology with VPC, subnets, security groups, and connectivity.

Shows the enterprise deployment model (private subnets, TGW/DX for outbound).
Public subnet mode (assign_public_ip=true) is also supported for dev/demo.

Generates: network_architecture.png
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import Fargate, Lambda
from diagrams.aws.network import (
    ALB,
    NATGateway,
    InternetGateway,
    Route53,
    TransitGateway,
    DirectConnect,
)
from diagrams.aws.network import APIGateway as Apigw
from diagrams.aws.security import ACM
from diagrams.aws.ml import Sagemaker
from diagrams.aws.storage import S3
from diagrams.aws.database import Dynamodb
from diagrams.saas.identity import Auth0
from diagrams.onprem.client import Users
from diagrams.onprem.vcs import Github

graph_attr = {
    "fontsize": "24",
    "bgcolor": "white",
    "pad": "0.8",
    "splines": "ortho",
    "nodesep": "0.8",
    "ranksep": "1.0",
}

with Diagram(
    "OutcomeOps AI Assist - Network Architecture",
    filename="network_architecture",
    outformat="png",
    show=False,
    direction="TB",
    graph_attr=graph_attr,
):
    # External
    users = Users("Users")
    github_ext = Github("GitHub API\n(External)")
    azure_ad = Auth0("Azure AD\n(External)")

    # DNS
    dns = Route53("Route 53\noutcomeops.example.com")

    with Cluster("Customer AWS Account"):

        # API Gateway (outside VPC)
        apigw = Apigw("API Gateway v2\nGitHub Webhooks\n(Public Endpoint)")

        # AWS Services (outside VPC)
        with Cluster("AWS Managed Services (Regional Endpoints)"):
            bedrock = Sagemaker("Amazon Bedrock")
            s3 = S3("S3 / S3 Vectors")
            dynamodb = Dynamodb("DynamoDB")

        with Cluster("VPC"):

            with Cluster("Subnets (Multi-AZ)"):
                alb = ALB("ALB\nSG: 80/443 inbound\n(Internal or Public)")

                with Cluster("SG: Fargate (port 3000 from ALB SG only)"):
                    fargate = Fargate("ECS Fargate\nChat UI Proxy\nRead-only FS")

                with Cluster("SG: Lambda (outbound 443 only)"):
                    lambda_chat = Lambda("chat-streaming")
                    lambda_other = Lambda("35+ Lambda\nFunctions")

        # Outbound options
        with Cluster("Outbound Path (choose one)"):
            tgw = TransitGateway("Transit Gateway\n(Enterprise)")
            dx = DirectConnect("Direct Connect\n/ NAT / IGW")

        cert = ACM("ACM\nDNS-validated\nTLS Certificate")

    # === Network Flows ===

    # Inbound user traffic
    users >> Edge(label="HTTPS :443") >> dns
    dns >> Edge(label="DNS Alias") >> alb
    cert >> Edge(style="dotted", label="TLS termination") >> alb

    # OIDC Authentication (public ALB only)
    alb >> Edge(label="OIDC redirect\n(public ALB only)", style="dashed") >> azure_ad
    azure_ad >> Edge(label="Auth callback", style="dashed") >> alb

    # ALB to Fargate
    alb >> Edge(label="Port 3000\n(SG rule)") >> fargate

    # Fargate to Lambda
    fargate >> Edge(label="SigV4\nIAM Auth") >> lambda_chat

    # Outbound via TGW/DX/NAT/IGW
    lambda_other >> Edge(label="Outbound\n:443") >> tgw
    tgw >> dx
    dx >> Edge(label="GitHub API\nHTTPS") >> github_ext

    # Lambda to AWS services (via AWS endpoints)
    lambda_chat >> Edge(label="AWS SDK\nHTTPS") >> bedrock
    lambda_other >> Edge(label="AWS SDK") >> s3
    lambda_other >> Edge(label="AWS SDK") >> dynamodb

    # GitHub webhooks
    github_ext >> Edge(label="Webhooks\nHMAC-SHA256") >> apigw
    apigw >> lambda_other
