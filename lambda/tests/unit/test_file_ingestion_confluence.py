# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for file-ingestion Lambda - Confluence and Jira handlers."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

mock_dynamodb_resource = MagicMock()
mock_s3_client = MagicMock()
mock_ssm_client = MagicMock()


def _load_handler_module():
    """Load or reload the file-ingestion handler module."""
    if 'file_ingestion_handler' in sys.modules:
        del sys.modules['file_ingestion_handler']

    # Add file-ingestion dir to path for sibling imports
    fi_dir = os.path.join(os.path.dirname(__file__), '../../file-ingestion')
    if fi_dir not in sys.path:
        sys.path.insert(0, fi_dir)

    handler_path = os.path.join(fi_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("file_ingestion_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['file_ingestion_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace boto3 clients with mocks
    handler_module.dynamodb = mock_dynamodb_resource
    handler_module.s3_client = mock_s3_client
    handler_module.ssm_client = mock_ssm_client

    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
process_confluence_page = handler_module.process_confluence_page
process_confluence_attachment = handler_module.process_confluence_attachment
process_jira_issue = handler_module.process_jira_issue
process_jira_attachment = handler_module.process_jira_attachment
confluence_storage_to_html = handler_module.confluence_storage_to_html
jira_description_to_html = handler_module.jira_description_to_html
sanitize_filename = handler_module.sanitize_filename
_load_oauth_config = handler_module._load_oauth_config


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset all mocks between tests."""
    mock_dynamodb_resource.reset_mock()
    mock_s3_client.reset_mock()
    mock_s3_client.put_object.side_effect = None
    mock_ssm_client.reset_mock()
    mock_ssm_client.get_parameter.side_effect = None
    handler_module._oauth_config = None
    handler_module._microsoft_oauth_config = None
    handler_module.KB_BUCKET = "test-kb-bucket"
    yield


@pytest.fixture
def confluence_page_file_info():
    """Sample Confluence page file info from queue message."""
    return {
        "page_id": "123456",
        "space_key": "ENG",
        "space_name": "Engineering",
        "title": "API Design Guidelines",
    }


@pytest.fixture
def confluence_attachment_file_info():
    """Sample Confluence attachment file info from queue message."""
    return {
        "attachment_id": "att789012",
        "page_id": "123456",
        "space_name": "Engineering",
        "filename": "architecture_diagram.pdf",
    }


@pytest.fixture
def jira_issue_file_info():
    """Sample Jira issue file info from queue message."""
    return {
        "issue_id": "10001",
        "issue_key": "PROJ-42",
        "project_key": "PROJ",
        "project_name": "My Project",
        "summary": "Implement user authentication",
        "description": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "We need to implement OAuth2 authentication."}
                    ],
                }
            ],
        },
        "status": "In Progress",
        "priority": "High",
        "issue_type": "Story",
        "assignee": "jane.doe",
        "reporter": "john.smith",
        "labels": ["backend", "security"],
        "created": "2024-01-15T10:00:00Z",
        "updated": "2024-02-20T14:30:00Z",
        "comments": [
            {
                "author": {"displayName": "John Smith"},
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "Let's use Auth0 for this."}
                            ],
                        }
                    ],
                },
                "created": "2024-01-16T09:00:00Z",
            }
        ],
    }


@pytest.fixture
def jira_attachment_file_info():
    """Sample Jira attachment file info from queue message."""
    return {
        "attachment_id": "20001",
        "issue_key": "PROJ-42",
        "project_name": "My Project",
        "filename": "requirements.pdf",
    }


# ============================================================================
# Load Atlassian OAuth Config
# ============================================================================

class TestLoadOAuthConfig:
    """Test loading Atlassian OAuth config from SSM."""

    def test_load_config_success(self):
        """Test loading config from SSM returns client_id and client_secret."""
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "atlassian-client-id"}},
            {"Parameter": {"Value": "atlassian-client-secret"}},
        ]

        config = _load_oauth_config()
        assert config["client_id"] == "atlassian-client-id"
        assert config["client_secret"] == "atlassian-client-secret"

    def test_load_config_ssm_error(self):
        """Test raises ClientError on SSM failure."""
        mock_ssm_client.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}},
            "GetParameter",
        )

        with pytest.raises(ClientError):
            _load_oauth_config()

    def test_load_config_caches_result(self):
        """Test config is cached after first load."""
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "cid"}},
            {"Parameter": {"Value": "csec"}},
        ]

        _load_oauth_config()
        _load_oauth_config()
        # SSM should only be called twice (two params), not four
        assert mock_ssm_client.get_parameter.call_count == 2


# ============================================================================
# Confluence Storage Format Parser
# ============================================================================

class TestConfluenceStorageToHtml:
    """Test Confluence storage format to HTML conversion."""

    def test_empty_content(self):
        """Test returns empty string for empty input."""
        assert confluence_storage_to_html("") == ""
        assert confluence_storage_to_html(None) == ""

    def test_plain_html_passthrough(self):
        """Test plain HTML is passed through unchanged."""
        html = "<p>Hello world</p>"
        result = confluence_storage_to_html(html)
        assert "<p>Hello world</p>" in result

    def test_removes_cdata_sections(self):
        """Test CDATA sections are unwrapped."""
        content = "<![CDATA[some code here]]>"
        result = confluence_storage_to_html(content)
        assert "some code here" in result
        assert "CDATA" not in result

    def test_converts_code_blocks(self):
        """Test Confluence code macros become pre/code tags."""
        content = (
            '<ac:structured-macro ac:name="code">'
            '<ac:plain-text-body>def hello(): pass</ac:plain-text-body>'
            '</ac:structured-macro>'
        )
        result = confluence_storage_to_html(content)
        assert "<pre><code>" in result
        assert "def hello(): pass" in result

    def test_converts_info_panel(self):
        """Test info/warning/note panels become div elements."""
        content = (
            '<ac:structured-macro ac:name="info">'
            '<ac:rich-text-body>Important note here</ac:rich-text-body>'
            '</ac:structured-macro>'
        )
        result = confluence_storage_to_html(content)
        assert '<div class="panel info">' in result
        assert "Important note here" in result

    def test_converts_warning_panel(self):
        """Test warning panel macro conversion."""
        content = (
            '<ac:structured-macro ac:name="warning">'
            '<ac:rich-text-body>Be careful!</ac:rich-text-body>'
            '</ac:structured-macro>'
        )
        result = confluence_storage_to_html(content)
        assert '<div class="panel warning">' in result
        assert "Be careful!" in result

    def test_converts_expand_macro(self):
        """Test expand/collapse macro conversion."""
        content = (
            '<ac:structured-macro ac:name="expand">'
            '<ac:rich-text-body>Hidden content</ac:rich-text-body>'
            '</ac:structured-macro>'
        )
        result = confluence_storage_to_html(content)
        assert '<div class="expand">' in result
        assert "Hidden content" in result

    def test_removes_remaining_ac_tags(self):
        """Test remaining ac: and ri: tags are stripped."""
        content = '<ac:emoticon ac:name="smile"/><ri:page ri:content-title="Test"/>'
        result = confluence_storage_to_html(content)
        assert "ac:" not in result
        assert "ri:" not in result

    def test_removes_unknown_macros(self):
        """Test unknown structured macros are removed."""
        content = (
            '<ac:structured-macro ac:name="toc">'
            '<ac:parameter ac:name="maxLevel">3</ac:parameter>'
            '</ac:structured-macro>'
            '<p>Remaining text</p>'
        )
        result = confluence_storage_to_html(content)
        assert "toc" not in result
        assert "Remaining text" in result


# ============================================================================
# Jira Description to HTML
# ============================================================================

class TestJiraDescriptionToHtml:
    """Test Jira ADF (Atlassian Document Format) to HTML conversion."""

    def test_none_description(self):
        """Test returns empty string for None."""
        assert jira_description_to_html(None) == ""

    def test_empty_string_description(self):
        """Test returns empty string for empty string."""
        assert jira_description_to_html("") == ""

    def test_string_description(self):
        """Test plain string is wrapped in p tag and HTML-escaped."""
        result = jira_description_to_html("Hello <world>")
        assert "<p>" in result
        assert "&lt;world&gt;" in result

    def test_non_dict_non_string(self):
        """Test returns empty string for non-dict, non-string types."""
        assert jira_description_to_html(12345) == ""
        assert jira_description_to_html([1, 2, 3]) == ""

    def test_simple_paragraph(self):
        """Test ADF paragraph node renders to HTML."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Hello world"}
                    ],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert "<p>Hello world</p>" in result

    def test_text_with_bold_mark(self):
        """Test bold (strong) mark on text."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "important",
                            "marks": [{"type": "strong"}],
                        }
                    ],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert "<strong>important</strong>" in result

    def test_text_with_em_mark(self):
        """Test italic (em) mark on text."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "emphasized",
                            "marks": [{"type": "em"}],
                        }
                    ],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert "<em>emphasized</em>" in result

    def test_text_with_code_mark(self):
        """Test inline code mark on text."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "var_name",
                            "marks": [{"type": "code"}],
                        }
                    ],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert "<code>var_name</code>" in result

    def test_text_with_link_mark(self):
        """Test link mark on text."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "Click here",
                            "marks": [
                                {
                                    "type": "link",
                                    "attrs": {"href": "https://example.com"},
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert '<a href="https://example.com">Click here</a>' in result

    def test_heading_node(self):
        """Test heading renders with correct level."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": "Section Title"}],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert "<h2>Section Title</h2>" in result

    def test_bullet_list(self):
        """Test bullet list renders as ul/li."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Item 1"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Item 2"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert "<ul>" in result
        assert "<li>" in result
        assert "Item 1" in result
        assert "Item 2" in result

    def test_ordered_list(self):
        """Test ordered list renders as ol/li."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "orderedList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Step 1"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert "<ol>" in result
        assert "<li>" in result

    def test_code_block(self):
        """Test code block renders as pre/code."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "codeBlock",
                    "content": [{"type": "text", "text": "print('hello')"}],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert "<pre><code>" in result
        assert "print(&#x27;hello&#x27;)" in result

    def test_blockquote(self):
        """Test blockquote node."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "blockquote",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "A quote"}],
                        }
                    ],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert "<blockquote>" in result
        assert "A quote" in result

    def test_hard_break(self):
        """Test hard break renders as br tag."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Line 1"},
                        {"type": "hardBreak"},
                        {"type": "text", "text": "Line 2"},
                    ],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert "Line 1" in result
        assert "<br/>" in result
        assert "Line 2" in result

    def test_unknown_node_type_renders_children(self):
        """Test unknown node type still renders its children."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "unknownNodeType",
                    "content": [
                        {"type": "text", "text": "Child text"}
                    ],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert "Child text" in result

    def test_html_escaping_in_text(self):
        """Test HTML characters in text are escaped."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "x < y && z > 0"}
                    ],
                }
            ],
        }
        result = jira_description_to_html(adf)
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result

    def test_multiple_marks_on_text(self):
        """Test multiple marks applied to the same text node."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "bold italic",
                            "marks": [{"type": "strong"}, {"type": "em"}],
                        }
                    ],
                }
            ],
        }
        result = jira_description_to_html(adf)
        # Both marks should be applied
        assert "<strong>" in result
        assert "<em>" in result
        assert "bold italic" in result


# ============================================================================
# Sanitize Filename
# ============================================================================

class TestSanitizeFilename:
    """Test filename sanitization."""

    def test_basic_sanitization(self):
        """Test problematic characters are replaced."""
        assert sanitize_filename('file<>name') == "file__name"

    def test_spaces_replaced(self):
        """Test spaces are replaced with underscores."""
        assert sanitize_filename("my file name") == "my_file_name"

    def test_strips_leading_trailing(self):
        """Test strips leading/trailing dots and underscores."""
        assert sanitize_filename(".hidden_") == "hidden"

    def test_length_limit(self):
        """Test filename is truncated to 200 characters."""
        long_name = "a" * 300
        result = sanitize_filename(long_name)
        assert len(result) == 200


# ============================================================================
# Process Confluence Page
# ============================================================================

class TestProcessConfluencePage:
    """Test Confluence page processing and S3 upload.

    process_confluence_page uses local imports:
        from confluence_api import get_page_content, ConfluenceAPIError
        from token_manager import refresh_access_token
    We mock these via sys.modules patches.
    """

    def _setup_oauth_ssm(self):
        """Set up SSM mock for Atlassian OAuth config."""
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "atl-client-id"}},
            {"Parameter": {"Value": "atl-client-secret"}},
        ]

    def _make_confluence_api_mock(self):
        """Create confluence_api mock with real exception class."""
        from confluence_api import ConfluenceAPIError
        mock_api = MagicMock()
        mock_api.ConfluenceAPIError = ConfluenceAPIError
        return mock_api

    def test_process_page_success(self, confluence_page_file_info):
        """Test successful Confluence page fetch and upload to S3."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.get_page_content.return_value = {
            "title": "API Design Guidelines",
            "body": {
                "storage": {
                    "value": "<p>Design your APIs with REST principles.</p>"
                }
            },
            "version": {"number": 3},
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_page(
                "ws_test", "conn_test", "cloud_123",
                confluence_page_file_info, sync_id="sync_001",
            )

        assert result is not None
        assert "workspaces/ws_test/confluence/" in result
        assert result.endswith(".html")
        assert "Engineering" in result
        mock_s3_client.put_object.assert_called_once()

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["Bucket"] == "test-kb-bucket"
        assert call_args["ContentType"] == "text/html"
        assert call_args["Metadata"]["source"] == "confluence"
        assert call_args["Metadata"]["workspace_id"] == "ws_test"
        assert call_args["Metadata"]["connection_id"] == "conn_test"
        assert call_args["Metadata"]["confluence_page_id"] == "123456"
        assert call_args["Metadata"]["confluence_space_key"] == "ENG"
        assert call_args["Metadata"]["confluence_version"] == "3"
        assert call_args["Metadata"]["sync_id"] == "sync_001"

    def test_process_page_html_content(self, confluence_page_file_info):
        """Test HTML body contains page title and content."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.get_page_content.return_value = {
            "title": "API Design Guidelines",
            "body": {"storage": {"value": "<p>REST principles</p>"}},
            "version": {"number": 1},
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            process_confluence_page(
                "ws_test", "conn_test", "cloud_123", confluence_page_file_info,
            )

        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        assert "API Design Guidelines" in body
        assert "REST principles" in body
        assert "<!DOCTYPE html>" in body

    def test_process_page_empty_content_returns_none(self, confluence_page_file_info):
        """Test returns None when page has empty storage content."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.get_page_content.return_value = {
            "title": "Empty Page",
            "body": {"storage": {"value": ""}},
            "version": {"number": 1},
        }

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_page(
                "ws_test", "conn_test", "cloud_123", confluence_page_file_info,
            )

        assert result is None
        mock_s3_client.put_object.assert_not_called()

    def test_process_page_missing_body_returns_none(self, confluence_page_file_info):
        """Test returns None when page response has no body key."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.get_page_content.return_value = {
            "title": "No Body Page",
            "version": {"number": 1},
        }

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_page(
                "ws_test", "conn_test", "cloud_123", confluence_page_file_info,
            )

        assert result is None

    def test_process_page_token_refresh_fails(self, confluence_page_file_info):
        """Test returns None when token refresh fails."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = None

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_page(
                "ws_test", "conn_test", "cloud_123", confluence_page_file_info,
            )

        assert result is None
        mock_s3_client.put_object.assert_not_called()

    def test_process_page_api_error(self, confluence_page_file_info):
        """Test returns None on Confluence API error."""
        self._setup_oauth_ssm()

        from confluence_api import ConfluenceAPIError

        mock_api = MagicMock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.get_page_content.side_effect = ConfluenceAPIError("Page not found", status_code=404)
        mock_api.ConfluenceAPIError = ConfluenceAPIError

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_page(
                "ws_test", "conn_test", "cloud_123", confluence_page_file_info,
            )

        assert result is None

    def test_process_page_s3_error(self, confluence_page_file_info):
        """Test returns None on S3 upload error."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.get_page_content.return_value = {
            "title": "Page",
            "body": {"storage": {"value": "<p>Content</p>"}},
            "version": {"number": 1},
        }
        mock_s3_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal Error"}}, "PutObject"
        )

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_page(
                "ws_test", "conn_test", "cloud_123", confluence_page_file_info,
            )

        assert result is None

    def test_process_page_no_sync_id(self, confluence_page_file_info):
        """Test metadata excludes sync_id when not provided."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.get_page_content.return_value = {
            "title": "Page",
            "body": {"storage": {"value": "<p>Content</p>"}},
            "version": {"number": 1},
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            process_confluence_page(
                "ws_test", "conn_test", "cloud_123", confluence_page_file_info,
            )

        call_args = mock_s3_client.put_object.call_args[1]
        assert "sync_id" not in call_args["Metadata"]

    def test_process_page_s3_path_structure(self, confluence_page_file_info):
        """Test S3 key follows expected path structure."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.get_page_content.return_value = {
            "title": "API Design Guidelines",
            "body": {"storage": {"value": "<p>Content</p>"}},
            "version": {"number": 1},
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_page(
                "ws_test", "conn_test", "cloud_123", confluence_page_file_info,
            )

        assert result.startswith("workspaces/ws_test/confluence/")
        assert "Engineering/" in result
        assert "_123456.html" in result

    def test_process_page_missing_optional_fields(self):
        """Test handles missing optional fields in file_info."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.get_page_content.return_value = {
            "title": "Minimal Page",
            "body": {"storage": {"value": "<p>Content</p>"}},
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        # Minimal file_info with only page_id
        file_info = {"page_id": "999"}

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_page(
                "ws_test", "conn_test", "cloud_123", file_info,
            )

        assert result is not None
        assert "workspaces/ws_test/confluence/" in result

    def test_process_page_escapes_html_in_title(self):
        """Test page title is HTML-escaped in the generated document."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.get_page_content.return_value = {
            "title": '<script>alert("xss")</script>',
            "body": {"storage": {"value": "<p>Content</p>"}},
            "version": {"number": 1},
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        file_info = {"page_id": "111"}

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            process_confluence_page(
                "ws_test", "conn_test", "cloud_123", file_info,
            )

        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        assert "<script>" not in body


# ============================================================================
# Process Confluence Attachment
# ============================================================================

class TestProcessConfluenceAttachment:
    """Test Confluence attachment processing.

    process_confluence_attachment uses local imports:
        from confluence_api import download_attachment, ConfluenceAPIError
        from token_manager import refresh_access_token
    """

    def _setup_oauth_ssm(self):
        """Set up SSM mock for Atlassian OAuth config."""
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "atl-client-id"}},
            {"Parameter": {"Value": "atl-client-secret"}},
        ]

    def _make_confluence_api_mock(self):
        """Create confluence_api mock with real exception class."""
        from confluence_api import ConfluenceAPIError
        mock_api = MagicMock()
        mock_api.ConfluenceAPIError = ConfluenceAPIError
        return mock_api

    def test_process_attachment_success(self, confluence_attachment_file_info):
        """Test successful attachment download and upload."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"PDF binary content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_attachment(
                "ws_test", "conn_test", "cloud_123",
                confluence_attachment_file_info, sync_id="sync_001",
            )

        assert result is not None
        assert "workspaces/ws_test/confluence/" in result
        assert "attachments/" in result
        mock_s3_client.put_object.assert_called_once()

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["Bucket"] == "test-kb-bucket"
        assert call_args["ContentType"] == "application/pdf"
        assert call_args["Metadata"]["source"] == "confluence_attachment"
        assert call_args["Metadata"]["workspace_id"] == "ws_test"
        assert call_args["Metadata"]["connection_id"] == "conn_test"
        assert call_args["Metadata"]["confluence_attachment_id"] == "att789012"
        assert call_args["Metadata"]["confluence_page_id"] == "123456"
        assert call_args["Metadata"]["sync_id"] == "sync_001"

    def test_process_attachment_s3_path_structure(self, confluence_attachment_file_info):
        """Test S3 key follows expected path: workspace/confluence/space/page_id/attachments/filename."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_attachment(
                "ws_test", "conn_test", "cloud_123",
                confluence_attachment_file_info,
            )

        assert result.startswith("workspaces/ws_test/confluence/Engineering/")
        assert "/123456/attachments/" in result

    def test_process_attachment_content_type_pdf(self, confluence_attachment_file_info):
        """Test PDF files get application/pdf content type."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            process_confluence_attachment(
                "ws_test", "conn_test", "cloud_123",
                confluence_attachment_file_info,
            )

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["ContentType"] == "application/pdf"

    def test_process_attachment_content_type_image(self):
        """Test image files get image/* content type."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"image content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        file_info = {
            "attachment_id": "att001",
            "page_id": "123",
            "space_name": "ENG",
            "filename": "screenshot.png",
        }

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            process_confluence_attachment(
                "ws_test", "conn_test", "cloud_123", file_info,
            )

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["ContentType"] == "image/png"

    def test_process_attachment_content_type_default(self):
        """Test unknown file extensions get octet-stream content type."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"binary content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        file_info = {
            "attachment_id": "att001",
            "page_id": "123",
            "space_name": "ENG",
            "filename": "data.xyz",
        }

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            process_confluence_attachment(
                "ws_test", "conn_test", "cloud_123", file_info,
            )

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["ContentType"] == "application/octet-stream"

    def test_process_attachment_token_refresh_fails(self, confluence_attachment_file_info):
        """Test returns None when token refresh fails."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = None

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_attachment(
                "ws_test", "conn_test", "cloud_123",
                confluence_attachment_file_info,
            )

        assert result is None
        mock_s3_client.put_object.assert_not_called()

    def test_process_attachment_download_error(self, confluence_attachment_file_info):
        """Test returns None on Confluence API download error."""
        self._setup_oauth_ssm()

        from confluence_api import ConfluenceAPIError

        mock_api = MagicMock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.download_attachment.side_effect = ConfluenceAPIError("Download failed", status_code=500)
        mock_api.ConfluenceAPIError = ConfluenceAPIError

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_attachment(
                "ws_test", "conn_test", "cloud_123",
                confluence_attachment_file_info,
            )

        assert result is None

    def test_process_attachment_s3_error(self, confluence_attachment_file_info):
        """Test returns None on S3 upload error."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "PutObject"
        )

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            result = process_confluence_attachment(
                "ws_test", "conn_test", "cloud_123",
                confluence_attachment_file_info,
            )

        assert result is None

    def test_process_attachment_no_sync_id(self, confluence_attachment_file_info):
        """Test metadata excludes sync_id when not provided."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            process_confluence_attachment(
                "ws_test", "conn_test", "cloud_123",
                confluence_attachment_file_info,
            )

        call_args = mock_s3_client.put_object.call_args[1]
        assert "sync_id" not in call_args["Metadata"]

    def test_process_attachment_passes_page_id_to_download(self, confluence_attachment_file_info):
        """Test page_id is passed to download_attachment for OAuth-compatible endpoint."""
        self._setup_oauth_ssm()

        mock_api = self._make_confluence_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'confluence_api': mock_api, 'token_manager': mock_token}):
            process_confluence_attachment(
                "ws_test", "conn_test", "cloud_123",
                confluence_attachment_file_info,
            )

        mock_api.download_attachment.assert_called_once_with(
            "access_token", "cloud_123", "att789012", page_id="123456",
        )


# ============================================================================
# Process Jira Issue
# ============================================================================

class TestProcessJiraIssue:
    """Test Jira issue processing and S3 upload.

    process_jira_issue does NOT use local imports (unlike attachment handlers).
    It receives all issue data in file_info and builds HTML directly.
    """

    def test_process_issue_success(self, jira_issue_file_info):
        """Test successful Jira issue upload to S3."""
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        result = process_jira_issue(
            "ws_test", "conn_test", "cloud_123",
            jira_issue_file_info, sync_id="sync_001",
        )

        assert result is not None
        assert "workspaces/ws_test/jira/" in result
        assert result.endswith(".html")
        assert "PROJ-42.html" in result
        mock_s3_client.put_object.assert_called_once()

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["Bucket"] == "test-kb-bucket"
        assert call_args["ContentType"] == "text/html"
        assert call_args["Metadata"]["source"] == "jira"
        assert call_args["Metadata"]["workspace_id"] == "ws_test"
        assert call_args["Metadata"]["connection_id"] == "conn_test"
        assert call_args["Metadata"]["jira_issue_id"] == "10001"
        assert call_args["Metadata"]["jira_issue_key"] == "PROJ-42"
        assert call_args["Metadata"]["jira_project_key"] == "PROJ"
        assert call_args["Metadata"]["sync_id"] == "sync_001"

    def test_process_issue_html_content(self, jira_issue_file_info):
        """Test HTML body contains issue key, summary, description, and metadata."""
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        process_jira_issue("ws_test", "conn_test", "cloud_123", jira_issue_file_info)

        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        assert "PROJ-42" in body
        assert "Implement user authentication" in body
        assert "OAuth2 authentication" in body
        assert "In Progress" in body
        assert "High" in body
        assert "Story" in body
        assert "jane.doe" in body
        assert "john.smith" in body
        assert "backend" in body
        assert "security" in body

    def test_process_issue_with_comments(self, jira_issue_file_info):
        """Test comments are included in the HTML output."""
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        process_jira_issue("ws_test", "conn_test", "cloud_123", jira_issue_file_info)

        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        assert "Comments" in body
        assert "John Smith" in body
        assert "Auth0" in body

    def test_process_issue_no_description(self):
        """Test issue with no description shows placeholder."""
        file_info = {
            "issue_id": "10002",
            "issue_key": "PROJ-43",
            "project_key": "PROJ",
            "project_name": "My Project",
            "summary": "Bug fix",
            "description": None,
            "status": "Open",
            "issue_type": "Bug",
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        result = process_jira_issue("ws_test", "conn_test", "cloud_123", file_info)

        assert result is not None
        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        assert "No description provided" in body

    def test_process_issue_no_comments(self):
        """Test issue with no comments omits comments section."""
        file_info = {
            "issue_id": "10003",
            "issue_key": "PROJ-44",
            "project_key": "PROJ",
            "project_name": "My Project",
            "summary": "Feature request",
            "status": "Open",
            "issue_type": "Task",
            "comments": [],
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        process_jira_issue("ws_test", "conn_test", "cloud_123", file_info)

        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        # Comments heading should not appear when there are no comments
        assert "<h2>Comments</h2>" not in body

    def test_process_issue_minimal_fields(self):
        """Test handles issue with only required fields (issue_id)."""
        file_info = {"issue_id": "10004"}
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        result = process_jira_issue("ws_test", "conn_test", "cloud_123", file_info)

        assert result is not None
        assert "workspaces/ws_test/jira/" in result

    def test_process_issue_s3_path_structure(self, jira_issue_file_info):
        """Test S3 key follows expected path structure."""
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        result = process_jira_issue(
            "ws_test", "conn_test", "cloud_123", jira_issue_file_info,
        )

        assert result.startswith("workspaces/ws_test/jira/")
        assert "My_Project/" in result
        assert "PROJ-42.html" in result

    def test_process_issue_s3_error(self, jira_issue_file_info):
        """Test returns None on S3 error."""
        mock_s3_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "PutObject"
        )

        result = process_jira_issue(
            "ws_test", "conn_test", "cloud_123", jira_issue_file_info,
        )

        assert result is None

    def test_process_issue_no_sync_id(self, jira_issue_file_info):
        """Test metadata excludes sync_id when not provided."""
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        process_jira_issue("ws_test", "conn_test", "cloud_123", jira_issue_file_info)

        call_args = mock_s3_client.put_object.call_args[1]
        assert "sync_id" not in call_args["Metadata"]

    def test_process_issue_html_escapes_summary(self):
        """Test HTML characters in summary are escaped."""
        file_info = {
            "issue_id": "10005",
            "issue_key": "PROJ-45",
            "project_key": "PROJ",
            "project_name": "My Project",
            "summary": '<script>alert("xss")</script>',
            "status": "Open",
            "issue_type": "Bug",
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        process_jira_issue("ws_test", "conn_test", "cloud_123", file_info)

        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        assert "<script>" not in body

    def test_process_issue_generic_exception(self):
        """Test returns None on unexpected exception."""
        file_info = {
            "issue_id": "10006",
            "issue_key": "PROJ-46",
            "project_key": "PROJ",
            "project_name": "My Project",
            "summary": "Test",
            "status": "Open",
            "issue_type": "Task",
            # Trigger a generic exception by making comments non-iterable
            "comments": "not_a_list",
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        # "not_a_list" is iterable (string), so we need a different approach.
        # Instead, mock put_object to raise a non-ClientError exception
        mock_s3_client.put_object.side_effect = RuntimeError("Unexpected error")

        result = process_jira_issue("ws_test", "conn_test", "cloud_123", file_info)
        assert result is None

    def test_process_issue_with_labels(self):
        """Test labels are rendered in metadata section."""
        file_info = {
            "issue_id": "10007",
            "issue_key": "PROJ-47",
            "project_key": "PROJ",
            "project_name": "My Project",
            "summary": "Test labels",
            "status": "Open",
            "issue_type": "Task",
            "labels": ["frontend", "urgent", "p0"],
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        process_jira_issue("ws_test", "conn_test", "cloud_123", file_info)

        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        assert "frontend" in body
        assert "urgent" in body
        assert "p0" in body

    def test_process_issue_string_description(self):
        """Test issue with plain string description (not ADF)."""
        file_info = {
            "issue_id": "10008",
            "issue_key": "PROJ-48",
            "project_key": "PROJ",
            "project_name": "My Project",
            "summary": "String desc issue",
            "description": "This is a plain text description",
            "status": "Open",
            "issue_type": "Task",
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        process_jira_issue("ws_test", "conn_test", "cloud_123", file_info)

        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        assert "This is a plain text description" in body


# ============================================================================
# Process Jira Attachment
# ============================================================================

class TestProcessJiraAttachment:
    """Test Jira attachment processing.

    process_jira_attachment uses local imports:
        from jira_api import download_attachment, JiraAPIError
        from jira_token_manager import refresh_access_token
    We mock these via sys.modules patches.
    """

    def _setup_oauth_ssm(self):
        """Set up SSM mock for Atlassian OAuth config."""
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "atl-client-id"}},
            {"Parameter": {"Value": "atl-client-secret"}},
        ]

    def _make_jira_api_mock(self):
        """Create jira_api mock with real exception class."""
        from jira_api import JiraAPIError
        mock_api = MagicMock()
        mock_api.JiraAPIError = JiraAPIError
        return mock_api

    def test_process_attachment_success(self, jira_attachment_file_info):
        """Test successful Jira attachment download and upload."""
        self._setup_oauth_ssm()

        mock_api = self._make_jira_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"PDF binary content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'jira_api': mock_api, 'jira_token_manager': mock_token}):
            result = process_jira_attachment(
                "ws_test", "conn_test", "cloud_123",
                jira_attachment_file_info, sync_id="sync_001",
            )

        assert result is not None
        assert "workspaces/ws_test/jira/" in result
        assert "attachments/" in result
        mock_s3_client.put_object.assert_called_once()

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["Bucket"] == "test-kb-bucket"
        assert call_args["ContentType"] == "application/pdf"
        assert call_args["Metadata"]["source"] == "jira_attachment"
        assert call_args["Metadata"]["workspace_id"] == "ws_test"
        assert call_args["Metadata"]["connection_id"] == "conn_test"
        assert call_args["Metadata"]["jira_attachment_id"] == "20001"
        assert call_args["Metadata"]["jira_issue_key"] == "PROJ-42"
        assert call_args["Metadata"]["sync_id"] == "sync_001"

    def test_process_attachment_s3_path_structure(self, jira_attachment_file_info):
        """Test S3 key follows expected path: workspace/jira/project/issue_key/attachments/filename."""
        self._setup_oauth_ssm()

        mock_api = self._make_jira_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'jira_api': mock_api, 'jira_token_manager': mock_token}):
            result = process_jira_attachment(
                "ws_test", "conn_test", "cloud_123",
                jira_attachment_file_info,
            )

        assert result.startswith("workspaces/ws_test/jira/My_Project/")
        assert "/PROJ-42/attachments/" in result

    def test_process_attachment_content_type_pdf(self, jira_attachment_file_info):
        """Test PDF files get application/pdf content type."""
        self._setup_oauth_ssm()

        mock_api = self._make_jira_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'jira_api': mock_api, 'jira_token_manager': mock_token}):
            process_jira_attachment(
                "ws_test", "conn_test", "cloud_123",
                jira_attachment_file_info,
            )

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["ContentType"] == "application/pdf"

    def test_process_attachment_content_type_image(self):
        """Test image files get image/* content type."""
        self._setup_oauth_ssm()

        mock_api = self._make_jira_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"image content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        file_info = {
            "attachment_id": "30001",
            "issue_key": "PROJ-50",
            "project_name": "Project",
            "filename": "diagram.jpg",
        }

        with patch.dict(sys.modules, {'jira_api': mock_api, 'jira_token_manager': mock_token}):
            process_jira_attachment(
                "ws_test", "conn_test", "cloud_123", file_info,
            )

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["ContentType"] == "image/jpg"

    def test_process_attachment_content_type_default(self):
        """Test unknown file extensions get octet-stream content type."""
        self._setup_oauth_ssm()

        mock_api = self._make_jira_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"binary content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        file_info = {
            "attachment_id": "30002",
            "issue_key": "PROJ-51",
            "project_name": "Project",
            "filename": "data.dat",
        }

        with patch.dict(sys.modules, {'jira_api': mock_api, 'jira_token_manager': mock_token}):
            process_jira_attachment(
                "ws_test", "conn_test", "cloud_123", file_info,
            )

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["ContentType"] == "application/octet-stream"

    def test_process_attachment_token_refresh_fails(self, jira_attachment_file_info):
        """Test returns None when token refresh fails."""
        self._setup_oauth_ssm()

        mock_api = self._make_jira_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = None

        with patch.dict(sys.modules, {'jira_api': mock_api, 'jira_token_manager': mock_token}):
            result = process_jira_attachment(
                "ws_test", "conn_test", "cloud_123",
                jira_attachment_file_info,
            )

        assert result is None
        mock_s3_client.put_object.assert_not_called()

    def test_process_attachment_download_error(self, jira_attachment_file_info):
        """Test returns None on Jira API download error."""
        self._setup_oauth_ssm()

        from jira_api import JiraAPIError

        mock_api = MagicMock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.download_attachment.side_effect = JiraAPIError("Download failed", status_code=500)
        mock_api.JiraAPIError = JiraAPIError

        with patch.dict(sys.modules, {'jira_api': mock_api, 'jira_token_manager': mock_token}):
            result = process_jira_attachment(
                "ws_test", "conn_test", "cloud_123",
                jira_attachment_file_info,
            )

        assert result is None

    def test_process_attachment_s3_error(self, jira_attachment_file_info):
        """Test returns None on S3 upload error."""
        self._setup_oauth_ssm()

        mock_api = self._make_jira_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "PutObject"
        )

        with patch.dict(sys.modules, {'jira_api': mock_api, 'jira_token_manager': mock_token}):
            result = process_jira_attachment(
                "ws_test", "conn_test", "cloud_123",
                jira_attachment_file_info,
            )

        assert result is None

    def test_process_attachment_no_sync_id(self, jira_attachment_file_info):
        """Test metadata excludes sync_id when not provided."""
        self._setup_oauth_ssm()

        mock_api = self._make_jira_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'jira_api': mock_api, 'jira_token_manager': mock_token}):
            process_jira_attachment(
                "ws_test", "conn_test", "cloud_123",
                jira_attachment_file_info,
            )

        call_args = mock_s3_client.put_object.call_args[1]
        assert "sync_id" not in call_args["Metadata"]

    def test_process_attachment_calls_download_correctly(self, jira_attachment_file_info):
        """Test download_attachment is called with correct parameters."""
        self._setup_oauth_ssm()

        mock_api = self._make_jira_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'jira_api': mock_api, 'jira_token_manager': mock_token}):
            process_jira_attachment(
                "ws_test", "conn_test", "cloud_123",
                jira_attachment_file_info,
            )

        mock_api.download_attachment.assert_called_once_with(
            "access_token", "cloud_123", "20001",
        )


# ============================================================================
# Lambda Handler Routing - Confluence and Jira Sources
# ============================================================================

class TestLambdaHandlerConfluenceJiraRouting:
    """Test lambda_handler routes Confluence and Jira sources correctly."""

    @patch.object(handler_module, 'process_confluence_page')
    def test_routes_confluence_source(self, mock_process):
        """Test routes source='confluence' to process_confluence_page."""
        mock_process.return_value = "workspaces/ws_test/confluence/ENG/page_123.html"

        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "confluence",
                    "workspace_id": "ws_test",
                    "connection_id": "conn_test",
                    "cloud_id": "cloud_123",
                    "file": {"page_id": "123", "title": "Test Page"},
                    "sync_id": "sync_001",
                })
            }]
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "success"
        assert body["results"][0]["source"] == "confluence"
        mock_process.assert_called_once()

        # Verify cloud_id was passed
        call_kwargs = mock_process.call_args[1]
        assert call_kwargs["cloud_id"] == "cloud_123"

    @patch.object(handler_module, 'process_confluence_attachment')
    def test_routes_confluence_attachment_source(self, mock_process):
        """Test routes source='confluence_attachment' to process_confluence_attachment."""
        mock_process.return_value = "workspaces/ws_test/confluence/ENG/123/attachments/file.pdf"

        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "confluence_attachment",
                    "workspace_id": "ws_test",
                    "connection_id": "conn_test",
                    "cloud_id": "cloud_123",
                    "file": {"attachment_id": "att001", "page_id": "123"},
                })
            }]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "success"
        assert body["results"][0]["source"] == "confluence_attachment"
        mock_process.assert_called_once()

    @patch.object(handler_module, 'process_jira_issue')
    def test_routes_jira_source(self, mock_process):
        """Test routes source='jira' to process_jira_issue."""
        mock_process.return_value = "workspaces/ws_test/jira/Project/PROJ-42.html"

        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "jira",
                    "workspace_id": "ws_test",
                    "connection_id": "conn_test",
                    "cloud_id": "cloud_123",
                    "file": {"issue_id": "10001", "issue_key": "PROJ-42"},
                    "sync_id": "sync_002",
                })
            }]
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "success"
        assert body["results"][0]["source"] == "jira"
        mock_process.assert_called_once()

    @patch.object(handler_module, 'process_jira_attachment')
    def test_routes_jira_attachment_source(self, mock_process):
        """Test routes source='jira_attachment' to process_jira_attachment."""
        mock_process.return_value = "workspaces/ws_test/jira/Project/PROJ-42/attachments/file.pdf"

        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "jira_attachment",
                    "workspace_id": "ws_test",
                    "connection_id": "conn_test",
                    "cloud_id": "cloud_123",
                    "file": {"attachment_id": "20001", "issue_key": "PROJ-42"},
                })
            }]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "success"
        assert body["results"][0]["source"] == "jira_attachment"
        mock_process.assert_called_once()

    @patch.object(handler_module, 'process_confluence_page')
    def test_confluence_processing_failure(self, mock_process):
        """Test reports error when Confluence processing returns None."""
        mock_process.return_value = None

        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "confluence",
                    "workspace_id": "ws_test",
                    "connection_id": "conn_test",
                    "cloud_id": "cloud_123",
                    "file": {"page_id": "123"},
                })
            }]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "error"

    @patch.object(handler_module, 'process_jira_issue')
    def test_jira_processing_failure(self, mock_process):
        """Test reports error when Jira processing returns None."""
        mock_process.return_value = None

        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "jira",
                    "workspace_id": "ws_test",
                    "connection_id": "conn_test",
                    "cloud_id": "cloud_123",
                    "file": {"issue_id": "10001"},
                })
            }]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "error"

    def test_missing_workspace_id_returns_error(self):
        """Test missing workspace_id field returns error."""
        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "confluence",
                    "connection_id": "conn_test",
                    "file": {},
                })
            }]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "error"

    @patch.object(handler_module, 'process_confluence_page')
    @patch.object(handler_module, 'process_jira_issue')
    def test_multiple_records_mixed_sources(self, mock_jira, mock_confluence):
        """Test handler processes multiple records with different sources."""
        mock_confluence.return_value = "workspaces/ws_test/confluence/page.html"
        mock_jira.return_value = "workspaces/ws_test/jira/issue.html"

        event = {
            "Records": [
                {
                    "body": json.dumps({
                        "source": "confluence",
                        "workspace_id": "ws_test",
                        "connection_id": "conn_test",
                        "cloud_id": "cloud_123",
                        "file": {"page_id": "123"},
                    })
                },
                {
                    "body": json.dumps({
                        "source": "jira",
                        "workspace_id": "ws_test",
                        "connection_id": "conn_test",
                        "cloud_id": "cloud_123",
                        "file": {"issue_id": "10001"},
                    })
                },
            ]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert len(body["results"]) == 2
        assert body["results"][0]["status"] == "success"
        assert body["results"][1]["status"] == "success"
        mock_confluence.assert_called_once()
        mock_jira.assert_called_once()

    @patch.object(handler_module, 'process_confluence_page')
    def test_sync_id_and_file_index_in_log_prefix(self, mock_process):
        """Test handler processes records with sync tracking fields."""
        mock_process.return_value = "workspaces/ws_test/confluence/page.html"

        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "confluence",
                    "workspace_id": "ws_test",
                    "connection_id": "conn_test",
                    "cloud_id": "cloud_123",
                    "file": {"page_id": "123"},
                    "sync_id": "sync_001",
                    "file_index": 5,
                    "total_files": 100,
                })
            }]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "success"

    @patch.object(handler_module, 'process_confluence_page')
    @patch.object(handler_module, 'process_jira_issue')
    def test_partial_batch_failure_only_failed_record_retried(
        self, mock_jira, mock_confluence
    ):
        """Chunk 02 contract: mid-batch failure -> only that itemIdentifier
        lands in batchItemFailures. Previously the whole batch returned
        200 and every message was silently deleted, causing permanent KB gaps."""
        mock_confluence.return_value = "workspaces/ws_test/confluence/page.html"
        mock_jira.return_value = None  # processing failure

        event = {
            "Records": [
                {
                    "messageId": "msg-conf",
                    "body": json.dumps({
                        "source": "confluence",
                        "workspace_id": "ws_test",
                        "connection_id": "conn_test",
                        "cloud_id": "cloud_123",
                        "file": {"page_id": "123"},
                    }),
                },
                {
                    "messageId": "msg-jira",
                    "body": json.dumps({
                        "source": "jira",
                        "workspace_id": "ws_test",
                        "connection_id": "conn_test",
                        "cloud_id": "cloud_123",
                        "file": {"issue_id": "10001"},
                    }),
                },
            ]
        }

        result = lambda_handler(event, None)
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-jira"}]
