"""
File Ingestion Lambda for OutcomeOps AI Assist.

Central file ingestion service that:
1. Receives normalized messages from FILE_INGESTION_QUEUE
2. Routes to source-specific handlers (Confluence, Jira, Outlook, etc.)
3. Downloads/fetches content from source APIs
4. Uploads normalized files to S3 knowledge base bucket
5. S3 events then trigger process-documents for embedding

Message Format:
{
    "source": "confluence|jira|outlook|github",
    "workspace_id": "ws_xxx",
    "connection_id": "conn_xxx",  # For OAuth token lookup
    "file": { ... },              # Source-specific metadata
    "sync_id": "sync_xxx",        # Optional: batch tracking
    "file_index": 0,              # Optional: index in batch
    "total_files": 100            # Optional: total in batch
}

S3 Path Structure:
    workspaces/{workspace_id}/{source}/{folder_path}/{filename}
"""

import html
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
import yaml
from botocore.exceptions import ClientError

# Add the lambda root to sys.path so the bundled `shared/` package
# resolves both at Lambda runtime (zip layout) and under pytest from the
# repo root. Mirrors process-documents/handler.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import dual_write  # noqa: E402
from shared.audit_writer import log_license_fail_open

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
KB_BUCKET = os.environ.get("KB_BUCKET")

# AWS clients
dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")
ssm_client = boto3.client("ssm")
sts_client = boto3.client("sts")

# Cached config
_oauth_config = None
_microsoft_oauth_config = None
_account_id = None


def _get_account_id() -> str:
    """Return the AWS account ID (cached after first call)."""
    global _account_id
    if _account_id is None:
        _account_id = sts_client.get_caller_identity()["Account"]
    return _account_id


def _load_oauth_config() -> Dict[str, str]:
    """Load Atlassian OAuth configuration from SSM."""
    global _oauth_config
    if _oauth_config is not None:
        return _oauth_config

    prefix = f"/{ENV}/{APP_NAME}/oauth/atlassian"
    try:
        client_id = ssm_client.get_parameter(Name=f"{prefix}/client-id")["Parameter"]["Value"]
        client_secret = ssm_client.get_parameter(
            Name=f"{prefix}/client-secret", WithDecryption=True
        )["Parameter"]["Value"]
        _oauth_config = {"client_id": client_id, "client_secret": client_secret}
        return _oauth_config
    except ClientError as e:
        logger.error(f"Failed to load OAuth config: {e}")
        raise


def _load_microsoft_oauth_config() -> Dict[str, str]:
    """Load Microsoft OAuth configuration from SSM."""
    global _microsoft_oauth_config
    if _microsoft_oauth_config is not None:
        return _microsoft_oauth_config

    prefix = f"/{ENV}/{APP_NAME}/oauth/microsoft"
    try:
        client_id = ssm_client.get_parameter(Name=f"{prefix}/client-id")["Parameter"]["Value"]
        client_secret = ssm_client.get_parameter(
            Name=f"{prefix}/client-secret", WithDecryption=True
        )["Parameter"]["Value"]
        _microsoft_oauth_config = {"client_id": client_id, "client_secret": client_secret}
        return _microsoft_oauth_config
    except ClientError as e:
        logger.error(f"Failed to load Microsoft OAuth config: {e}")
        raise


# ============================================================================
# Confluence Storage Format Parser
# ============================================================================


def confluence_storage_to_html(storage_content: str) -> str:
    """
    Convert Confluence storage format to clean HTML.

    Preserves structure for later text extraction while removing
    Confluence-specific macros.
    """
    if not storage_content:
        return ""

    text = storage_content

    # Remove CDATA sections
    text = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', text, flags=re.DOTALL)

    # Handle code blocks - convert to pre tags
    text = re.sub(
        r'<ac:structured-macro[^>]*ac:name="code"[^>]*>.*?<ac:plain-text-body>(.*?)</ac:plain-text-body>.*?</ac:structured-macro>',
        r'<pre><code>\1</code></pre>',
        text,
        flags=re.DOTALL | re.IGNORECASE
    )

    # Handle info/warning/note panels
    text = re.sub(
        r'<ac:structured-macro[^>]*ac:name="(info|warning|note|tip)"[^>]*>.*?<ac:rich-text-body>(.*?)</ac:rich-text-body>.*?</ac:structured-macro>',
        r'<div class="panel \1">\2</div>',
        text,
        flags=re.DOTALL | re.IGNORECASE
    )

    # Handle expand/collapse
    text = re.sub(
        r'<ac:structured-macro[^>]*ac:name="expand"[^>]*>.*?<ac:rich-text-body>(.*?)</ac:rich-text-body>.*?</ac:structured-macro>',
        r'<div class="expand">\1</div>',
        text,
        flags=re.DOTALL | re.IGNORECASE
    )

    # Remove remaining macros
    text = re.sub(r'<ac:structured-macro[^>]*>.*?</ac:structured-macro>', '', text, flags=re.DOTALL)
    text = re.sub(r'<ac:[^>]*/?>', '', text)
    text = re.sub(r'</ac:[^>]*>', '', text)
    text = re.sub(r'<ri:[^>]*/?>', '', text)
    text = re.sub(r'</ri:[^>]*>', '', text)

    return text


def sanitize_filename(name: str) -> str:
    """Sanitize a string to be safe for use as a filename."""
    # Replace problematic characters
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', '_', name)
    name = name.strip('._')
    return name[:200]  # Limit length


# ============================================================================
# Source Handlers
# ============================================================================


def process_confluence_page(
    workspace_id: str,
    connection_id: str,
    cloud_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process a Confluence page and upload to S3.

    Args:
        workspace_id: Workspace ID
        connection_id: Connection ID for token lookup
        cloud_id: Atlassian Cloud ID
        file_info: Page metadata from queue message
        sync_id: Optional sync tracking ID

    Returns:
        S3 key if successful, None otherwise
    """
    from confluence_api import get_page_content, ConfluenceAPIError
    from token_manager import refresh_access_token

    page_id = file_info.get("page_id")
    space_key = file_info.get("space_key", "")
    space_name = file_info.get("space_name", space_key)
    title = file_info.get("title", f"page_{page_id}")

    logger.info(f"Processing Confluence page: {title} ({page_id})")

    # Get OAuth config and refresh token
    oauth_config = _load_oauth_config()
    access_token = refresh_access_token(
        connection_id=connection_id,
        client_id=oauth_config["client_id"],
        client_secret=oauth_config["client_secret"],
    )

    if not access_token:
        logger.error(f"Failed to refresh token for connection {connection_id}")
        return None

    try:
        # Fetch page content
        page = get_page_content(access_token, cloud_id, page_id)

        # Extract storage format content
        body = page.get("body", {})
        storage_content = body.get("storage", {}).get("value", "")

        if not storage_content:
            logger.warning(f"Empty content for page {page_id}")
            return None

        # Convert to HTML
        html_content = confluence_storage_to_html(storage_content)

        # Build full HTML document
        page_title = page.get("title", title)
        full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{html.escape(page_title)}</title>
</head>
<body>
<h1>{html.escape(page_title)}</h1>
{html_content}
</body>
</html>"""

        # Build S3 path
        safe_space = sanitize_filename(space_name)
        safe_title = sanitize_filename(page_title)
        filename = f"{safe_title}_{page_id}.html"
        s3_key = f"workspaces/{workspace_id}/confluence/{safe_space}/{filename}"

        # Upload to S3
        metadata = {
            "source": "confluence",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "confluence_page_id": page_id,
            "confluence_space_key": space_key,
            "confluence_version": str(page.get("version", {}).get("number", 1)),
        }

        if sync_id:
            metadata["sync_id"] = sync_id

        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=full_html.encode("utf-8"),
            ContentType="text/html",
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )

        logger.info(f"Uploaded Confluence page to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except ConfluenceAPIError as e:
        logger.error(f"Confluence API error for page {page_id}: {e}")
        return None
    except ClientError as e:
        logger.error(f"S3 error uploading page {page_id}: {e}")
        return None


def process_confluence_attachment(
    workspace_id: str,
    connection_id: str,
    cloud_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process a Confluence attachment and upload to S3.

    Args:
        workspace_id: Workspace ID
        connection_id: Connection ID for token lookup
        cloud_id: Atlassian Cloud ID
        file_info: Attachment metadata
        sync_id: Optional sync tracking ID

    Returns:
        S3 key if successful, None otherwise
    """
    from confluence_api import download_attachment, ConfluenceAPIError
    from token_manager import refresh_access_token

    attachment_id = file_info.get("attachment_id")
    page_id = file_info.get("page_id")
    space_name = file_info.get("space_name", "")
    filename = file_info.get("filename", f"attachment_{attachment_id}")

    logger.info(f"Processing Confluence attachment: {filename} ({attachment_id})")

    # Get OAuth config and refresh token
    oauth_config = _load_oauth_config()
    access_token = refresh_access_token(
        connection_id=connection_id,
        client_id=oauth_config["client_id"],
        client_secret=oauth_config["client_secret"],
    )

    if not access_token:
        logger.error(f"Failed to refresh token for connection {connection_id}")
        return None

    try:
        # Download attachment content
        content = download_attachment(access_token, cloud_id, attachment_id, page_id=page_id)

        # Build S3 path
        safe_space = sanitize_filename(space_name)
        safe_filename = sanitize_filename(filename)
        s3_key = f"workspaces/{workspace_id}/confluence/{safe_space}/{page_id}/attachments/{safe_filename}"

        # Determine content type
        content_type = "application/octet-stream"
        if filename.lower().endswith(".pdf"):
            content_type = "application/pdf"
        elif filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif")):
            content_type = f"image/{filename.split('.')[-1].lower()}"

        # Upload to S3
        metadata = {
            "source": "confluence_attachment",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "confluence_attachment_id": attachment_id,
            "confluence_page_id": page_id,
        }

        if sync_id:
            metadata["sync_id"] = sync_id

        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content,
            ContentType=content_type,
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )

        logger.info(f"Uploaded Confluence attachment to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except ConfluenceAPIError as e:
        logger.error(f"Confluence API error for attachment {attachment_id}: {e}")
        return None
    except ClientError as e:
        logger.error(f"S3 error uploading attachment {attachment_id}: {e}")
        return None


# ============================================================================
# Jira Handlers
# ============================================================================


def jira_description_to_html(description: Any) -> str:
    """
    Convert Jira Atlassian Document Format (ADF) to HTML.

    ADF is a JSON structure used by Jira Cloud for rich text content.
    """
    if not description:
        return ""

    if isinstance(description, str):
        return f"<p>{html.escape(description)}</p>"

    if not isinstance(description, dict):
        return ""

    def render_node(node: Dict) -> str:
        node_type = node.get("type", "")
        content = node.get("content", [])
        text = node.get("text", "")
        marks = node.get("marks", [])

        # Handle text nodes
        if node_type == "text":
            result = html.escape(text)
            for mark in marks:
                mark_type = mark.get("type", "")
                if mark_type == "strong":
                    result = f"<strong>{result}</strong>"
                elif mark_type == "em":
                    result = f"<em>{result}</em>"
                elif mark_type == "code":
                    result = f"<code>{result}</code>"
                elif mark_type == "link":
                    href = mark.get("attrs", {}).get("href", "")
                    result = f'<a href="{html.escape(href)}">{result}</a>'
            return result

        # Handle block nodes
        children = "".join(render_node(child) for child in content)

        if node_type == "paragraph":
            return f"<p>{children}</p>"
        elif node_type == "heading":
            level = node.get("attrs", {}).get("level", 1)
            return f"<h{level}>{children}</h{level}>"
        elif node_type == "bulletList":
            return f"<ul>{children}</ul>"
        elif node_type == "orderedList":
            return f"<ol>{children}</ol>"
        elif node_type == "listItem":
            return f"<li>{children}</li>"
        elif node_type == "codeBlock":
            return f"<pre><code>{children}</code></pre>"
        elif node_type == "blockquote":
            return f"<blockquote>{children}</blockquote>"
        elif node_type == "hardBreak":
            return "<br/>"
        elif node_type == "doc":
            return children
        else:
            return children

    return render_node(description)


def process_jira_issue(
    workspace_id: str,
    connection_id: str,
    cloud_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process a Jira issue and upload to S3.

    The issue data is already in file_info (fetched by jira-sync),
    so we just convert to HTML and upload.

    Args:
        workspace_id: Workspace ID
        connection_id: Connection ID
        cloud_id: Atlassian Cloud ID
        file_info: Issue data from queue message
        sync_id: Optional sync tracking ID

    Returns:
        S3 key if successful, None otherwise
    """
    issue_id = file_info.get("issue_id")
    issue_key = file_info.get("issue_key", f"issue_{issue_id}")
    project_key = file_info.get("project_key", "")
    project_name = file_info.get("project_name", project_key)
    summary = file_info.get("summary", "")
    description = file_info.get("description")
    status = file_info.get("status", "Unknown")
    priority = file_info.get("priority")
    issue_type = file_info.get("issue_type", "Unknown")
    assignee = file_info.get("assignee")
    reporter = file_info.get("reporter")
    labels = file_info.get("labels", [])
    created = file_info.get("created", "")
    updated = file_info.get("updated", "")
    comments = file_info.get("comments", [])

    logger.info(f"Processing Jira issue: {issue_key} - {summary}")

    try:
        # Convert description to HTML
        description_html = jira_description_to_html(description)

        # Build comments HTML
        comments_html = ""
        if comments:
            comments_html = "<h2>Comments</h2>"
            for comment in comments:
                author = comment.get("author", {}).get("displayName", "Unknown")
                body = comment.get("body", {})
                comment_text = jira_description_to_html(body)
                comment_created = comment.get("created", "")
                comments_html += f"""
<div class="comment">
    <div class="comment-author"><strong>{html.escape(author)}</strong> - {html.escape(comment_created)}</div>
    <div class="comment-body">{comment_text}</div>
</div>
"""

        # Build metadata section
        meta_items = []
        if status:
            meta_items.append(f"<li><strong>Status:</strong> {html.escape(status)}</li>")
        if issue_type:
            meta_items.append(f"<li><strong>Type:</strong> {html.escape(issue_type)}</li>")
        if priority:
            meta_items.append(f"<li><strong>Priority:</strong> {html.escape(priority)}</li>")
        if assignee:
            meta_items.append(f"<li><strong>Assignee:</strong> {html.escape(assignee)}</li>")
        if reporter:
            meta_items.append(f"<li><strong>Reporter:</strong> {html.escape(reporter)}</li>")
        if labels:
            meta_items.append(f"<li><strong>Labels:</strong> {html.escape(', '.join(labels))}</li>")
        if created:
            meta_items.append(f"<li><strong>Created:</strong> {html.escape(created)}</li>")
        if updated:
            meta_items.append(f"<li><strong>Updated:</strong> {html.escape(updated)}</li>")

        meta_html = f"<ul>{''.join(meta_items)}</ul>" if meta_items else ""

        # Build full HTML document
        full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{html.escape(issue_key)}: {html.escape(summary)}</title>
</head>
<body>
<h1>[{html.escape(issue_key)}] {html.escape(summary)}</h1>
<div class="issue-meta">
{meta_html}
</div>
<h2>Description</h2>
<div class="description">
{description_html if description_html else "<p><em>No description provided</em></p>"}
</div>
{comments_html}
</body>
</html>"""

        # Build S3 path
        safe_project = sanitize_filename(project_name)
        filename = f"{issue_key}.html"
        s3_key = f"workspaces/{workspace_id}/jira/{safe_project}/{filename}"

        # Upload to S3
        metadata = {
            "source": "jira",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "jira_issue_id": str(issue_id),
            "jira_issue_key": issue_key,
            "jira_project_key": project_key,
        }

        if sync_id:
            metadata["sync_id"] = sync_id

        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=full_html.encode("utf-8"),
            ContentType="text/html",
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )

        logger.info(f"Uploaded Jira issue to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except ClientError as e:
        logger.error(f"S3 error uploading issue {issue_key}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing issue {issue_key}: {e}")
        return None


# ============================================================================
# Azure DevOps Boards handler
# ============================================================================


def process_azure_devops_boards_work_item(
    workspace_id: str,
    connection_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """Process an Azure DevOps Boards work item and upload to S3.

    The work-item data is already in ``file_info`` (fetched by the
    azure-devops-boards-sync worker) as a flat structure -- no ADO
    envelope parsing required here. Sections with no content
    (empty description, no comments) are omitted from the rendered
    document to keep the output concise.

    Deterministic S3 key
    (``workspaces/{ws}/azure_devops_boards/{org}/{project}/WI-{id}.html``)
    so a re-sync updates the existing object rather than duplicating.
    """
    org = file_info.get("org", "")
    project = file_info.get("project", "")
    wi_id = file_info.get("id")
    if wi_id is None:
        logger.error("Boards work item missing id; skipping")
        return None
    title = file_info.get("title", "")
    work_item_type = file_info.get("work_item_type", "Unknown")
    state = file_info.get("state", "Unknown")
    reason = file_info.get("reason")
    assigned_to = file_info.get("assigned_to")
    created_date = file_info.get("created_date", "")
    changed_date = file_info.get("changed_date", "")
    priority = file_info.get("priority")
    severity = file_info.get("severity")
    tags = file_info.get("tags")
    area_path = file_info.get("area_path")
    iteration_path = file_info.get("iteration_path")
    description = file_info.get("description")
    repro_steps = file_info.get("repro_steps")
    acceptance_criteria = file_info.get("acceptance_criteria")
    url = file_info.get("url")
    comments = file_info.get("comments") or []

    logger.info(f"Processing ADO Boards work item {org}/{project}/{wi_id}: {title}")

    try:
        # Metadata rows -- only render fields with a value so the doc
        # doesn't fill up with "None" placeholders.
        meta_items = []
        if state:
            state_line = html.escape(state)
            if reason:
                state_line += f" ({html.escape(reason)})"
            meta_items.append(f"<li><strong>State:</strong> {state_line}</li>")
        if work_item_type:
            meta_items.append(
                f"<li><strong>Type:</strong> {html.escape(work_item_type)}</li>"
            )
        if priority is not None:
            meta_items.append(
                f"<li><strong>Priority:</strong> {html.escape(str(priority))}</li>"
            )
        if severity:
            meta_items.append(
                f"<li><strong>Severity:</strong> {html.escape(str(severity))}</li>"
            )
        if assigned_to:
            meta_items.append(
                f"<li><strong>Assigned To:</strong> {html.escape(str(assigned_to))}</li>"
            )
        if tags:
            meta_items.append(
                f"<li><strong>Tags:</strong> {html.escape(str(tags))}</li>"
            )
        if area_path:
            meta_items.append(
                f"<li><strong>Area:</strong> {html.escape(str(area_path))}</li>"
            )
        if iteration_path:
            meta_items.append(
                f"<li><strong>Iteration:</strong> {html.escape(str(iteration_path))}</li>"
            )
        if created_date:
            meta_items.append(
                f"<li><strong>Created:</strong> {html.escape(created_date)}</li>"
            )
        if changed_date:
            meta_items.append(
                f"<li><strong>Changed:</strong> {html.escape(changed_date)}</li>"
            )
        if url:
            meta_items.append(
                f'<li><strong>URL:</strong> <a href="{html.escape(url)}">'
                f'{html.escape(url)}</a></li>'
            )

        meta_html = f"<ul>{''.join(meta_items)}</ul>" if meta_items else ""

        # ADO rich-text fields come through as HTML; pass through directly.
        # Empty fields skip their whole section to keep the doc tight.
        sections_html = ""
        if description:
            sections_html += (
                "<h2>Description</h2>"
                f'<div class="description">{description}</div>'
            )
        if repro_steps:
            sections_html += (
                "<h2>Repro Steps</h2>"
                f'<div class="repro-steps">{repro_steps}</div>'
            )
        if acceptance_criteria:
            sections_html += (
                "<h2>Acceptance Criteria</h2>"
                f'<div class="acceptance-criteria">{acceptance_criteria}</div>'
            )

        comments_html = ""
        if comments:
            comments_html = "<h2>Comments</h2>"
            for comment in comments:
                created_by = comment.get("createdBy") or {}
                author = (
                    created_by.get("displayName")
                    if isinstance(created_by, dict)
                    else str(created_by)
                ) or "Unknown"
                created = comment.get("createdDate", "")
                text = comment.get("text") or ""
                comments_html += (
                    '<div class="comment">'
                    f'<div class="comment-author"><strong>{html.escape(str(author))}</strong> '
                    f'- {html.escape(str(created))}</div>'
                    f'<div class="comment-body">{text}</div>'
                    '</div>'
                )

        full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>WI-{html.escape(str(wi_id))}: {html.escape(title)}</title>
</head>
<body>
<h1>[WI-{html.escape(str(wi_id))}] {html.escape(title)}</h1>
<div class="work-item-meta">
{meta_html}
</div>
{sections_html}
{comments_html}
</body>
</html>"""

        safe_org = sanitize_filename(org)
        safe_project = sanitize_filename(project)
        s3_key = (
            f"workspaces/{workspace_id}/azure_devops_boards/"
            f"{safe_org}/{safe_project}/WI-{wi_id}.html"
        )

        metadata = {
            "source": "azure_devops_boards",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "azdo_boards_org": org[:128],
            "azdo_boards_project": project[:128],
            "azdo_boards_work_item_id": str(wi_id),
        }
        if sync_id:
            metadata["sync_id"] = sync_id

        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=full_html.encode("utf-8"),
            ContentType="text/html",
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )

        logger.info(f"Uploaded ADO Boards work item to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except ClientError as e:
        logger.error(f"S3 error uploading work item {wi_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing work item {wi_id}: {e}")
        return None


def process_jira_attachment(
    workspace_id: str,
    connection_id: str,
    cloud_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process a Jira attachment and upload to S3.

    Args:
        workspace_id: Workspace ID
        connection_id: Connection ID for token lookup
        cloud_id: Atlassian Cloud ID
        file_info: Attachment metadata
        sync_id: Optional sync tracking ID

    Returns:
        S3 key if successful, None otherwise
    """
    from jira_api import download_attachment, JiraAPIError
    from jira_token_manager import refresh_access_token

    attachment_id = file_info.get("attachment_id")
    issue_key = file_info.get("issue_key")
    project_name = file_info.get("project_name", "")
    filename = file_info.get("filename", f"attachment_{attachment_id}")

    logger.info(f"Processing Jira attachment: {filename} ({attachment_id}) from {issue_key}")

    # Get OAuth config and refresh token
    oauth_config = _load_oauth_config()
    access_token = refresh_access_token(
        connection_id=connection_id,
        client_id=oauth_config["client_id"],
        client_secret=oauth_config["client_secret"],
    )

    if not access_token:
        logger.error(f"Failed to refresh token for connection {connection_id}")
        return None

    try:
        # Download attachment content
        content = download_attachment(access_token, cloud_id, attachment_id)

        # Build S3 path
        safe_project = sanitize_filename(project_name)
        safe_filename = sanitize_filename(filename)
        s3_key = f"workspaces/{workspace_id}/jira/{safe_project}/{issue_key}/attachments/{safe_filename}"

        # Determine content type
        content_type = "application/octet-stream"
        if filename.lower().endswith(".pdf"):
            content_type = "application/pdf"
        elif filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif")):
            content_type = f"image/{filename.split('.')[-1].lower()}"

        # Upload to S3
        metadata = {
            "source": "jira_attachment",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "jira_attachment_id": str(attachment_id),
            "jira_issue_key": issue_key,
        }

        if sync_id:
            metadata["sync_id"] = sync_id

        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content,
            ContentType=content_type,
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )

        logger.info(f"Uploaded Jira attachment to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except JiraAPIError as e:
        logger.error(f"Jira API error for attachment {attachment_id}: {e}")
        return None
    except ClientError as e:
        logger.error(f"S3 error uploading attachment {attachment_id}: {e}")
        return None


# ============================================================================
# Outlook Handlers
# ============================================================================


def process_outlook_email(
    workspace_id: str,
    connection_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process an Outlook email and upload to S3.

    The email body is already in file_info (fetched by outlook-sync),
    so we just wrap it in HTML and upload.

    Args:
        workspace_id: Workspace ID
        connection_id: Connection ID
        file_info: Email data from queue message
        sync_id: Optional sync tracking ID

    Returns:
        S3 key if successful, None otherwise
    """
    message_id = file_info.get("message_id", "")
    subject = file_info.get("subject", "No Subject")
    folder_name = file_info.get("folder_name", "Inbox")
    from_addr = file_info.get("from", "")
    received_at = file_info.get("received_at", "")
    body_content = file_info.get("body_content", "")
    body_content_type = file_info.get("body_content_type", "html")

    logger.info(f"Processing Outlook email: {subject} ({message_id[:20]}...)")

    if not body_content:
        logger.warning(f"Empty body for email {message_id}")
        # Still upload with metadata for subject-based search
        body_content = "<p><em>No content</em></p>"

    try:
        # Build full HTML document
        if body_content_type == "text":
            body_html = f"<pre>{html.escape(body_content)}</pre>"
        else:
            body_html = body_content

        full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{html.escape(subject)}</title>
</head>
<body>
<h1>{html.escape(subject)}</h1>
<div class="email-meta">
<ul>
<li><strong>From:</strong> {html.escape(from_addr)}</li>
<li><strong>Date:</strong> {html.escape(received_at)}</li>
<li><strong>Folder:</strong> {html.escape(folder_name)}</li>
</ul>
</div>
<div class="email-body">
{body_html}
</div>
</body>
</html>"""

        # Build S3 path
        safe_folder = sanitize_filename(folder_name)
        safe_subject = sanitize_filename(subject)
        # Use last 12 chars of message_id for uniqueness
        short_id = message_id[-12:] if len(message_id) > 12 else message_id
        filename = f"{safe_subject}_{short_id}.html"
        s3_key = f"workspaces/{workspace_id}/outlook/{safe_folder}/{filename}"

        # Upload to S3
        metadata = {
            "source": "outlook",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "outlook_message_id": message_id[:128],
            "outlook_folder": folder_name,
        }

        if sync_id:
            metadata["sync_id"] = sync_id

        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=full_html.encode("utf-8"),
            ContentType="text/html",
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )

        logger.info(f"Uploaded Outlook email to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except ClientError as e:
        logger.error(f"S3 error uploading email {message_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing email {message_id}: {e}")
        return None


def process_outlook_attachment(
    workspace_id: str,
    connection_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process an Outlook email attachment and upload to S3.

    Args:
        workspace_id: Workspace ID
        connection_id: Connection ID for token lookup
        file_info: Attachment metadata
        sync_id: Optional sync tracking ID

    Returns:
        S3 key if successful, None otherwise
    """
    from outlook_api import download_attachment, OutlookAPIError
    from outlook_token_manager import refresh_access_token

    attachment_id = file_info.get("attachment_id")
    message_id = file_info.get("message_id")
    folder_name = file_info.get("folder_name", "Inbox")
    filename = file_info.get("filename", f"attachment_{attachment_id}")

    logger.info(f"Processing Outlook attachment: {filename} ({attachment_id})")

    # Get Microsoft OAuth config and refresh token
    oauth_config = _load_microsoft_oauth_config()
    access_token = refresh_access_token(
        connection_id=connection_id,
        client_id=oauth_config["client_id"],
        client_secret=oauth_config["client_secret"],
    )

    if not access_token:
        logger.error(f"Failed to refresh token for connection {connection_id}")
        return None

    try:
        # Download attachment content
        content = download_attachment(access_token, message_id, attachment_id)

        # Build S3 path
        safe_folder = sanitize_filename(folder_name)
        safe_filename = sanitize_filename(filename)
        short_msg_id = message_id[-12:] if len(message_id) > 12 else message_id
        s3_key = f"workspaces/{workspace_id}/outlook/{safe_folder}/{short_msg_id}/attachments/{safe_filename}"

        # Determine content type
        content_type = file_info.get("content_type", "application/octet-stream")

        # Upload to S3
        metadata = {
            "source": "outlook_attachment",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "outlook_attachment_id": attachment_id[:128] if attachment_id else "",
            "outlook_message_id": message_id[:128] if message_id else "",
        }

        if sync_id:
            metadata["sync_id"] = sync_id

        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content,
            ContentType=content_type,
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )

        logger.info(f"Uploaded Outlook attachment to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except OutlookAPIError as e:
        logger.error(f"Outlook API error for attachment {attachment_id}: {e}")
        return None
    except ClientError as e:
        logger.error(f"S3 error uploading attachment {attachment_id}: {e}")
        return None


# ============================================================================
# Teams Handlers
# ============================================================================


def process_teams_message(
    workspace_id: str,
    connection_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process a Teams message and upload to S3.

    The message HTML body is already in file_info (built by teams-sync).
    """
    message_id = file_info.get("message_id", "")
    channel_name = file_info.get("channel_name", "General")
    body_content = file_info.get("body_content", "")
    created_at = file_info.get("created_at", "")

    logger.info(f"Processing Teams message: {channel_name}/{message_id[:20]}...")

    if not body_content:
        logger.warning(f"Empty body for Teams message {message_id}")
        body_content = "<html><body><p><em>No content</em></p></body></html>"

    try:
        safe_channel = sanitize_filename(channel_name)
        short_id = message_id[-12:] if len(message_id) > 12 else message_id
        filename = f"{short_id}.html"
        s3_key = f"workspaces/{workspace_id}/teams/{safe_channel}/{filename}"

        metadata = {
            "source": "teams",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "teams_message_id": message_id[:128],
            "teams_channel": channel_name,
        }

        if sync_id:
            metadata["sync_id"] = sync_id

        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=body_content.encode("utf-8"),
            ContentType="text/html",
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )

        logger.info(f"Uploaded Teams message to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except ClientError as e:
        logger.error(f"S3 error uploading Teams message {message_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing Teams message {message_id}: {e}")
        return None


def _teams_file_unchanged(s3_key: str, graph_etag: str) -> bool:
    """head_object + teams_etag metadata check.

    Fail-safe contract: return False on any error path so the caller
    falls through to the download+put flow. Never silently drop a
    real change on a broken check. Only 404 (NoSuchKey / NotFound)
    is treated as "not yet uploaded, proceed with put"; other
    error codes get a warning log and still proceed.
    """
    try:
        resp = s3_client.head_object(
            Bucket=KB_BUCKET, Key=s3_key,
            ExpectedBucketOwner=_get_account_id(),
        )
        stored_etag = (resp.get("Metadata") or {}).get("teams_etag")
        return bool(stored_etag) and stored_etag == graph_etag
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in ("404", "NoSuchKey", "NotFound"):
            logger.warning(
                f"[teams-file-skip] head_object failed for {s3_key}: "
                f"{code}; falling through to put"
            )
        return False


def process_teams_file(
    workspace_id: str,
    connection_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process a Teams channel file and upload to S3.

    Downloads from pre-authenticated URL provided in file_info.

    Chunk 03: etag safety net. When the SQS body carries a Graph
    ``etag`` and the existing S3 object's ``teams_etag`` metadata
    matches, skip the download + put + downstream re-embedding.
    Fail-safe on any head_object error (falls through to the
    existing download+put path).
    """
    import urllib.error
    import urllib.request

    channel_name = file_info.get("channel_name", "General")
    filename = file_info.get("filename", "")
    download_url = file_info.get("download_url", "")
    mime_type = file_info.get("mime_type", "application/octet-stream")
    item_id = file_info.get("item_id", "")
    graph_etag = file_info.get("etag", "") or ""

    logger.info(f"Processing Teams file: {channel_name}/{filename}")

    if not download_url:
        logger.error(f"No download URL for Teams file {filename}")
        return None

    safe_channel = sanitize_filename(channel_name)
    safe_filename = sanitize_filename(filename)
    s3_key = f"workspaces/{workspace_id}/teams/{safe_channel}/files/{safe_filename}"

    # Etag pre-flight (chunk 03). Only runs when Graph gave us an etag
    # to compare against; empty means "we can't prove no change" and
    # we take the download+put path.
    if graph_etag and _teams_file_unchanged(s3_key, graph_etag):
        logger.info(
            f"[teams-file-skip] {s3_key} unchanged "
            f"(etag={graph_etag[:12]}...); short-circuiting"
        )
        return s3_key

    try:
        request = urllib.request.Request(download_url)
        with urllib.request.urlopen(request, timeout=300) as response:
            content = response.read()

        metadata = {
            "source": "teams_file",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "teams_channel": channel_name,
            "teams_item_id": item_id[:128] if item_id else "",
        }

        if graph_etag:
            metadata["teams_etag"] = graph_etag[:256]

        if sync_id:
            metadata["sync_id"] = sync_id

        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content,
            ContentType=mime_type,
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )

        logger.info(
            f"[teams-file-put] {s3_key} uploaded "
            f"(etag={graph_etag[:12] if graph_etag else 'none'}...); "
            f"size={len(content)}"
        )
        return s3_key

    except urllib.error.HTTPError as e:
        logger.error(f"Download error for Teams file {filename}: {e.code}")
        return None
    except ClientError as e:
        logger.error(f"S3 error uploading Teams file {filename}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing Teams file {filename}: {e}")
        return None


# ============================================================================
# SharePoint Handlers
# ============================================================================


def process_sharepoint_file(
    workspace_id: str,
    connection_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process a SharePoint file and upload to S3.

    Downloads from pre-authenticated @microsoft.graph.downloadUrl.
    """
    import urllib.error
    import urllib.request

    site_name = file_info.get("site_name", "Unknown")
    filename = file_info.get("filename", "")
    download_url = file_info.get("download_url", "")
    mime_type = file_info.get("mime_type", "application/octet-stream")
    item_id = file_info.get("item_id", "")

    logger.info(f"Processing SharePoint file: {site_name}/{filename}")

    if not download_url:
        logger.error(f"No download URL for SharePoint file {filename}")
        return None

    try:
        request = urllib.request.Request(download_url)
        with urllib.request.urlopen(request, timeout=300) as response:
            content = response.read()

        safe_site = sanitize_filename(site_name)
        safe_filename = sanitize_filename(filename)
        s3_key = f"workspaces/{workspace_id}/sharepoint/{safe_site}/{safe_filename}"

        metadata = {
            "source": "sharepoint",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "sharepoint_site": site_name,
            "sharepoint_item_id": item_id[:128] if item_id else "",
        }

        if sync_id:
            metadata["sync_id"] = sync_id

        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content,
            ContentType=mime_type,
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )

        logger.info(f"Uploaded SharePoint file to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except urllib.error.HTTPError as e:
        logger.error(f"Download error for SharePoint file {filename}: {e.code}")
        return None
    except ClientError as e:
        logger.error(f"S3 error uploading SharePoint file {filename}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing SharePoint file {filename}: {e}")
        return None


def process_onedrive_file(
    workspace_id: str,
    connection_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process a OneDrive file and upload to S3.

    Downloads from pre-authenticated @microsoft.graph.downloadUrl. Stores
    under workspaces/{ws}/onedrive/{connection_id}/{folder_path}/{filename}
    so multiple OneDrive connections in the same workspace don't collide.
    """
    import urllib.error
    import urllib.request

    drive_name = file_info.get("drive_name", "OneDrive")
    folder_path = file_info.get("folder_path", "")
    filename = file_info.get("filename", "")
    download_url = file_info.get("download_url", "")
    mime_type = file_info.get("mime_type", "application/octet-stream")
    item_id = file_info.get("item_id", "")
    drive_id = file_info.get("drive_id", "")

    logger.info(f"Processing OneDrive file: {drive_name}{folder_path}/{filename}")

    if not download_url:
        logger.error(f"No download URL for OneDrive file {filename}")
        return None

    try:
        request = urllib.request.Request(download_url)
        with urllib.request.urlopen(request, timeout=300) as response:
            content = response.read()

        # Sanitize each segment of folder_path independently so the
        # original directory structure is preserved as a hierarchy in S3.
        safe_folder_parts = [
            sanitize_filename(p) for p in folder_path.strip("/").split("/") if p
        ]
        safe_filename = sanitize_filename(filename)

        if safe_folder_parts:
            s3_key = f"workspaces/{workspace_id}/onedrive/{connection_id}/{'/'.join(safe_folder_parts)}/{safe_filename}"
        else:
            s3_key = f"workspaces/{workspace_id}/onedrive/{connection_id}/{safe_filename}"

        metadata = {
            "source": "onedrive",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "onedrive_drive_id": drive_id[:128] if drive_id else "",
            "onedrive_item_id": item_id[:128] if item_id else "",
            "onedrive_folder_path": folder_path[:1024] if folder_path else "",
        }

        if sync_id:
            metadata["sync_id"] = sync_id

        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content,
            ContentType=mime_type,
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )

        logger.info(f"Uploaded OneDrive file to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except urllib.error.HTTPError as e:
        logger.error(f"Download error for OneDrive file {filename}: {e.code}")
        return None
    except ClientError as e:
        logger.error(f"S3 error uploading OneDrive file {filename}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing OneDrive file {filename}: {e}")
        return None


def process_onenote_page(
    workspace_id: str,
    connection_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process a OneNote page and upload to S3.

    Unlike OneDrive (download from pre-authenticated URL), OneNote
    page content arrives inline in the SQS message body as HTML.
    onenote-sync already fetched it from Graph - this just uploads.

    S3 key shape:
      workspaces/{ws}/onenote/{conn_id}/{notebook_id}/{section_id}/{page_id}.html

    Per-connection prefix + page_id in the filename so orphan
    detection (delete-worker, source=onenote) can identify pages
    by parsing the key directly - no HeadObject needed like OneDrive.
    """
    page_id = file_info.get("page_id", "")
    title = file_info.get("title", "Untitled")
    notebook_id = file_info.get("notebook_id", "")
    notebook_name = file_info.get("notebook_name", "")
    section_id = file_info.get("section_id", "")
    section_name = file_info.get("section_name", "")
    last_modified = file_info.get("last_modified", "")
    content_html = file_info.get("content_html", "")

    logger.info(f"Processing OneNote page: {notebook_name}/{section_name}/{title}")

    if not page_id:
        logger.error(f"OneNote page missing page_id: {title}")
        return None

    if not content_html or len(content_html.strip()) < 10:
        logger.warning(f"OneNote page {page_id} has empty content; skipping")
        return None

    # Sanitize ID segments: Graph returns IDs that occasionally contain
    # URL-unsafe chars (=, +, /). Keep them readable but S3-safe.
    safe_notebook = sanitize_filename(notebook_id) or "_default"
    safe_section = sanitize_filename(section_id) or "_default"
    safe_page = sanitize_filename(page_id)

    s3_key = f"workspaces/{workspace_id}/onenote/{connection_id}/{safe_notebook}/{safe_section}/{safe_page}.html"

    metadata = {
        "source": "onenote",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "onenote_page_id": page_id[:128] if page_id else "",
        "onenote_notebook_id": notebook_id[:128] if notebook_id else "",
        "onenote_section_id": section_id[:128] if section_id else "",
        "onenote_notebook_name": (notebook_name or "")[:128],
        "onenote_section_name": (section_name or "")[:128],
        "onenote_title": (title or "")[:128],
        "onenote_last_modified": last_modified or "",
    }
    if sync_id:
        metadata["sync_id"] = sync_id

    try:
        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content_html.encode("utf-8"),
            ContentType="text/html",
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )
        logger.info(f"Uploaded OneNote page to s3://{KB_BUCKET}/{s3_key}")
        return s3_key
    except ClientError as e:
        logger.error(f"S3 error uploading OneNote page {page_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing OneNote page {page_id}: {e}")
        return None


def _strip_top_values(doc: Any) -> Any:
    """Recursively remove any ``top_values`` key from a parsed YAML doc.

    Defensive backstop to the strip already performed in database-sync.
    Even if a future contributor regresses the upstream pass, this gate
    ensures column-value leakage never lands in S3. The two
    implementations share the same shape; tests in chunk 05 keep them
    in sync.
    """
    if isinstance(doc, dict):
        doc.pop("top_values", None)
        for v in doc.values():
            _strip_top_values(v)
    elif isinstance(doc, list):
        for item in doc:
            _strip_top_values(item)
    return doc


def process_database_artifact(
    workspace_id: str,
    connection_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process a per-table YAML chunk from schema introspection and
    upload to S3.

    S3 key shape:
      workspaces/{ws}/database/{conn_id}/{catalog}/{schema}/{table_id}.yaml

    The table_id is `catalog.schema.table` sanitized via
    sanitize_filename — matches what database-sync stores in
    known_table_ids so delete-worker's orphan check is a direct stem
    match (same pattern as OneNote pages).

    Defensive top_values strip runs here AGAIN even though
    database-sync already stripped on the way into SQS. Two gates
    keep column-value data out of S3 if the upstream pass ever
    regresses.
    """
    engine = file_info.get("engine", "")
    host_hash = file_info.get("host_hash", "")
    catalog = file_info.get("catalog", "")
    schema = file_info.get("schema", "")
    table_name = file_info.get("table", "")
    table_id = file_info.get("table_id", "")
    yaml_content = file_info.get("yaml_content", "")

    logger.info(
        f"Processing database artifact: {engine} {catalog}.{schema}.{table_name}"
    )

    if not table_id:
        logger.error(f"Database artifact missing table_id: {table_name}")
        return None

    if not yaml_content or len(yaml_content.strip()) < 10:
        logger.warning(f"Database artifact for {table_id} has empty yaml; skipping")
        return None

    # Defensive top_values strip. yaml round-trip rather than regex so
    # we don't accidentally mangle column values that legitimately
    # contain the string "top_values".
    try:
        doc = yaml.safe_load(yaml_content)
        _strip_top_values(doc)
        clean_yaml = yaml.dump(doc, sort_keys=False)
    except yaml.YAMLError as e:
        logger.error(f"Failed to parse YAML for {table_id}: {e}")
        return None

    # Sanitize ID segments independently so the hierarchy is preserved
    # while OS-unsafe chars are scrubbed. Same per-segment treatment
    # as process_onedrive_file / process_onenote_page.
    safe_catalog = sanitize_filename(catalog) or "_default"
    safe_schema = sanitize_filename(schema) or "_default"
    safe_table_id = sanitize_filename(table_id)

    s3_key = (
        f"workspaces/{workspace_id}/database/{connection_id}/"
        f"{safe_catalog}/{safe_schema}/{safe_table_id}.yaml"
    )

    metadata = {
        "source": "database",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "database_engine": engine[:128],
        "database_host_hash": host_hash[:128] if host_hash else "",
        "database_catalog": (catalog or "")[:128],
        "database_schema": (schema or "")[:128],
        "database_table": (table_name or "")[:128],
        "database_table_id": table_id[:128],
    }
    if sync_id:
        metadata["sync_id"] = sync_id

    try:
        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=clean_yaml.encode("utf-8"),
            ContentType="application/yaml",
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )
        logger.info(f"Uploaded database artifact to s3://{KB_BUCKET}/{s3_key}")
        return s3_key
    except ClientError as e:
        logger.error(f"S3 error uploading database artifact {table_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing database artifact {table_id}: {e}")
        return None


def process_sharepoint_page(
    workspace_id: str,
    connection_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process a SharePoint page and upload to S3.

    Page text content is already extracted and in file_info.
    """
    site_name = file_info.get("site_name", "Unknown")
    title = file_info.get("title", "Untitled")
    content = file_info.get("content", "")
    page_id = file_info.get("page_id", "")
    web_url = file_info.get("web_url", "")

    logger.info(f"Processing SharePoint page: {site_name}/{title}")

    if not content:
        logger.warning(f"Empty content for SharePoint page {title}")
        return None

    try:
        # Wrap in HTML
        full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{html.escape(title)}</title>
</head>
<body>
<h1>{html.escape(title)}</h1>
<div class="page-meta">
<ul>
<li><strong>Site:</strong> {html.escape(site_name)}</li>
<li><strong>URL:</strong> {html.escape(web_url)}</li>
</ul>
</div>
<div class="page-content">
{html.escape(content)}
</div>
</body>
</html>"""

        safe_site = sanitize_filename(site_name)
        safe_title = sanitize_filename(title)
        s3_key = f"workspaces/{workspace_id}/sharepoint/{safe_site}/pages/{safe_title}.html"

        metadata = {
            "source": "sharepoint_page",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "sharepoint_site": site_name,
            "sharepoint_page_id": page_id[:128] if page_id else "",
        }

        if sync_id:
            metadata["sync_id"] = sync_id

        dual_write.s3_put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=full_html.encode("utf-8"),
            ContentType="text/html",
            Metadata=metadata,
            ExpectedBucketOwner=_get_account_id(),
        )

        logger.info(f"Uploaded SharePoint page to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except ClientError as e:
        logger.error(f"S3 error uploading SharePoint page {title}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing SharePoint page {title}: {e}")
        return None


# ============================================================================
# Main Handler
# ============================================================================


def _build_repo_identifier(source: str, workspace_id: str, body: Dict, file_info: Dict) -> str:
    """Build the per-source composite repo identifier used by
    check_repo_limit() (ADR-019).

    file-ingestion is the upstream entry: it picks the source identifier
    out of the SQS message and composes ``{workspace_id}:{source}:{ident}``
    so the license server has a stable, low-cardinality string per
    customer source. Liveness reporting (license layer v2.0.0
    desired-state snapshot via report_repos_snapshot) is owned by the
    *-integration connect/disconnect handlers, not this Lambda.
    """
    if source in ("confluence", "confluence_attachment"):
        ident = file_info.get("space_key") or file_info.get("space_name") or ""
    elif source in ("jira", "jira_attachment"):
        ident = file_info.get("project_key") or file_info.get("project_name") or ""
    elif source in ("outlook", "outlook_attachment"):
        ident = file_info.get("folder_name") or "Inbox"
    elif source in ("teams", "teams_file"):
        ident = file_info.get("channel_name") or "General"
    elif source in ("sharepoint", "sharepoint_page"):
        ident = file_info.get("site_name") or ""
    elif source == "onedrive":
        # Use drive_id (Graph stable identifier) as the license-tracking
        # identity. Different folders within the same OneDrive drive share
        # the same identifier - they're the same licensable repo.
        ident = file_info.get("drive_id") or ""
    elif source == "onenote":
        # OneNote scope is /me-wide, so one user = one licensable repo
        # regardless of which notebooks they pick. Use the notebook_id
        # as a stable per-page identifier; license-server dedups by
        # string match so re-emitting the same notebook_id per page
        # is fine.
        ident = file_info.get("notebook_id") or ""
    elif source == "database":
        # One physical DB (engine + host_hash) is one licensable repo
        # regardless of how many catalogs/schemas/tables are introspected.
        # database-sync uses the same "{engine}:{host_hash}" form when
        # calling check_repo_limit; emitting it as the ident here keeps
        # license-server dedup consistent.
        engine = file_info.get("engine") or ""
        host_hash = file_info.get("host_hash") or ""
        ident = f"{engine}:{host_hash}" if engine and host_hash else ""
    else:
        ident = ""
    # Strip slashes/colons from ident so the composite stays parseable.
    ident = (ident or "").replace(":", "_").replace("/", "_")
    return f"{workspace_id}:{source}:{ident}"


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    SQS-triggered handler for file ingestion.

    Routes messages to source-specific handlers based on 'source' field.

    ADR-019 (license-layer-rollout chunk 04):
    - validate_license() runs once per invocation (fail-closed; raises
      to let SQS DLQ the batch on hard failures).
    - check_repo_limit(repo=composite) runs per-source at this upstream
      entry boundary; blocked sources are skipped in this batch.
    - Liveness reporting (license layer v2.0.0 desired-state snapshot via
      report_repos_snapshot) is owned by the *-integration connect/
      disconnect handlers, not this Lambda.
    """
    logger.info(f"File ingestion handler invoked with {len(event.get('Records', []))} records")

    # ADR-019 fail-closed license validation. SQS-driven: raise on failure
    # so the batch is retried / DLQ'd rather than silently dropped.
    try:
        from outcomeops_license import validate_license

        claims = validate_license()
        logger.info(
            f"[file-ingestion] License validated: {claims.get('sub')}"
        )
    except ImportError:
        logger.error(
            "[file-ingestion] License layer not attached - deploy bug (ADR-019)"
        )
        log_license_fail_open(os.environ.get("AUDIT_TABLE", ""), "file-ingestion")
        raise
    except Exception as e:
        logger.error(f"[file-ingestion] License validation failed: {e}")
        raise

    if "Records" not in event:
        logger.error("No SQS records in event")
        return {"statusCode": 400, "body": "Invalid event"}

    results = []

    for record in event["Records"]:
        try:
            body = json.loads(record["body"])
            source = body.get("source")
            workspace_id = body.get("workspace_id")
            connection_id = body.get("connection_id")
            file_info = body.get("file", {})
            sync_id = body.get("sync_id")
            file_index = body.get("file_index", 0)
            total_files = body.get("total_files", 1)

            log_prefix = f"[{source}]"
            if sync_id:
                log_prefix = f"[{source}] [{sync_id}] [{file_index + 1}/{total_files}]"

            logger.info(f"{log_prefix} Processing file ingestion message")

            if not source or not workspace_id:
                logger.error(f"Invalid message - missing source or workspace_id")
                results.append({"status": "error", "error": "Invalid message"})
                continue

            # ADR-019 per-source repo-limit enforcement. Composite avoids
            # collisions across customers and across sources within one
            # workspace. check_repo_limit returns allowed=True for sources
            # already tracked, so this is safe on every message.
            repo_project = _build_repo_identifier(source, workspace_id, body, file_info)
            try:
                from outcomeops_license import check_repo_limit

                limit_result = check_repo_limit(repo=repo_project)
                if not limit_result.allowed:
                    logger.warning(
                        f"{log_prefix} Repo limit reached for {repo_project}: "
                        f"{limit_result.reason} "
                        f"(current={limit_result.current}/{limit_result.limit})"
                    )
                    results.append({
                        "status": "skipped",
                        "source": source,
                        "reason": "repo_limit",
                        "repo": repo_project,
                    })
                    continue
            except ImportError:
                # Reached only if this point is hit before the top-level
                # validate_license import resolved (shouldn't happen) -
                # log and continue rather than DLQ the entire batch.
                logger.error(
                    "[file-ingestion] License layer not attached during check_repo_limit"
                )
                log_license_fail_open(
                    os.environ.get("AUDIT_TABLE", ""), "file-ingestion"
                )
                raise
            except Exception as e:
                # Network errors fall through here -- the layer's
                # internal fail-closed/fail-open logic on hard tiers
                # already wrapped check_repo_limit. Log and proceed
                # rather than block the entire ingest.
                logger.warning(
                    f"{log_prefix} check_repo_limit failed for {repo_project}: {e}"
                )

            s3_key = None

            # Route to source-specific handler
            if source == "confluence":
                cloud_id = body.get("cloud_id")
                s3_key = process_confluence_page(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    cloud_id=cloud_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "confluence_attachment":
                cloud_id = body.get("cloud_id")
                s3_key = process_confluence_attachment(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    cloud_id=cloud_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "jira":
                cloud_id = body.get("cloud_id")
                s3_key = process_jira_issue(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    cloud_id=cloud_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "azure_devops_boards":
                s3_key = process_azure_devops_boards_work_item(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "jira_attachment":
                cloud_id = body.get("cloud_id")
                s3_key = process_jira_attachment(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    cloud_id=cloud_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "outlook":
                s3_key = process_outlook_email(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "outlook_attachment":
                s3_key = process_outlook_attachment(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "teams":
                s3_key = process_teams_message(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "teams_file":
                s3_key = process_teams_file(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "sharepoint":
                s3_key = process_sharepoint_file(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "sharepoint_page":
                s3_key = process_sharepoint_page(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "onedrive":
                s3_key = process_onedrive_file(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "onenote":
                s3_key = process_onenote_page(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            elif source == "database":
                s3_key = process_database_artifact(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    file_info=file_info,
                    sync_id=sync_id,
                )
            else:
                logger.warning(f"Unknown source: {source}")
                results.append({"status": "skipped", "source": source, "reason": "Unknown source"})
                continue

            if s3_key:
                results.append({"status": "success", "source": source, "s3_key": s3_key})
            else:
                results.append({"status": "error", "source": source, "error": "Processing failed"})

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in SQS message: {e}")
            results.append({"status": "error", "error": "Invalid JSON"})
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            results.append({"status": "error", "error": str(e)})

    successful = sum(1 for r in results if r.get("status") == "success")
    logger.info(f"File ingestion completed: {successful}/{len(results)} successful")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "File ingestion completed",
            "results": results,
        }),
    }
