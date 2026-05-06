"""
OutcomeOps AI Assist - UI Internal Request Architecture

Shows what happens INSIDE the UI tier on each request: ALB OIDC header
injection, the Express SigV4 reverse proxy, and the split between the
chat-streaming Lambda (SSE) and the workspace-management Lambda (REST).

Companion to ui_internal_alb / ui_external_alb_oidc, which show how a
user's HTTPS request reaches the ALB. This diagram picks up at the ALB
and shows where the request goes from there.

Generates: ui_architecture.png
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import Fargate, Lambda
from diagrams.aws.network import ALB
from diagrams.aws.database import Dynamodb
from diagrams.aws.storage import S3
from diagrams.aws.ml import Sagemaker, Comprehend
from diagrams.aws.security import IAM
from diagrams.saas.identity import Auth0
from diagrams.onprem.client import Users

graph_attr = {
    "fontsize": "22",
    "bgcolor": "white",
    "pad": "0.6",
    "splines": "ortho",
    "nodesep": "0.6",
    "ranksep": "0.9",
}

with Diagram(
    "OutcomeOps AI Assist - UI Internal Request Architecture",
    filename="ui_architecture",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    user = Users("Browser\n(authenticated)")
    azure_ad = Auth0("Azure AD\n(OIDC)")

    with Cluster("Customer AWS Account"):
        with Cluster("VPC (Private Subnets)"):

            with Cluster("ALB Layer"):
                alb = ALB(
                    "ALB\n"
                    "1) OAuth handshake w/ Azure AD\n"
                    "2) Issues x-amzn-oidc-data JWT\n"
                    "(AWS-signed, not Microsoft-signed)"
                )

            with Cluster("ECS Fargate (UI Proxy)"):
                react_spa = Fargate(
                    "React SPA\n"
                    "(static, served from /dist)"
                )
                express = Fargate(
                    "Express server\n"
                    "Decodes x-amzn-oidc-data\n"
                    "SigV4-signs each /api/* call"
                )
                task_role = IAM(
                    "Fargate Task Role\n"
                    "lambda:InvokeFunctionUrl\n"
                    "ssm:GetParameter\n"
                    "kms:Decrypt"
                )

            with Cluster("Backend Lambdas (Function URLs, AWS_IAM auth)"):
                chat_lambda = Lambda(
                    "chat-streaming\n"
                    "(SSE, 900s timeout)\n"
                    "RAG + Bedrock streaming"
                )
                workspace_lambda = Lambda(
                    "workspace-management\n"
                    "(REST, LWA + FastAPI)\n"
                    "CRUD, audit, MCP, billing"
                )

            with Cluster("AWS services (via PrivateLink VPC endpoints)"):
                bedrock = Sagemaker(
                    "Bedrock\nClaude / Titan / Cohere"
                )
                comprehend = Comprehend(
                    "Comprehend\nDetect-PII\n(ingest-only)"
                )
                ddb = Dynamodb(
                    "DynamoDB\nworkspaces, audit,\nchat, analytics"
                )
                s3_vectors = S3(
                    "S3 Vectors\nRAG index"
                )
                s3_kb = S3(
                    "S3 Knowledge Base\nuploaded docs"
                )

    # User → ALB
    user >> Edge(label="HTTPS\n(corp DX/TGW)") >> alb
    alb >> Edge(
        label="OAuth redirect\n(token exchange + JWKS)",
        style="dashed",
    ) >> azure_ad

    # ALB → Fargate
    alb >> Edge(
        label="x-amzn-oidc-data\nx-amzn-oidc-identity"
    ) >> express
    alb >> Edge(label="static assets") >> react_spa

    # Express SigV4 → Lambda Function URLs
    task_role >> Edge(style="dotted", label="assume role") >> express
    express >> Edge(
        label="SigV4 POST /chat\n(SSE pass-through)"
    ) >> chat_lambda
    express >> Edge(
        label="SigV4 GET/POST/PUT/DELETE\n/api/*"
    ) >> workspace_lambda

    # Lambdas → AWS services
    chat_lambda >> bedrock
    chat_lambda >> s3_vectors
    chat_lambda >> ddb
    workspace_lambda >> ddb
    workspace_lambda >> s3_kb
    workspace_lambda >> Edge(
        label="audit emit",
        style="dashed",
    ) >> ddb

    # Comprehend is invoked by ingest Lambdas (process-documents,
    # process-batch-summary, process-repo-summary), shown here as a
    # platform service for completeness even though it's not on the
    # chat or workspace-management hot path.
    comprehend >> Edge(
        label="invoked by ingest path\n(see data_flow diagram)",
        style="dotted",
    ) >> s3_kb
