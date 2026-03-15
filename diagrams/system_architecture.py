"""
OutcomeOps AI Assist - Full System Architecture
ARCH-001: Complete system architecture showing all AWS services, integrations, and data flows.

Generates: system_architecture.png
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import Lambda, Fargate, ECR
from diagrams.aws.network import ALB, Route53, APIGateway
from diagrams.aws.database import Dynamodb
from diagrams.aws.storage import S3
from diagrams.aws.integration import SQS, Eventbridge
from diagrams.aws.ml import Sagemaker
from diagrams.aws.security import KMS, ACM
from diagrams.aws.management import SSM, Cloudwatch
from diagrams.saas.identity import Auth0
from diagrams.onprem.client import Users
from diagrams.onprem.vcs import Github
from diagrams.custom import Custom

graph_attr = {
    "fontsize": "24",
    "bgcolor": "white",
    "pad": "0.8",
    "splines": "ortho",
    "nodesep": "0.8",
    "ranksep": "1.2",
}

with Diagram(
    "OutcomeOps AI Assist - System Architecture",
    filename="system_architecture",
    outformat="png",
    show=False,
    direction="TB",
    graph_attr=graph_attr,
):
    # External actors
    users = Users("Users")
    github = Github("GitHub")

    with Cluster("External Identity"):
        azure_ad = Auth0("Azure AD\n(OIDC)")

    with Cluster("Customer AWS Account"):

        # DNS & Certificates
        dns = Route53("Route 53")
        cert = ACM("ACM\nCertificate")

        with Cluster("Networking"):
            alb = ALB("ALB\n(Internal/Public)")

        with Cluster("Compute - ECS"):
            fargate = Fargate("Fargate Spot\nChat UI Proxy")

        # --- Lambda Functions ---
        with Cluster("Core Lambdas"):
            chat = Lambda("chat-streaming\n(1GB, 900s)")
            query_kb = Lambda("query-kb\n(256MB)")
            ask_claude = Lambda("ask-claude\n(256MB)")

        with Cluster("Code Generation Lambdas"):
            generate_code = Lambda("generate-code\n(512MB)")
            analyze_pr = Lambda("analyze-pr\n(256MB)")
            process_pr = Lambda("process-pr-check\n(256MB)")
            run_tests = Lambda("run-tests\n(1GB)")

        with Cluster("Ingestion Lambdas"):
            github_sync = Lambda("github-sync\n(512MB)")
            process_repo = Lambda("process-repo-summary\n(512MB)")
            process_batch = Lambda("process-batch-summary\n(512MB)")
            process_docs = Lambda("process-documents\n(512MB)")

        with Cluster("Integration Lambdas"):
            confluence = Lambda("confluence-sync")
            jira = Lambda("jira-sync")
            teams = Lambda("teams-sync")
            sharepoint = Lambda("sharepoint-sync")
            outlook = Lambda("outlook-sync")

        with Cluster("Workspace Lambdas"):
            workspace_mgmt = Lambda("workspace-mgmt\n(256MB)")
            file_ingestion = Lambda("file-ingestion\n(512MB)")
            audit_alert = Lambda("audit-alert\n(256MB)")

        # --- Queues ---
        with Cluster("SQS FIFO Queues"):
            pr_queue = SQS("pr-checks\nqueue.fifo")
            code_gen_queue = SQS("code-generation\nqueue.fifo")
            repo_queue = SQS("repo-summaries\nqueue.fifo")
            code_maps_queue = SQS("code-maps\nqueue")

        # --- API Gateway ---
        apigw = APIGateway("API Gateway v2\n(GitHub Webhooks)")

        # --- Event Bus ---
        eventbus = Eventbridge("EventBridge\nCustom Bus")

        # --- Storage ---
        with Cluster("Data Storage"):
            s3_kb = S3("S3\nKnowledge Base")
            s3_vectors = S3("S3 Vectors\n(1024-dim, cosine)")

        with Cluster("DynamoDB Tables"):
            ddb_chat = Dynamodb("chat")
            ddb_codemaps = Dynamodb("code-maps")
            ddb_audit = Dynamodb("audit-logs")
            ddb_workspaces = Dynamodb("workspaces")

        # --- AI/ML ---
        with Cluster("Amazon Bedrock"):
            bedrock_claude = Sagemaker("Claude Sonnet/Haiku")
            bedrock_titan = Sagemaker("Titan Embeddings v2")
            bedrock_rerank = Sagemaker("Cohere Rerank 3.5")

        # --- Security ---
        with Cluster("Security & Config"):
            kms = KMS("KMS")
            ssm = SSM("SSM\nParameter Store")
            ecr = ECR("ECR\nRepositories")

        # --- Monitoring ---
        cw = Cloudwatch("CloudWatch\nLogs & Metrics")

    # === Connections ===

    # User flow
    users >> Edge(label="HTTPS") >> dns
    dns >> alb
    cert >> Edge(style="dotted") >> alb
    alb >> Edge(label="OIDC Auth", style="dashed") >> azure_ad
    alb >> Edge(label=":3000") >> fargate
    fargate >> Edge(label="SigV4") >> chat
    fargate >> Edge(label="SigV4") >> workspace_mgmt

    # Chat flow
    chat >> bedrock_claude
    chat >> bedrock_titan
    chat >> bedrock_rerank
    chat >> s3_vectors
    chat >> ddb_chat
    chat >> ddb_audit

    # Query flow
    query_kb >> bedrock_titan
    query_kb >> bedrock_rerank
    query_kb >> s3_vectors
    ask_claude >> bedrock_claude

    # GitHub webhook flow
    github >> Edge(label="Webhooks") >> apigw
    apigw >> analyze_pr
    analyze_pr >> pr_queue
    pr_queue >> process_pr
    process_pr >> github

    # Code generation flow
    github >> Edge(label="App Events") >> github_sync
    github_sync >> repo_queue
    repo_queue >> process_repo
    process_repo >> code_maps_queue
    code_maps_queue >> process_batch
    process_batch >> s3_kb
    generate_code >> bedrock_claude
    generate_code >> code_gen_queue

    # Document processing
    s3_kb >> Edge(label="S3 Trigger") >> process_docs
    process_docs >> bedrock_titan
    process_docs >> s3_vectors

    # Event-driven automation
    generate_code >> eventbus
    eventbus >> run_tests

    # Integration flows
    confluence >> s3_kb
    jira >> s3_kb
    teams >> s3_kb
    sharepoint >> s3_kb
    outlook >> s3_kb
    file_ingestion >> s3_kb

    # Audit
    ddb_audit >> Edge(label="Streams") >> audit_alert

    # Storage & state
    process_repo >> ddb_codemaps
    workspace_mgmt >> ddb_workspaces

    # Security
    ssm >> Edge(style="dotted") >> chat
    kms >> Edge(style="dotted") >> cw
