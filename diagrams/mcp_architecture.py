"""
OutcomeOps AI Assist - MCP Server Architecture
ARCH-006: MCP gateway showing authentication, tool proxy, and downstream server topology.

Shows how MCP clients authenticate via API keys, how the ALB routes MCP traffic
(bypassing OIDC), and how the OutcomeOps MCP server proxies tools to downstream
MCP servers (SonarQube, Snyk, user-added).

Generates: mcp_architecture.png
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import Fargate, ECR
from diagrams.aws.network import ALB, CloudMap
from diagrams.aws.database import Dynamodb
from diagrams.aws.storage import S3
from diagrams.aws.security import KMS
from diagrams.aws.management import SSM
from diagrams.aws.ml import Sagemaker
from diagrams.onprem.client import Users
from diagrams.saas.identity import Auth0

graph_attr = {
    "fontsize": "24",
    "bgcolor": "white",
    "pad": "0.8",
    "splines": "ortho",
    "nodesep": "0.8",
    "ranksep": "1.2",
}

with Diagram(
    "OutcomeOps AI Assist - MCP Server Architecture",
    filename="mcp_architecture",
    outformat="png",
    show=False,
    direction="TB",
    graph_attr=graph_attr,
):
    # External actors
    with Cluster("MCP Clients"):
        claude_code = Users("Claude Code\nVS Code\nCursor")

    with Cluster("Browser Users"):
        browser = Users("Browser\n(UI)")

    azure_ad = Auth0("Azure AD\n(OIDC)")

    with Cluster("Customer AWS Account"):

        alb = ALB("ALB\n(HTTPS, TLS 1.3)")

        # --- MCP Servers (ECS) ---
        with Cluster("ECS Cluster (Fargate Spot, Private Subnets)"):

            with Cluster("SG: port 8080 from ALB SG only"):
                mcp_outcomeops = Fargate("OutcomeOps\nMCP Server\n0.5 vCPU, 1GB")

            with Cluster("SG: port 8080 from ALB SG only "):
                mcp_sonarqube = Fargate("SonarQube\nMCP Server\n0.5 vCPU, 1GB")

            with Cluster("SG: port 8080 from ALB SG only  "):
                mcp_snyk = Fargate("Snyk\nMCP Server\n0.5 vCPU, 1GB")

            with Cluster("SG: port 3000 from ALB SG only"):
                ui_fargate = Fargate("Chat UI\nProxy\n0.25 vCPU")

        # --- Service Discovery ---
        cloudmap = CloudMap("Cloud Map\n{env}-mcp.local\n(DNS discovery)")

        # --- Data Stores ---
        with Cluster("DynamoDB (Workspaces Table)"):
            ddb_keys = Dynamodb("API Keys\nUSER#{email}\nAPI_KEY#{hash}")
            ddb_mcp = Dynamodb("MCP Servers\nMCP#{id}\nMETADATA / TOOLS")
            ddb_ws_mcp = Dynamodb("WS Assignments\nWS#{id}\nMCP_SERVER#{id}")

        with Cluster("Knowledge Base"):
            s3_vectors = S3("S3 Vectors\n(1024-dim)")
            bedrock = Sagemaker("Bedrock\nTitan Embeddings")

        # --- Security ---
        with Cluster("Secrets & Encryption"):
            ssm = SSM("SSM Parameters\nsonarqube-token\nsnyk-token")
            kms = KMS("KMS\n(OAuth/credential\nencryption)")

        ecr = ECR("ECR\nmcp-server\nmcp-snyk")

    # === Connections ===

    # MCP client flow (NO OIDC)
    claude_code >> Edge(label="HTTPS\nBearer ooa_xxx") >> alb
    alb >> Edge(label="/mcp/v1*\nNO OIDC\n(priority 3)") >> mcp_outcomeops
    alb >> Edge(label="/mcp/snyk*\nNO OIDC\n(priority 6)") >> mcp_snyk
    alb >> Edge(label="/mcp*\nNO OIDC\n(priority 7)") >> mcp_sonarqube

    # Browser flow (OIDC)
    browser >> Edge(label="HTTPS") >> alb
    alb >> Edge(label="/*\nOIDC auth", style="dashed") >> azure_ad
    alb >> Edge(label="/* (default)") >> ui_fargate

    # MCP auth: API key validation
    mcp_outcomeops >> Edge(label="Validate\nAPI key hash") >> ddb_keys

    # MCP tool discovery
    mcp_outcomeops >> Edge(label="Discover\nservers") >> ddb_mcp
    mcp_outcomeops >> Edge(label="WS->MCP\nassignments") >> ddb_ws_mcp

    # MCP proxy to downstream servers (via Cloud Map DNS)
    mcp_outcomeops >> Edge(label="Proxy tool calls\n(JSON-RPC)") >> mcp_sonarqube
    mcp_outcomeops >> Edge(label="Proxy tool calls\n(JSON-RPC)") >> mcp_snyk
    mcp_outcomeops >> Edge(style="dotted", label="DNS resolve") >> cloudmap

    # Native tools: KB search
    mcp_outcomeops >> Edge(label="KB search") >> s3_vectors
    mcp_outcomeops >> Edge(label="Embeddings") >> bedrock

    # Credentials for downstream servers
    mcp_sonarqube >> Edge(style="dotted") >> ssm
    mcp_snyk >> Edge(style="dotted") >> ssm
    mcp_outcomeops >> Edge(style="dotted", label="Decrypt\ncredentials") >> kms

    # Container images
    ecr >> Edge(style="dotted", label="Pull image") >> mcp_outcomeops
    ecr >> Edge(style="dotted") >> mcp_snyk
