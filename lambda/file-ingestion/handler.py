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
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

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

# Cached config
_oauth_config = None
_microsoft_oauth_config = None


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

        s3_client.put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=full_html.encode("utf-8"),
            ContentType="text/html",
            Metadata=metadata,
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

        s3_client.put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content,
            ContentType=content_type,
            Metadata=metadata,
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

        s3_client.put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=full_html.encode("utf-8"),
            ContentType="text/html",
            Metadata=metadata,
        )

        logger.info(f"Uploaded Jira issue to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except ClientError as e:
        logger.error(f"S3 error uploading issue {issue_key}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing issue {issue_key}: {e}")
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

        s3_client.put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content,
            ContentType=content_type,
            Metadata=metadata,
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

        s3_client.put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=full_html.encode("utf-8"),
            ContentType="text/html",
            Metadata=metadata,
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

        s3_client.put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content,
            ContentType=content_type,
            Metadata=metadata,
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

        s3_client.put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=body_content.encode("utf-8"),
            ContentType="text/html",
            Metadata=metadata,
        )

        logger.info(f"Uploaded Teams message to s3://{KB_BUCKET}/{s3_key}")
        return s3_key

    except ClientError as e:
        logger.error(f"S3 error uploading Teams message {message_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing Teams message {message_id}: {e}")
        return None


def process_teams_file(
    workspace_id: str,
    connection_id: str,
    file_info: Dict[str, Any],
    sync_id: Optional[str] = None,
) -> Optional[str]:
    """
    Process a Teams channel file and upload to S3.

    Downloads from pre-authenticated URL provided in file_info.
    """
    import urllib.error
    import urllib.request

    channel_name = file_info.get("channel_name", "General")
    filename = file_info.get("filename", "")
    download_url = file_info.get("download_url", "")
    mime_type = file_info.get("mime_type", "application/octet-stream")
    item_id = file_info.get("item_id", "")

    logger.info(f"Processing Teams file: {channel_name}/{filename}")

    if not download_url:
        logger.error(f"No download URL for Teams file {filename}")
        return None

    try:
        request = urllib.request.Request(download_url)
        with urllib.request.urlopen(request, timeout=300) as response:
            content = response.read()

        safe_channel = sanitize_filename(channel_name)
        safe_filename = sanitize_filename(filename)
        s3_key = f"workspaces/{workspace_id}/teams/{safe_channel}/files/{safe_filename}"

        metadata = {
            "source": "teams_file",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "teams_channel": channel_name,
            "teams_item_id": item_id[:128] if item_id else "",
        }

        if sync_id:
            metadata["sync_id"] = sync_id

        s3_client.put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content,
            ContentType=mime_type,
            Metadata=metadata,
        )

        logger.info(f"Uploaded Teams file to s3://{KB_BUCKET}/{s3_key}")
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

        s3_client.put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content,
            ContentType=mime_type,
            Metadata=metadata,
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

        s3_client.put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=full_html.encode("utf-8"),
            ContentType="text/html",
            Metadata=metadata,
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


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    SQS-triggered handler for file ingestion.

    Routes messages to source-specific handlers based on 'source' field.
    """
    logger.info(f"File ingestion handler invoked with {len(event.get('Records', []))} records")

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
