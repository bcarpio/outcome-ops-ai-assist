"""
OutcomeOps AI Assist - Data Flow Diagram
ARCH-004: Data flow showing ingestion, query, and PR analysis event routing.

Generates: data_flow.png
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.storage import S3
from diagrams.aws.database import Dynamodb
from diagrams.aws.integration import SQS, Eventbridge, SNS
from diagrams.aws.ml import Sagemaker, Comprehend
from diagrams.aws.network import ALB
from diagrams.aws.compute import Fargate
from diagrams.aws.network import APIGateway as Apigw
from diagrams.onprem.client import Users
from diagrams.onprem.vcs import Github

graph_attr = {
    "fontsize": "22",
    "bgcolor": "white",
    "pad": "0.8",
    "splines": "ortho",
    "nodesep": "0.6",
    "ranksep": "0.8",
}

with Diagram(
    "OutcomeOps AI Assist - Data Flow",
    filename="data_flow",
    outformat="png",
    show=False,
    direction="TB",
    graph_attr=graph_attr,
):
    github = Github("GitHub")
    users = Users("Users")

    # ============================================================
    # FLOW 1: Knowledge Ingestion Pipeline
    # ============================================================
    with Cluster("Flow 1: Knowledge Ingestion"):

        with Cluster("1a. Application Repos"):
            gh_sync = Lambda("github-sync")
            repo_queue = SQS("repo-summaries\nqueue.fifo")
            process_repo = Lambda("process-repo-\nsummary")
            code_maps_queue = SQS("code-maps\nqueue")
            process_batch = Lambda("process-batch-\nsummary")
            ddb_symbol_graph = Dynamodb("DynamoDB\nsymbol-graph")

        with Cluster("1b. Standards Repos"):
            gh_sync_std = Lambda("github-sync\n(standards)")
            s3_kb = S3("S3 Knowledge\nBase Bucket")

        with Cluster("1c. Document Processing"):
            process_docs = Lambda("process-\ndocuments")
            comprehend_pii = Comprehend("Comprehend\nDetect-PII\n(STRICT mode)")
            titan_embed = Sagemaker("Titan\nEmbeddings v2")
            s3_vectors = S3("S3 Vectors\n(1024-dim)")

        with Cluster("1d. Third-Party Sources"):
            confluence = Lambda("confluence-sync")
            jira = Lambda("jira-sync")
            teams = Lambda("teams-sync")
            sharepoint = Lambda("sharepoint-sync")
            file_ingest = Lambda("file-ingestion")
            database = Lambda("database-sync\n(MSSQL/MySQL/Postgres)")
            onedrive = Lambda("onedrive-sync")
            onenote = Lambda("onenote-sync")

    # Ingestion connections
    github >> Edge(label="App webhook") >> gh_sync
    gh_sync >> repo_queue
    repo_queue >> process_repo
    process_repo >> code_maps_queue
    code_maps_queue >> process_batch
    process_batch >> s3_kb

    github >> Edge(label="Standards webhook") >> gh_sync_std
    gh_sync_std >> Edge(label="Direct upload") >> s3_kb

    s3_kb >> Edge(label="S3 ObjectCreated\ntrigger") >> process_docs
    process_docs >> Edge(label="Redact PII\n(STRICT only)") >> comprehend_pii
    comprehend_pii >> Edge(label="Redacted text") >> titan_embed
    process_batch >> Edge(label="Redact summary\n(STRICT only)", style="dashed") >> comprehend_pii
    process_repo >> Edge(label="Redact summary\n(STRICT only)", style="dashed") >> comprehend_pii
    titan_embed >> Edge(label="Embeddings") >> s3_vectors

    confluence >> file_ingest
    jira >> file_ingest
    teams >> file_ingest
    sharepoint >> file_ingest
    onedrive >> file_ingest
    onenote >> file_ingest
    file_ingest >> s3_kb

    # Database source: introspects schemas + drift notifications
    sns_db_drift = SNS("SNS\ndatabase-drift")
    database >> Edge(label="Introspect\nschemas") >> s3_kb
    database >> Edge(label="Drift detected", style="dashed") >> sns_db_drift

    # Symbol-graph: tree-sitter facts from process-batch-summary
    process_batch >> Edge(label="Symbol facts\n(tree-sitter)") >> ddb_symbol_graph

    # ============================================================
    # FLOW 2: Chat / RAG Query Pipeline
    # ============================================================
    with Cluster("Flow 2: Chat Query (RAG)"):
        alb = ALB("ALB")
        fargate = Fargate("Fargate\nChat UI")
        chat_lambda = Lambda("chat-streaming")
        router = Lambda("router\n(Bedrock intent\nclassifier)")
        titan_query = Sagemaker("Titan\nEmbeddings v2")
        s3_vec_query = S3("S3 Vectors\n(Search)")
        cohere_rerank = Sagemaker("Cohere\nRerank 3.5")
        ddb_symbol_graph_query = Dynamodb("DynamoDB\nsymbol-graph\n(read)")
        claude_llm = Sagemaker("Claude\nSonnet/Haiku")
        ddb_chat = Dynamodb("DynamoDB\nchat table")
        ddb_audit = Dynamodb("DynamoDB\naudit-logs")

    # Query connections
    users >> Edge(label="HTTPS") >> alb
    alb >> fargate
    fargate >> Edge(label="SigV4") >> chat_lambda
    chat_lambda >> Edge(label="1. Classify\nintent") >> router
    router >> Edge(label="2a. Embed\nquery") >> titan_query
    titan_query >> Edge(label="3a. Vector\nsearch") >> s3_vec_query
    s3_vec_query >> Edge(label="4a. Over-fetch\ncandidates") >> cohere_rerank
    cohere_rerank >> Edge(label="5a. Reranked\nTop-K") >> claude_llm
    router >> Edge(
        label="2b. structural /\nhybrid / unknown\nonly",
        style="dashed",
    ) >> ddb_symbol_graph_query
    ddb_symbol_graph_query >> Edge(
        label="3b. Structural\nfacts",
        style="dashed",
    ) >> claude_llm
    claude_llm >> Edge(label="6. Synthesized\nresponse") >> chat_lambda
    chat_lambda >> ddb_chat
    chat_lambda >> ddb_audit

    # ============================================================
    # FLOW 3: PR Analysis Pipeline
    # ============================================================
    with Cluster("Flow 3: PR Analysis"):
        apigw = Apigw("API Gateway v2")
        analyze = Lambda("analyze-pr")
        pr_queue = SQS("pr-checks\nqueue.fifo")
        process_pr = Lambda("process-pr-\ncheck")
        claude_pr = Sagemaker("Claude\n(PR Review)")

    # PR analysis connections
    github >> Edge(label="PR webhook") >> apigw
    apigw >> analyze
    analyze >> pr_queue
    pr_queue >> process_pr
    process_pr >> claude_pr
    claude_pr >> Edge(label="Review\ncomment") >> process_pr
    process_pr >> Edge(label="Post comment") >> github

    # ============================================================
    # FLOW 4: Event-Driven Automation
    # ============================================================
    with Cluster("Flow 4: Event-Driven Automation"):
        gen_code = Lambda("generate-code")
        event_bus = Eventbridge("EventBridge\nCustom Bus")
        run_tests_fn = Lambda("run-tests")
        code_queue = SQS("code-generation\nqueue.fifo")
        ddb_state = Dynamodb("DynamoDB\ncode-maps")

    # Automation connections
    gen_code >> Edge(label="CodeGeneration\n.Completed") >> event_bus
    event_bus >> Edge(label="Rule match") >> run_tests_fn
    run_tests_fn >> Edge(label="Tests.Completed") >> event_bus
    gen_code >> code_queue
    gen_code >> ddb_state
