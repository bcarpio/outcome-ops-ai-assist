"""
Process Documents Lambda for OutcomeOps AI Assist.

Triggered by S3 ObjectCreated events on the knowledge base bucket.
Extracts text from documents, generates embeddings, and stores in S3 Vectors.

Supported file types:
- .html (Confluence pages, emails)
- .md (Markdown documentation)
- .txt (Plain text)
- .pdf (PDF documents via textract)
- .json, .yaml, .yml (Structured data)

S3 Path Structure (input):
    workspaces/{workspace_id}/{source}/{folder_path}/{filename}

Vector Key Structure:
    {workspace_id}#{source}#{folder_path}#{filename}_chunk_{index}

Vector Metadata:
    workspace_id, source, folder_path, doc_type, chunk_index, total_chunks,
    content, file_name, file_path, content_hash, timestamp
"""

import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

# Add shared module to path for Lambda deployment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit import COMPREHEND_MODEL_ID, log_audit_entry
from shared.audit_writer import (
    ACTION_DOCUMENT_INGESTED,
    log_admin_action,
    log_license_fail_open,
)
from shared.pii import MODE_STRICT, read_workspace_mode, redact

# Bedrock model configuration
EMBEDDING_MODEL_ID = os.environ.get(
    "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
KB_BUCKET = os.environ.get("KB_BUCKET")

# AWS clients
s3_client = boto3.client("s3")
ssm_client = boto3.client("ssm")
# ADR-035: bedrock_client is embeddings-only here (Titan via
# invoke_model at line ~538). process-documents has no LLM-completion
# path; if one is added, use shared.model_client.invoke per ADR-035.
bedrock_client = boto3.client("bedrock-runtime")
s3vectors_client = boto3.client("s3vectors")
sts_client = boto3.client("sts")
dynamodb_resource = boto3.resource("dynamodb")

# Cached account ID
_account_id = None


def _get_workspaces_table():
    """Lazy-init DDB table handle for workspace lookups (privacy_mode)."""
    name = os.environ.get("WORKSPACES_TABLE", "")
    if not name:
        return None
    return dynamodb_resource.Table(name)


def _get_account_id() -> str:
    """Return the AWS account ID (cached after first call)."""
    global _account_id
    if _account_id is None:
        _account_id = sts_client.get_caller_identity()["Account"]
    return _account_id

# Vector store config (loaded from SSM)
_vector_config = None


def _load_vector_config() -> Dict[str, str]:
    """Load vector store configuration from SSM."""
    global _vector_config
    if _vector_config is not None:
        return _vector_config

    prefix = f"/{ENV}/{APP_NAME}/s3vectors"
    try:
        bucket = ssm_client.get_parameter(Name=f"{prefix}/bucket-name")["Parameter"]["Value"]
        index = ssm_client.get_parameter(Name=f"{prefix}/index-name")["Parameter"]["Value"]
        _vector_config = {"bucket": bucket, "index": index}
        return _vector_config
    except ClientError as e:
        logger.error(f"Failed to load vector config: {e}")
        raise


# ============================================================================
# S3 Key Parsing
# ============================================================================


def parse_s3_key(key: str) -> Optional[Dict[str, str]]:
    """
    Parse S3 key to extract workspace_id, source, folder_path, filename.

    Expected format: workspaces/{workspace_id}/{source}/{folder_path}/{filename}

    Returns:
        Dict with workspace_id, source, folder_path, filename, or None if invalid
    """
    # Decode URL-encoded characters
    key = unquote_plus(key)

    # Parse path
    parts = key.split("/")

    if len(parts) < 4 or parts[0] != "workspaces":
        logger.warning(f"Invalid S3 key format: {key}")
        return None

    workspace_id = parts[1]
    source = parts[2]

    # Everything between source and filename is folder_path
    filename = parts[-1]
    folder_path = "/".join(parts[3:-1]) if len(parts) > 4 else ""

    return {
        "workspace_id": workspace_id,
        "source": source,
        "folder_path": folder_path,
        "filename": filename,
    }


# ============================================================================
# Text Extraction
# ============================================================================


def extract_text_from_html(content: bytes) -> str:
    """Extract plain text from HTML content."""
    text = content.decode("utf-8", errors="replace")

    # Remove script and style elements
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)

    # Convert block elements to newlines
    text = re.sub(r'<(p|div|h[1-6]|li|tr|br|hr)[^>]*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(p|div|h[1-6]|li|tr)>', '\n', text, flags=re.IGNORECASE)

    # Remove remaining tags
    text = re.sub(r'<[^>]+>', '', text)

    # Decode HTML entities
    text = html.unescape(text)

    # Clean up whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = '\n'.join(line.strip() for line in text.split('\n'))

    return text.strip()


def extract_text_from_markdown(content: bytes) -> str:
    """Extract text from Markdown, preserving structure."""
    return content.decode("utf-8", errors="replace").strip()


def extract_text_from_plain(content: bytes) -> str:
    """Extract text from plain text files."""
    return content.decode("utf-8", errors="replace").strip()


def extract_text_from_json(content: bytes) -> str:
    """Extract text from JSON, formatting for readability."""
    try:
        data = json.loads(content.decode("utf-8"))
        return json.dumps(data, indent=2)
    except json.JSONDecodeError:
        return content.decode("utf-8", errors="replace").strip()


def extract_text_from_yaml(content: bytes) -> str:
    """Extract text from YAML files."""
    return content.decode("utf-8", errors="replace").strip()


def extract_text_from_pdf_pypdf2(content: bytes) -> str:
    """Extract text from PDF files using PyPDF2 (for text-based PDFs)."""
    try:
        import io
        from PyPDF2 import PdfReader

        reader = PdfReader(io.BytesIO(content))
        text_parts = []
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
        return "\n\n".join(text_parts).strip()
    except Exception as e:
        logger.warning(f"Failed to extract PDF text with PyPDF2: {e}")
        return ""


def extract_text_from_pdf_textract(s3_bucket: str, s3_key: str) -> str:
    """Extract text from scanned PDF using AWS Textract OCR.

    Uses async API (start/get) which supports multi-page PDFs.
    Cost: ~$1.50 per 1000 pages.
    """
    import time

    try:
        textract_client = boto3.client("textract")

        # Start async text detection job
        response = textract_client.start_document_text_detection(
            DocumentLocation={
                "S3Object": {"Bucket": s3_bucket, "Name": s3_key}
            }
        )
        job_id = response["JobId"]
        logger.info(f"Started Textract job {job_id} for {s3_key}")

        # Poll for completion (max 5 minutes)
        max_wait = 300
        waited = 0
        while waited < max_wait:
            result = textract_client.get_document_text_detection(JobId=job_id)
            status = result["JobStatus"]

            if status == "SUCCEEDED":
                break
            elif status == "FAILED":
                logger.error(f"Textract job {job_id} failed: {result.get('StatusMessage', 'unknown error')}")
                return ""

            time.sleep(2)
            waited += 2

        if waited >= max_wait:
            logger.error(f"Textract job {job_id} timed out after {max_wait}s")
            return ""

        # Collect LINE blocks across all pages
        text_parts = []
        for block in result.get("Blocks", []):
            if block["BlockType"] == "LINE":
                text_parts.append(block["Text"])

        # Handle pagination for large documents
        next_token = result.get("NextToken")
        while next_token:
            result = textract_client.get_document_text_detection(
                JobId=job_id, NextToken=next_token
            )
            for block in result.get("Blocks", []):
                if block["BlockType"] == "LINE":
                    text_parts.append(block["Text"])
            next_token = result.get("NextToken")

        if not text_parts:
            logger.warning(f"Textract OCR extracted no text from {s3_key}")
            return ""

        logger.info(f"Textract extracted {len(text_parts)} lines from {s3_key}")
        return "\n".join(text_parts)

    except Exception as e:
        logger.error(f"Textract OCR failed for {s3_key}: {e}")
        return ""


def extract_text_from_pdf(
    content: bytes,
    s3_bucket: str = None,
    s3_key: str = None,
    force_textract: bool = False,
) -> str:
    """Extract text from PDF with OCR fallback for scanned documents.

    Tries PyPDF2 first (fast, free). Falls back to AWS Textract OCR
    (~$1.50/1000 pages) when PyPDF2 extraction is insufficient.

    Uses per-page character density to detect image-heavy PDFs (e.g. slide
    decks) where PyPDF2 extracts some text but misses most content.

    ``force_textract`` bypasses the heuristic. Set by the caller for
    sources where visual content (tables, diagrams, signature pages,
    screenshots) reliably carries business value that PyPDF2 drops --
    all ``*_attachment`` sources (Salesforce, ServiceNow, Confluence,
    Jira user-uploaded PDFs). PyPDF2 still runs as a fallback if
    Textract fails.
    """
    import io
    from PyPDF2 import PdfReader

    # Try PyPDF2 first (works for text-based PDFs)
    text = extract_text_from_pdf_pypdf2(content)
    extracted_len = len(text.strip()) if text else 0

    # Count pages to calculate extraction density
    try:
        page_count = len(PdfReader(io.BytesIO(content)).pages)
    except Exception:
        page_count = 1

    chars_per_page = extracted_len / max(page_count, 1)

    if force_textract and s3_bucket and s3_key:
        logger.info(
            f"Textract forced (attachment source): {s3_key} "
            f"(PyPDF2 baseline: {extracted_len} chars, {page_count} pages)"
        )
        textract_text = extract_text_from_pdf_textract(s3_bucket, s3_key)
        # Prefer Textract; fall back to PyPDF2 only if Textract yielded
        # strictly less content (e.g. Textract failed and returned "").
        if textract_text and len(textract_text.strip()) >= extracted_len:
            return textract_text
        logger.warning(
            f"Textract returned {len(textract_text.strip()) if textract_text else 0} chars vs "
            f"PyPDF2 {extracted_len}; using PyPDF2 as fallback"
        )
        return text or ""

    # A normal text page yields 1000-3000+ chars. Slide decks with images
    # yield much less. If density is below 200 chars/page, the PDF is likely
    # image-heavy and needs OCR even if total chars > 50.
    if extracted_len > 50 and chars_per_page >= 200:
        logger.info(
            f"PyPDF2 extraction sufficient: {extracted_len} chars from {page_count} pages "
            f"({chars_per_page:.0f} chars/page)"
        )
        return text

    # Fallback to Textract OCR for scanned/image-heavy PDFs
    if s3_bucket and s3_key:
        logger.info(
            f"PyPDF2 extraction insufficient ({extracted_len} chars, {page_count} pages, "
            f"{chars_per_page:.0f} chars/page), attempting Textract OCR: {s3_key}"
        )
        textract_text = extract_text_from_pdf_textract(s3_bucket, s3_key)
        # Use whichever extraction got more content
        if textract_text and len(textract_text.strip()) > extracted_len:
            return textract_text
        return text or ""

    logger.warning("No text extracted and no S3 location for Textract fallback")
    return text or ""


def extract_text_from_docx(content: bytes) -> str:
    """Extract text from DOCX files using python-docx."""
    try:
        import io
        from docx import Document

        doc = Document(io.BytesIO(content))
        text_parts = []
        for para in doc.paragraphs:
            if para.text.strip():
                text_parts.append(para.text)
        # Also extract text from tables
        for table in doc.tables:
            for row in table.rows:
                row_text = "\t".join(cell.text.strip() for cell in row.cells)
                if row_text.strip():
                    text_parts.append(row_text)
        return "\n\n".join(text_parts).strip()
    except Exception as e:
        logger.warning(f"Failed to extract DOCX text: {e}")
        return ""


def extract_text_from_xlsx(content: bytes) -> str:
    """Extract text from XLSX files using openpyxl."""
    try:
        import io
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        text_parts = []
        for sheet_name in wb.sheetnames:
            sheet = wb[sheet_name]
            text_parts.append(f"# Sheet: {sheet_name}\n")
            for row in sheet.iter_rows(values_only=True):
                row_values = [str(cell) if cell is not None else "" for cell in row]
                if any(v.strip() for v in row_values):
                    text_parts.append("\t".join(row_values))
        wb.close()
        return "\n".join(text_parts).strip()
    except Exception as e:
        logger.warning(f"Failed to extract XLSX text: {e}")
        return ""


def extract_text_from_pptx(content: bytes) -> str:
    """Extract text from PPTX files using python-pptx."""
    try:
        import io
        from pptx import Presentation

        prs = Presentation(io.BytesIO(content))
        text_parts = []
        for slide_num, slide in enumerate(prs.slides, 1):
            slide_texts = []
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    slide_texts.append(shape.text)
            if slide_texts:
                text_parts.append(f"# Slide {slide_num}\n" + "\n".join(slide_texts))
        return "\n\n".join(text_parts).strip()
    except Exception as e:
        logger.warning(f"Failed to extract PPTX text: {e}")
        return ""


def extract_text_from_csv(content: bytes) -> str:
    """Extract text from CSV files."""
    try:
        import csv
        import io

        text = content.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = []
        for row in reader:
            if any(cell.strip() for cell in row):
                rows.append("\t".join(row))
        return "\n".join(rows).strip()
    except Exception as e:
        logger.warning(f"Failed to extract CSV text: {e}")
        return content.decode("utf-8", errors="replace").strip()


def extract_text(
    filename: str,
    content: bytes,
    s3_bucket: str = None,
    s3_key: str = None,
    source: str = "",
) -> Tuple[str, str]:
    """
    Extract text from file based on extension.

    Args:
        filename: Name of the file
        content: File content as bytes
        s3_bucket: S3 bucket name (for Textract OCR fallback on PDFs)
        s3_key: S3 object key (for Textract OCR fallback on PDFs)
        source: The upstream source (from parse_s3_key). ``*_attachment``
                sources force Textract on PDFs to catch tables, diagrams,
                signature pages, and screenshots that PyPDF2 drops. Native
                code-repo PDFs (source == "github" etc.) keep the fast
                heuristic path.

    Returns:
        Tuple of (extracted_text, doc_type)
    """
    filename_lower = filename.lower()
    force_textract = bool(source) and source.endswith("_attachment")

    if filename_lower.endswith(".html") or filename_lower.endswith(".htm"):
        return extract_text_from_html(content), "html"
    elif filename_lower.endswith(".md"):
        return extract_text_from_markdown(content), "markdown"
    elif filename_lower.endswith(".txt"):
        return extract_text_from_plain(content), "text"
    elif filename_lower.endswith(".json"):
        return extract_text_from_json(content), "json"
    elif filename_lower.endswith((".yaml", ".yml")):
        return extract_text_from_yaml(content), "yaml"
    elif filename_lower.endswith(".pdf"):
        return (
            extract_text_from_pdf(
                content, s3_bucket, s3_key, force_textract=force_textract,
            ),
            "pdf",
        )
    elif filename_lower.endswith(".docx"):
        return extract_text_from_docx(content), "docx"
    elif filename_lower.endswith(".xlsx"):
        return extract_text_from_xlsx(content), "xlsx"
    elif filename_lower.endswith(".pptx"):
        return extract_text_from_pptx(content), "pptx"
    elif filename_lower.endswith(".csv"):
        return extract_text_from_csv(content), "csv"
    else:
        # Try to decode as text
        try:
            return content.decode("utf-8").strip(), "unknown"
        except UnicodeDecodeError:
            logger.warning(f"Cannot extract text from binary file: {filename}")
            return "", "binary"


# ============================================================================
# Embedding Generation
# ============================================================================


def chunk_text(text: str, max_chars: int = 8000, overlap: int = 500) -> List[str]:
    """
    Split text into chunks for embedding.

    Args:
        text: Text to chunk
        max_chars: Maximum characters per chunk (~2000 tokens)
        overlap: Character overlap between chunks

    Returns:
        List of text chunks
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    start = 0

    while start < len(text):
        end = min(start + max_chars, len(text))

        # Try to break at paragraph boundary
        if end < len(text):
            # Look for paragraph break
            para_break = text.rfind("\n\n", start, end)
            if para_break > start + max_chars * 0.5:
                end = para_break
            else:
                # Look for sentence break
                sentence_break = text.rfind(". ", start, end)
                if sentence_break > start + max_chars * 0.5:
                    end = sentence_break + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break

        start = max(start + 1, end - overlap)

    return chunks if chunks else [text]


def generate_embedding(text: str) -> List[float]:
    """
    Generate embedding using Bedrock Titan Embeddings v2.

    Args:
        text: Text to embed

    Returns:
        1024-dimensional embedding vector, or empty list on failure
    """
    try:
        bedrock_start = time.time()
        response = bedrock_client.invoke_model(
            modelId=EMBEDDING_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "inputText": text,
                "dimensions": 1024,
                "normalize": True,
            }),
        )
        bedrock_duration_ms = int((time.time() - bedrock_start) * 1000)

        response_body = json.loads(response["body"].read())
        embedding = response_body.get("embedding", [])

        if not embedding:
            logger.warning("Empty embedding returned")
            return []

        log_audit_entry(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            lambda_name="process-documents",
            model_id=EMBEDDING_MODEL_ID,
            input_text=text,
            output_text=f"embedding[{len(embedding)}]",
            duration_ms=bedrock_duration_ms,
        )

        return embedding

    except ClientError as e:
        if "Too many input tokens" in str(e):
            logger.warning(f"Text too large for embedding ({len(text)} chars), truncating")
            # Truncate and retry
            truncated = text[:30000]  # ~7500 tokens
            return generate_embedding(truncated)
        logger.error(f"Failed to generate embedding: {e}")
        return []


def compute_content_hash(content: str) -> str:
    """Compute SHA-256 hash of content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ============================================================================
# Vector Storage
# ============================================================================


def store_vectors(
    workspace_id: str,
    source: str,
    folder_path: str,
    filename: str,
    s3_key: str,
    chunks: List[str],
    embeddings: List[List[float]],
    doc_type: str,
    metadata: Dict[str, str],
) -> int:
    """
    Store document embeddings in S3 Vectors.

    Args:
        workspace_id: Workspace ID
        source: Source type (confluence, jira, etc.)
        folder_path: Folder path within source
        filename: Original filename
        s3_key: S3 key of source document
        chunks: Text chunks
        embeddings: Embeddings for each chunk
        doc_type: Document type
        metadata: Additional S3 object metadata

    Returns:
        Number of vectors stored
    """
    config = _load_vector_config()
    content_hash = compute_content_hash("".join(chunks))
    timestamp = datetime.now(timezone.utc).isoformat()

    vectors = []
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        if not embedding:
            continue

        # Build vector key
        safe_folder = folder_path.replace("/", "#") if folder_path else ""
        if safe_folder:
            vector_key = f"{workspace_id}#{source}#{safe_folder}#{filename}_chunk_{i}"
        else:
            vector_key = f"{workspace_id}#{source}#{filename}_chunk_{i}"

        # For GitHub sources, prefer explicit repo name from S3 object metadata
        # (set by github-sync), fall back to first folder_path component
        repo = ""
        if source == "github":
            repo = metadata.get("github_repo_name", "")
            if not repo and folder_path:
                repo = folder_path.split("/")[0]

        vector_metadata = {
            "workspace_id": workspace_id,
            "source": source,
            "repo": repo,
            "folder_path": folder_path,
            "file_name": filename,
            "file_path": s3_key,
            "type": doc_type,
            "chunk_index": str(i),
            "total_chunks": str(len(chunks)),
            "content": chunk[:10000],  # Limit metadata size
            "content_hash": content_hash,
            "timestamp": timestamp,
        }

        # Add source-specific metadata
        for key in ["confluence_page_id", "confluence_space_key", "connection_id"]:
            if key in metadata:
                vector_metadata[key] = metadata[key]

        vectors.append({
            "key": vector_key,
            "data": {"float32": embedding},
            "metadata": vector_metadata,
        })

    if not vectors:
        logger.warning(f"No vectors to store for {filename}")
        return 0

    try:
        s3vectors_client.put_vectors(
            vectorBucketName=config["bucket"],
            indexName=config["index"],
            vectors=vectors,
        )
        logger.info(f"Stored {len(vectors)} vectors for {filename}")
        return len(vectors)

    except ClientError as e:
        logger.error(f"Failed to store vectors: {e}")
        return 0


def delete_vectors_for_document(workspace_id: str, source: str, folder_path: str, filename: str) -> int:
    """
    Delete all vectors for a document.

    Args:
        workspace_id: Workspace ID
        source: Source type
        folder_path: Folder path
        filename: Filename

    Returns:
        Number of vectors deleted
    """
    config = _load_vector_config()

    # Build key prefix to find all chunks
    safe_folder = folder_path.replace("/", "#") if folder_path else ""
    if safe_folder:
        key_prefix = f"{workspace_id}#{source}#{safe_folder}#{filename}_chunk_"
    else:
        key_prefix = f"{workspace_id}#{source}#{filename}_chunk_"

    # Delete chunks 0-99 (should cover any document)
    keys_to_delete = [f"{key_prefix}{i}" for i in range(100)]

    try:
        s3vectors_client.delete_vectors(
            vectorBucketName=config["bucket"],
            indexName=config["index"],
            keys=keys_to_delete,
        )
        logger.info(f"Deleted vectors with prefix {key_prefix}")
        return len(keys_to_delete)
    except ClientError as e:
        logger.warning(f"Error deleting vectors: {e}")
        return 0


# ============================================================================
# Main Handler
# ============================================================================


def _emit_ingest_audit(
    workspace_id: str,
    source: str,
    filename: str,
    redaction,
    mode: str,
) -> None:
    """Emit two audit rows for one ingest: an admin DOCUMENT_INGESTED row
    that surfaces in the audit dashboard, and a Comprehend bedrock row
    that rolls into cost-by-model. Skipped runs (workspace_none /
    org_disabled / empty_text) emit only the admin row.

    Non-fatal: audit writes never raise. Mirrors plan/ chunk 05 spec.
    """
    audit_table = os.environ.get("AUDIT_TABLE", "")
    if not audit_table:
        return

    log_admin_action(
        audit_table,
        ACTION_DOCUMENT_INGESTED,
        workspace_id=workspace_id,
        user_email="",  # S3-triggered; no user context.
        source_ip="",
        user_agent="",
        details={
            "source": source,
            "document_id": filename,
            "char_count": redaction.char_count,
            "redaction_summary": redaction.summary,
            "skipped_reason": redaction.skipped_reason,
            "mode": mode,
        },
    )

    # Cost row only when Comprehend actually ran (or attempted to).
    # Never log the raw input/output - char_count alone is what cost
    # rollups need.
    if redaction.skipped_reason in ("", "comprehend_error"):
        log_audit_entry(
            audit_table_name=audit_table,
            lambda_name="process-documents",
            model_id=COMPREHEND_MODEL_ID,
            input_text="",
            output_text="",
            char_count=redaction.char_count,
            workspace_id=workspace_id,
        )


def _close_docs_unit(
    workspace_id: str,
    source: str,
    metadata: Dict[str, str],
    *,
    status: str,
    file_path: str = "",
    failure_reason: str = "",
    failure_detail: str = "",
) -> None:
    """Close one unit of the docs SHA-truth ledger after a successful
    (or permanently failed) vector write.

    plan-git-truth chunk 02b/4: github-sync opens
    ``PENDING#docs#{sha}`` with ``expected_count = uploaded count``.
    Each process-documents invocation closes one unit; the last
    close advances ``last_docs_sha`` on the workspaces row. The
    stricter contract (SHA only advances after every vector lands)
    is required by the active/passive HA failover story — a mid-
    vectorization failover must see the SHA still at the previous
    commit so the secondary region picks up where the primary
    left off.

    Only fires for github sources today. confluence/jira/etc. don't
    have a Git SHA concept; tracking those under SHA-truth needs a
    separate ledger design (a sync-id, not a commit_sha).
    """
    if source != "github":
        return
    repo = metadata.get("github_repo_name") or ""
    commit_sha = metadata.get("github_commit_sha") or ""
    if not (workspace_id and repo and commit_sha):
        return

    try:
        from shared.sha_truth import (  # type: ignore
            mark_unit_complete,
            mark_unit_failed,
            try_advance_sha,
        )
    except Exception as exc:  # pragma: no cover - import-failure path
        logger.warning(
            "[process-documents] sha_truth import failed: %s", exc
        )
        return

    try:
        if status == "ok":
            ledger = mark_unit_complete(
                workspace_id, repo, "docs", commit_sha
            )
        else:
            ledger = mark_unit_failed(
                workspace_id,
                repo,
                "docs",
                commit_sha,
                file_path=file_path or "<unknown-doc>",
                failure_reason=failure_reason or "process_documents_failed",
                failure_detail=failure_detail,
                attempt_count=1,
            )
        if ledger:
            try_advance_sha(ledger)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[process-documents] sha-truth %s failed for %s@%s: %s",
            status,
            repo,
            commit_sha,
            exc,
        )


def process_document(bucket: str, key: str) -> Dict[str, Any]:
    """
    Process a single document from S3.

    Args:
        bucket: S3 bucket name
        key: S3 object key

    Returns:
        Processing result
    """
    # Parse S3 key
    key_info = parse_s3_key(key)
    if not key_info:
        return {"status": "skipped", "reason": "Invalid key format", "key": key}

    workspace_id = key_info["workspace_id"]
    source = key_info["source"]
    folder_path = key_info["folder_path"]
    filename = key_info["filename"]

    logger.info(f"Processing document: {key}")

    # Hoisted outside the try so the except path can pass it to
    # _close_docs_unit for failure-mode SHA-truth bookkeeping.
    metadata: Dict[str, str] = {}

    try:
        # Get object from S3
        response = s3_client.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=_get_account_id())
        content = response["Body"].read()
        metadata = response.get("Metadata", {})

        # Extract text (pass S3 location for Textract OCR fallback on PDFs;
        # source lets extract_text force Textract on *_attachment sources).
        text, doc_type = extract_text(
            filename, content, s3_bucket=bucket, s3_key=key, source=source,
        )

        # Use doc type classification from github-sync (set via .outcomeops.yaml config)
        if metadata.get("github_doc_type"):
            doc_type = metadata["github_doc_type"]

        if not text or len(text) < 50:
            logger.warning(f"Insufficient text content in {filename}")
            # plan-git-truth chunk 02b/4 race fix #2 (2026-05-17): every
            # github-sync-uploaded doc has a corresponding unit on the
            # docs ledger. If we early-return here without closing the
            # unit, the ledger sits below expected_count forever and
            # last_docs_sha never advances. Closing as
            # mark_unit_complete is the right semantic — the file IS
            # done with this commit (we deliberately skipped vectorizing
            # tiny files), the commit isn't blocked on it.
            _close_docs_unit(workspace_id, source, metadata, status="ok")
            return {"status": "skipped", "reason": "Insufficient content", "key": key}

        # PII redaction (plan/ chunk 05). Scrub PII from `text` BEFORE
        # chunking + embedding so neither S3 Vectors nor Bedrock ever see
        # raw identifiers. Honors the workspace's privacy_mode and the
        # org-level PII_REDACTION_ENABLED kill switch. The audit rows
        # below capture char_count + summary so operators can see what
        # was protected without ever logging the raw PII.
        ws_table = _get_workspaces_table()
        org_enabled = (
            os.environ.get("PII_REDACTION_ENABLED", "true").lower() == "true"
        )
        mode = (
            read_workspace_mode(ws_table, workspace_id)
            if ws_table is not None
            else MODE_STRICT
        )
        redaction = redact(text, mode=mode, org_enabled=org_enabled)
        text = redaction.text

        _emit_ingest_audit(
            workspace_id=workspace_id,
            source=source,
            filename=filename,
            redaction=redaction,
            mode=mode,
        )

        # Chunk text
        chunks = chunk_text(text)
        logger.info(f"Split {filename} into {len(chunks)} chunks")

        # Generate embeddings
        embeddings = []
        for i, chunk in enumerate(chunks):
            embedding = generate_embedding(chunk)
            embeddings.append(embedding)
            if not embedding:
                logger.warning(f"Failed to generate embedding for chunk {i} of {filename}")

        # Store vectors
        vectors_stored = store_vectors(
            workspace_id=workspace_id,
            source=source,
            folder_path=folder_path,
            filename=filename,
            s3_key=key,
            chunks=chunks,
            embeddings=embeddings,
            doc_type=doc_type,
            metadata=metadata,
        )

        # plan-git-truth chunk 02b/4: close one unit of the docs
        # ledger that github-sync opened with expected_count =
        # uploaded count. last_docs_sha advances only when every
        # vector lands -- critical for HA failover so a mid-flight
        # vectorization is observable in the SHA columns.
        _close_docs_unit(
            workspace_id, source, metadata, status="ok"
        )

        return {
            "status": "success",
            "key": key,
            "doc_type": doc_type,
            "chunks": len(chunks),
            "vectors_stored": vectors_stored,
        }

    except ClientError as e:
        logger.error(f"Error processing document {key}: {e}")
        # plan-git-truth chunk 02b/4: record the per-doc failure on
        # the ledger so try_advance_sha eventually closes the batch
        # (otherwise a single permanent S3/Bedrock failure would
        # wedge the SHA forever).
        _close_docs_unit(
            workspace_id,
            source,
            metadata,
            status="failed",
            file_path=key,
            failure_reason=e.response.get("Error", {}).get(
                "Code", "process_documents_client_error"
            ),
            failure_detail=str(e)[:1024],
        )
        return {"status": "error", "key": key, "error": str(e)}


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    S3-triggered handler for document processing.

    Triggered by ObjectCreated events on the knowledge base bucket.

    ADR-019 (license-layer-rollout chunk 04):
    - validate_license() runs once per invocation (fail-closed; raises
      to let S3/SQS DLQ the batch on hard failures).
    - Liveness tracking lives in the connect/disconnect handlers via
      report_repos_snapshot (license layer v2.0.0 desired-state model);
      check_repo_limit is intentionally NOT called here (it ran upstream
      in file-ingestion).
    """
    logger.info(f"Process documents handler invoked")

    # ADR-019 fail-closed license validation. S3/SQS-driven: raise on
    # failure so the event is retried / DLQ'd rather than silently dropped.
    try:
        from outcomeops_license import validate_license

        claims = validate_license()
        logger.info(
            f"[process-documents] License validated: {claims.get('sub')}"
        )
    except ImportError:
        logger.error(
            "[process-documents] License layer not attached - deploy bug (ADR-019)"
        )
        log_license_fail_open(
            os.environ.get("AUDIT_TABLE", ""), "process-documents"
        )
        raise
    except Exception as e:
        logger.error(f"[process-documents] License validation failed: {e}")
        raise

    results = []

    for record in event.get("Records", []):
        # Handle S3 event
        if "s3" in record:
            bucket = record["s3"]["bucket"]["name"]
            key = unquote_plus(record["s3"]["object"]["key"])

            # Skip non-document paths
            if not key.startswith("workspaces/"):
                logger.debug(f"Skipping non-workspace key: {key}")
                continue

            result = process_document(bucket, key)
            results.append(result)

        # Handle SQS event (for reprocessing)
        elif "body" in record:
            try:
                body = json.loads(record["body"])
                bucket = body.get("bucket", KB_BUCKET)
                key = body.get("key")

                if key:
                    result = process_document(bucket, key)
                    results.append(result)
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON in SQS message")

    successful = sum(1 for r in results if r.get("status") == "success")
    logger.info(f"Document processing completed: {successful}/{len(results)} successful")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Document processing completed",
            "results": results,
        }),
    }
