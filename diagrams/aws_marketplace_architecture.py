"""
OutcomeOps AI Assist - AWS Marketplace Architecture Diagram

Built specifically to satisfy AWS Marketplace SaaS architecture-review
guidelines (https://docs.aws.amazon.com/marketplace/latest/userguide/saas-guidelines.html#saas-architecture).

Required by AWS Marketplace:
  - Group and label components as Control Plane vs Application Plane
  - Place components where they LOGICALLY run
    (seller's AWS account, buyer's AWS account, or external)
  - High-level service groupings are acceptable

Architecture model: BYOC (Bring Your Own Cloud) SaaS.
  - Application plane runs ENTIRELY in the buyer's AWS account.
    Buyer owns Lambdas, S3, DynamoDB, Bedrock invocations, etc.
  - Control plane runs in the seller's (OutcomeOps) AWS account.
    Handles license issuance, validation phone-home, usage metering,
    AWS Marketplace integration (BatchMeterUsage), customer/tier
    management.

Both planes run on AWS, qualifying for the "deployed on AWS" badge.

Generates: aws_marketplace_architecture.png
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import Lambda, Fargate
from diagrams.aws.network import ALB, Route53, APIGateway
from diagrams.aws.database import Dynamodb
from diagrams.aws.storage import S3
from diagrams.aws.integration import SQS, Eventbridge
from diagrams.aws.ml import Sagemaker, Comprehend
from diagrams.aws.security import KMS
from diagrams.aws.management import SSM, Cloudwatch
from diagrams.aws.business import AlexaForBusiness  # AWS Marketplace icon proxy
from diagrams.generic.blank import Blank

graph_attr = {
    "fontsize": "20",
    "bgcolor": "white",
    "pad": "1.0",
    "splines": "ortho",
    "nodesep": "1.0",
    "ranksep": "1.4",
    "labelloc": "t",
}

cluster_control_plane = {
    "bgcolor": "#FFF4E6",
    "pencolor": "#D97706",
    "fontsize": "18",
    "style": "rounded",
}

cluster_application_plane = {
    "bgcolor": "#E0F2FE",
    "pencolor": "#0369A1",
    "fontsize": "18",
    "style": "rounded",
}

cluster_note = {
    "bgcolor": "#FFFBEB",
    "pencolor": "#92400E",
    "fontsize": "12",
    "style": "rounded,dashed",
}

cluster_seller_account = {
    "bgcolor": "#FFFBEB",
    "pencolor": "#92400E",
    "fontsize": "16",
    "style": "rounded",
}

cluster_buyer_account = {
    "bgcolor": "#EFF6FF",
    "pencolor": "#1E3A8A",
    "fontsize": "16",
    "style": "rounded",
}

with Diagram(
    "OutcomeOps AI Assist - AWS Marketplace Architecture",
    filename="aws_marketplace_architecture",
    outformat="png",
    show=False,
    direction="TB",
    graph_attr=graph_attr,
):

    # =========================================================================
    # Note: customer integrations are data sources, not runtime dependencies
    # =========================================================================
    with Cluster(
        "Optional customer integrations (GitHub, Atlassian, Microsoft 365) connect to the\n"
        "application plane via standard OAuth/webhook patterns from customer-owned\n"
        "third-party systems. These are data sources, not runtime dependencies.",
        graph_attr=cluster_note,
    ):
        Blank("")

    # =========================================================================
    # SELLER's AWS Account - Control Plane
    # =========================================================================
    with Cluster("Seller's AWS Account (OutcomeOps)", graph_attr=cluster_seller_account):

        with Cluster("Control Plane", graph_attr=cluster_control_plane):

            # License lifecycle: issue, validate (phone-home), revoke, expire
            license_api = Lambda("License Server API\n(issue / validate /\nrevoke)")
            license_db = Dynamodb("Customer & License DB\n(tier, entitlements,\nrevocation table)")

            # Usage metering aggregator: receives track_* calls from
            # buyer-account Lambdas, deduplicates, rolls up
            usage_api = Lambda("Usage Aggregator\n(track_repo_active,\ntrack_pr_generated, etc.)")
            usage_db = Dynamodb("Usage Metrics DB")

            # AWS Marketplace integration - calls BatchMeterUsage and
            # GetEntitlements from the SELLER's account, not the buyer's
            marketplace_lambda = Lambda("Marketplace Integration\n(BatchMeterUsage,\nGetEntitlements,\nMarketplace Events)")
            marketplace_api = AlexaForBusiness("AWS Marketplace\nMetering & Entitlement\nAPIs")

            # License validator layer - published as a Lambda layer ARN
            # that buyer-account Lambdas attach to
            license_layer_pub = Lambda("License Validator Layer\n(published ARN -\nattached by buyer Lambdas)")

            # Admin / expiration alerting
            admin_api = Lambda("Admin API\n+ Expiration Alerting")

    # =========================================================================
    # BUYER's AWS Account - Application Plane
    # =========================================================================
    with Cluster("Buyer's AWS Account (deployed via Terraform)",
                 graph_attr=cluster_buyer_account):

        with Cluster("Application Plane", graph_attr=cluster_application_plane):

            # Edge / ingress
            with Cluster("Edge & UI"):
                dns = Route53("Route 53")
                alb = ALB("ALB\n(Azure AD OIDC)")
                ui = Fargate("Chat UI\n(Fargate Spot)")
                api_gw = APIGateway("API Gateway\n(GitHub webhooks)")

            # Core SaaS business logic - 12 license-gated Lambdas grouped by function
            with Cluster("Chat & Query"):
                chat_svc = Lambda("Chat Streaming\n+ Conversation API")
                query_svc = Lambda("Knowledge Base\nQuery + Rerank")

            with Cluster("AI Code Generation"):
                codegen_svc = Lambda("Code Generation\n(generate-code,\nhandle-command,\nrun-tests, analyze-pr)")

            with Cluster("Knowledge Base Ingestion"):
                ingest_svc = Lambda("Source Sync\n(github, confluence,\njira, sharepoint,\noutlook, teams)")
                process_svc = Lambda("Document &\nCode-Map Processing\n(file-ingestion,\nprocess-documents,\nprocess-repo-summary,\nprocess-batch-summary)")

            with Cluster("Workspace & Admin"):
                ws_svc = Lambda("Workspace\nManagement")
                audit_svc = Lambda("Audit Logging\n+ Alerting")

            # Async messaging
            queues = SQS("SQS Queues\n(per-source FIFO,\ncode-maps, ingestion,\ndelete-worker)")
            eventbus = Eventbridge("EventBridge\nAutomation Bus")

            # Storage
            with Cluster("Storage (per-buyer isolation)"):
                s3_kb = S3("Knowledge Base\nS3 Bucket")
                s3_vec = S3("S3 Vectors\n(1024-dim embeddings)")
                ddb_state = Dynamodb("Workspaces, Chat,\nCode-Maps, Audit Logs\n(DynamoDB)")

            # AWS managed services - consumed FROM the buyer's account
            with Cluster("AWS Managed Services (in buyer's account)"):
                bedrock = Sagemaker("Amazon Bedrock\n(Claude, Titan,\nCohere Rerank)")
                comprehend = Comprehend("Amazon Comprehend\n(PII redaction)")

            # Security & config (in buyer's account)
            with Cluster("Security & Config"):
                kms = KMS("KMS")
                ssm = SSM("SSM Parameter Store\n(license JWT,\nintegration secrets)")
                cw = Cloudwatch("CloudWatch\nLogs & Metrics")

            # The license validator layer ATTACHES to gated Lambdas - it's
            # published from the seller account but RUNS in the buyer's
            # Lambdas
            license_layer_in_buyer = Lambda("License Validator Layer\n(attached to all\ngated Lambdas)")

    # =========================================================================
    # FLOWS
    # =========================================================================

    # --- Buyer onboarding via AWS Marketplace ---
    marketplace_api >> Edge(label="Subscription event", color="darkorange") >> marketplace_lambda
    marketplace_lambda >> license_db
    marketplace_lambda >> Edge(label="Issue JWT", color="darkorange") >> license_api

    # --- License lifecycle (control plane internal) ---
    license_api >> license_db
    license_api >> license_layer_pub
    admin_api >> license_db

    # --- License layer attached + phone-home (cross-account) ---
    license_layer_pub >> Edge(label="Layer ARN\n(cross-account)",
                              style="dashed", color="darkorange") >> license_layer_in_buyer
    license_layer_in_buyer >> Edge(label="POST /license/validate\n(phone-home, 1h cache)",
                                   color="darkorange") >> license_api

    # --- Usage metering (buyer → seller) ---
    license_layer_in_buyer >> Edge(label="track_repo_active,\ntrack_pr_generated,\ntrack_command_executed",
                                   color="darkorange") >> usage_api
    usage_api >> usage_db
    usage_api >> Edge(label="Aggregate metering",
                      color="darkorange") >> marketplace_lambda
    marketplace_lambda >> Edge(label="BatchMeterUsage",
                               color="darkorange") >> marketplace_api

    # --- Buyer user flow (application plane internal) ---
    dns >> alb >> ui
    ui >> Edge(label="SigV4 IAM auth") >> chat_svc
    ui >> ws_svc

    # --- Chat / Query data plane ---
    chat_svc >> bedrock
    chat_svc >> s3_vec
    chat_svc >> ddb_state
    query_svc >> bedrock
    query_svc >> s3_vec

    # --- Code Generation ---
    api_gw >> codegen_svc
    codegen_svc >> bedrock
    codegen_svc >> queues
    codegen_svc >> eventbus

    # --- Knowledge Base Ingestion ---
    ingest_svc >> queues
    queues >> process_svc
    process_svc >> comprehend
    process_svc >> bedrock
    process_svc >> s3_kb
    process_svc >> s3_vec
    process_svc >> ddb_state

    # --- Workspace / Audit ---
    ws_svc >> ddb_state
    audit_svc >> ddb_state
    chat_svc >> Edge(label="audit row", style="dashed") >> audit_svc

    # --- Security & config (cross-cutting) ---
    ssm >> Edge(style="dotted") >> chat_svc
    ssm >> Edge(style="dotted") >> license_layer_in_buyer
    kms >> Edge(style="dotted") >> s3_kb
    kms >> Edge(style="dotted") >> ddb_state
    cw << Edge(style="dotted") << chat_svc
