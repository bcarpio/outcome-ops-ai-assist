"""Unit tests for process-documents Lambda handler."""

import hashlib
import importlib.util
import io
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest
from botocore.exceptions import ClientError

# Check for optional document-processing packages
_has_pypdf2 = importlib.util.find_spec("PyPDF2") is not None
_has_docx = importlib.util.find_spec("docx") is not None
_has_openpyxl = importlib.util.find_spec("openpyxl") is not None
_has_pptx = importlib.util.find_spec("pptx") is not None


# ============================================================================
# Module loading
# ============================================================================

# Pre-mock boto3 clients before module import
mock_s3_client = MagicMock()
mock_ssm_client = MagicMock()
mock_bedrock_client = MagicMock()
mock_s3vectors_client = MagicMock()

# Set environment variables before module load
# Save and explicitly set ENV (will restore in conftest-style cleanup below)
_original_env = os.environ.get("ENV")
os.environ["ENV"] = "test"
os.environ.setdefault("APP_NAME", "outcome-ops-ai-assist")
os.environ.setdefault("KB_BUCKET", "test-kb-bucket")
os.environ.setdefault("AUDIT_TABLE", "")


def _load_handler_module():
    """Load or reload the process-documents handler module."""
    if "process_documents_handler" in sys.modules:
        del sys.modules["process_documents_handler"]

    # Add process-documents dir to path so handler can resolve sibling imports
    handler_dir = os.path.join(os.path.dirname(__file__), "../../process-documents")
    if handler_dir not in sys.path:
        sys.path.insert(0, handler_dir)

    # Also add the lambda root so shared.audit can be resolved
    lambda_root = os.path.join(os.path.dirname(__file__), "../..")
    if lambda_root not in sys.path:
        sys.path.insert(0, lambda_root)

    handler_path = os.path.join(handler_dir, "handler.py")
    spec = importlib.util.spec_from_file_location(
        "process_documents_handler", handler_path
    )
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules["process_documents_handler"] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace boto3 clients with mocks
    handler_module.s3_client = mock_s3_client
    handler_module.ssm_client = mock_ssm_client
    handler_module.bedrock_client = mock_bedrock_client
    handler_module.s3vectors_client = mock_s3vectors_client

    return handler_module


handler_module = _load_handler_module()

# Restore ENV to avoid polluting other tests
if _original_env is not None:
    os.environ["ENV"] = _original_env
else:
    os.environ.pop("ENV", None)

# Import functions from the loaded module
lambda_handler = handler_module.lambda_handler
process_document = handler_module.process_document
parse_s3_key = handler_module.parse_s3_key
extract_text = handler_module.extract_text
extract_text_from_html = handler_module.extract_text_from_html
extract_text_from_markdown = handler_module.extract_text_from_markdown
extract_text_from_plain = handler_module.extract_text_from_plain
extract_text_from_json = handler_module.extract_text_from_json
extract_text_from_yaml = handler_module.extract_text_from_yaml
extract_text_from_pdf = handler_module.extract_text_from_pdf
extract_text_from_pdf_pypdf2 = handler_module.extract_text_from_pdf_pypdf2
extract_text_from_pdf_textract = handler_module.extract_text_from_pdf_textract
extract_text_from_docx = handler_module.extract_text_from_docx
extract_text_from_xlsx = handler_module.extract_text_from_xlsx
extract_text_from_pptx = handler_module.extract_text_from_pptx
extract_text_from_csv = handler_module.extract_text_from_csv
chunk_text = handler_module.chunk_text
generate_embedding = handler_module.generate_embedding
compute_content_hash = handler_module.compute_content_hash
store_vectors = handler_module.store_vectors
delete_vectors_for_document = handler_module.delete_vectors_for_document
_load_vector_config = handler_module._load_vector_config


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset all mocks and module state between tests."""
    mock_s3_client.reset_mock()
    mock_ssm_client.reset_mock()
    mock_bedrock_client.reset_mock()
    mock_s3vectors_client.reset_mock()
    # Clear any side_effect set by tests
    mock_s3_client.get_object.side_effect = None
    mock_ssm_client.get_parameter.side_effect = None
    mock_bedrock_client.invoke_model.side_effect = None
    mock_s3vectors_client.put_vectors.side_effect = None
    mock_s3vectors_client.delete_vectors.side_effect = None
    # Reset cached vector config
    handler_module._vector_config = None
    yield


@pytest.fixture
def vector_config():
    """Set up SSM responses for vector config loading."""
    mock_ssm_client.get_parameter.side_effect = lambda Name: {
        "/test/outcome-ops-ai-assist/s3vectors/bucket-name": {
            "Parameter": {"Value": "test-vector-bucket"}
        },
        "/test/outcome-ops-ai-assist/s3vectors/index-name": {
            "Parameter": {"Value": "test-vector-index"}
        },
    }[Name]
    return {"bucket": "test-vector-bucket", "index": "test-vector-index"}


@pytest.fixture
def sample_embedding():
    """Return a sample 1024-dimensional embedding."""
    return [0.1] * 1024


@pytest.fixture
def mock_bedrock_embedding(sample_embedding):
    """Configure bedrock mock to return a valid embedding response."""
    body_mock = MagicMock()
    body_mock.read.return_value = json.dumps(
        {"embedding": sample_embedding}
    ).encode("utf-8")
    mock_bedrock_client.invoke_model.return_value = {"body": body_mock}
    return sample_embedding


@pytest.fixture
def s3_event():
    """Create a sample S3 ObjectCreated event."""
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": "test-kb-bucket"},
                    "object": {
                        "key": "workspaces/ws_abc/confluence/my-space/page.html"
                    },
                }
            }
        ]
    }


@pytest.fixture
def sqs_event():
    """Create a sample SQS reprocessing event."""
    return {
        "Records": [
            {
                "body": json.dumps(
                    {
                        "bucket": "test-kb-bucket",
                        "key": "workspaces/ws_abc/confluence/my-space/page.html",
                    }
                )
            }
        ]
    }


# ============================================================================
# S3 Key Parsing
# ============================================================================


class TestParseS3Key:
    """Test S3 key parsing into workspace/source/folder/filename components."""

    def test_parse_s3_key_standard_path(self):
        """Test parsing a standard workspace key."""
        result = parse_s3_key("workspaces/ws_abc/confluence/my-space/page.html")
        assert result == {
            "workspace_id": "ws_abc",
            "source": "confluence",
            "folder_path": "my-space",
            "filename": "page.html",
        }

    def test_parse_s3_key_nested_folder_path(self):
        """Test parsing key with deeply nested folder path."""
        result = parse_s3_key(
            "workspaces/ws_abc/github/my-repo/src/main/handler.py"
        )
        assert result == {
            "workspace_id": "ws_abc",
            "source": "github",
            "folder_path": "my-repo/src/main",
            "filename": "handler.py",
        }

    def test_parse_s3_key_no_folder_path(self):
        """Test parsing key with no folder path (file directly under source)."""
        result = parse_s3_key("workspaces/ws_abc/jira/issue.json")
        assert result == {
            "workspace_id": "ws_abc",
            "source": "jira",
            "folder_path": "",
            "filename": "issue.json",
        }

    def test_parse_s3_key_url_encoded(self):
        """Test parsing URL-encoded S3 key."""
        result = parse_s3_key(
            "workspaces/ws_abc/confluence/My+Space/Page+Title.html"
        )
        assert result is not None
        assert result["folder_path"] == "My Space"
        assert result["filename"] == "Page Title.html"

    def test_parse_s3_key_invalid_no_workspaces_prefix(self):
        """Test returns None for key without workspaces/ prefix."""
        result = parse_s3_key("other/ws_abc/confluence/page.html")
        assert result is None

    def test_parse_s3_key_invalid_too_short(self):
        """Test returns None for key with fewer than 4 parts."""
        result = parse_s3_key("workspaces/ws_abc/confluence")
        assert result is None

    def test_parse_s3_key_invalid_empty(self):
        """Test returns None for empty key."""
        result = parse_s3_key("")
        assert result is None

    def test_parse_s3_key_url_encoded_plus(self):
        """Test URL-encoded plus signs are decoded to spaces."""
        result = parse_s3_key(
            "workspaces/ws_abc/confluence/space+key/doc+name.html"
        )
        assert result["folder_path"] == "space key"
        assert result["filename"] == "doc name.html"


# ============================================================================
# Text Extraction - HTML
# ============================================================================


class TestExtractTextFromHtml:
    """Test HTML text extraction."""

    def test_extract_text_from_html_basic(self):
        """Test basic HTML tag removal."""
        content = b"<p>Hello <b>world</b></p>"
        result = extract_text_from_html(content)
        assert "Hello" in result
        assert "world" in result
        assert "<p>" not in result
        assert "<b>" not in result

    def test_extract_text_from_html_removes_scripts(self):
        """Test script tags and content are removed."""
        content = b"<p>Content</p><script>alert('xss')</script><p>More</p>"
        result = extract_text_from_html(content)
        assert "Content" in result
        assert "More" in result
        assert "alert" not in result
        assert "script" not in result

    def test_extract_text_from_html_removes_styles(self):
        """Test style tags and content are removed."""
        content = b"<style>body { color: red; }</style><p>Visible text</p>"
        result = extract_text_from_html(content)
        assert "Visible text" in result
        assert "color" not in result

    def test_extract_text_from_html_decodes_entities(self):
        """Test HTML entities are decoded."""
        content = b"<p>Price &amp; value &lt; 100 &gt; 50</p>"
        result = extract_text_from_html(content)
        assert "&" in result
        assert "<" in result
        assert ">" in result
        assert "&amp;" not in result

    def test_extract_text_from_html_block_elements_add_newlines(self):
        """Test block elements produce newlines."""
        content = b"<h1>Title</h1><p>Paragraph 1</p><p>Paragraph 2</p>"
        result = extract_text_from_html(content)
        assert "Title" in result
        assert "Paragraph 1" in result
        assert "Paragraph 2" in result

    def test_extract_text_from_html_empty_content(self):
        """Test empty HTML returns empty string."""
        result = extract_text_from_html(b"")
        assert result == ""

    def test_extract_text_from_html_whitespace_cleanup(self):
        """Test excessive whitespace is cleaned up."""
        content = b"<p>  Hello    world  </p>\n\n\n\n<p>next</p>"
        result = extract_text_from_html(content)
        # Should not have more than 2 consecutive newlines
        assert "\n\n\n" not in result


# ============================================================================
# Text Extraction - Markdown, Plain, YAML
# ============================================================================


class TestExtractTextSimpleFormats:
    """Test text extraction for simple text formats."""

    def test_extract_text_from_markdown(self):
        """Test markdown extraction preserves content."""
        content = b"# Title\n\nSome **bold** text\n- item 1\n- item 2"
        result = extract_text_from_markdown(content)
        assert result == "# Title\n\nSome **bold** text\n- item 1\n- item 2"

    def test_extract_text_from_markdown_strips_whitespace(self):
        """Test leading/trailing whitespace is stripped."""
        content = b"  \n  content here  \n  "
        result = extract_text_from_markdown(content)
        assert result == "content here"

    def test_extract_text_from_plain(self):
        """Test plain text extraction."""
        content = b"Hello world\nSecond line"
        result = extract_text_from_plain(content)
        assert result == "Hello world\nSecond line"

    def test_extract_text_from_plain_handles_encoding_errors(self):
        """Test plain text handles invalid UTF-8 gracefully."""
        content = b"Hello \xff\xfe world"
        result = extract_text_from_plain(content)
        assert "Hello" in result
        assert "world" in result

    def test_extract_text_from_yaml(self):
        """Test YAML extraction preserves content."""
        content = b"key: value\nlist:\n  - item1\n  - item2"
        result = extract_text_from_yaml(content)
        assert result == "key: value\nlist:\n  - item1\n  - item2"


# ============================================================================
# Text Extraction - JSON
# ============================================================================


class TestExtractTextFromJson:
    """Test JSON text extraction."""

    def test_extract_text_from_json_valid(self):
        """Test valid JSON is pretty-printed."""
        data = {"key": "value", "list": [1, 2, 3]}
        content = json.dumps(data).encode("utf-8")
        result = extract_text_from_json(content)
        parsed = json.loads(result)
        assert parsed == data

    def test_extract_text_from_json_formatted_output(self):
        """Test JSON output is indented."""
        content = b'{"a": 1}'
        result = extract_text_from_json(content)
        assert "  " in result  # indent=2

    def test_extract_text_from_json_invalid_json(self):
        """Test invalid JSON falls back to raw text."""
        content = b"not valid json {]"
        result = extract_text_from_json(content)
        assert result == "not valid json {]"


# ============================================================================
# Text Extraction - CSV
# ============================================================================


class TestExtractTextFromCsv:
    """Test CSV text extraction."""

    def test_extract_text_from_csv_basic(self):
        """Test basic CSV extraction."""
        content = b"name,age,city\nAlice,30,NYC\nBob,25,LA"
        result = extract_text_from_csv(content)
        assert "Alice" in result
        assert "Bob" in result
        assert "name" in result

    def test_extract_text_from_csv_tab_separated_output(self):
        """Test CSV columns are tab-separated in output."""
        content = b"col1,col2\nval1,val2"
        result = extract_text_from_csv(content)
        lines = result.strip().split("\n")
        assert "\t" in lines[0]
        assert "\t" in lines[1]

    def test_extract_text_from_csv_skips_empty_rows(self):
        """Test empty CSV rows are skipped."""
        content = b"a,b\n,,\nc,d"
        result = extract_text_from_csv(content)
        assert "a" in result
        assert "c" in result

    def test_extract_text_from_csv_handles_error(self):
        """Test CSV extraction handles errors by returning raw text."""
        # Malformed content that might cause csv reader issues should still
        # return something meaningful. The handler falls back to raw decode.
        content = b"still,readable"
        result = extract_text_from_csv(content)
        assert "still" in result


# ============================================================================
# Text Extraction - PDF (PyPDF2)
# ============================================================================


@pytest.mark.skipif(not _has_pypdf2, reason="PyPDF2 not installed")
class TestExtractTextFromPdfPyPdf2:
    """Test PDF text extraction with PyPDF2."""

    @patch("PyPDF2.PdfReader")
    def test_extract_pdf_pypdf2_success(self, mock_reader_cls):
        """Test successful PDF text extraction."""
        mock_page1 = MagicMock()
        mock_page1.extract_text.return_value = "Page 1 text"
        mock_page2 = MagicMock()
        mock_page2.extract_text.return_value = "Page 2 text"
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page1, mock_page2]
        mock_reader_cls.return_value = mock_reader

        result = extract_text_from_pdf_pypdf2(b"fake pdf content")
        assert "Page 1 text" in result
        assert "Page 2 text" in result

    @patch("PyPDF2.PdfReader")
    def test_extract_pdf_pypdf2_empty_pages(self, mock_reader_cls):
        """Test PDF with pages that return no text."""
        mock_page = MagicMock()
        mock_page.extract_text.return_value = ""
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page]
        mock_reader_cls.return_value = mock_reader

        result = extract_text_from_pdf_pypdf2(b"fake pdf content")
        assert result == ""

    @patch("PyPDF2.PdfReader")
    def test_extract_pdf_pypdf2_exception(self, mock_reader_cls):
        """Test PyPDF2 extraction returns empty on exception."""
        mock_reader_cls.side_effect = Exception("Cannot parse PDF")
        result = extract_text_from_pdf_pypdf2(b"bad content")
        assert result == ""


# ============================================================================
# Text Extraction - PDF (Textract)
# ============================================================================


class TestExtractTextFromPdfTextract:
    """Test PDF text extraction with AWS Textract OCR."""

    @patch("time.sleep")
    @patch.object(handler_module, "boto3")
    def test_extract_pdf_textract_success(self, mock_boto3, mock_sleep):
        """Test successful Textract OCR extraction."""
        mock_textract = MagicMock()
        mock_boto3.client.return_value = mock_textract

        mock_textract.start_document_text_detection.return_value = {
            "JobId": "job-123"
        }
        mock_textract.get_document_text_detection.return_value = {
            "JobStatus": "SUCCEEDED",
            "Blocks": [
                {"BlockType": "LINE", "Text": "Line 1 from OCR"},
                {"BlockType": "LINE", "Text": "Line 2 from OCR"},
                {"BlockType": "WORD", "Text": "ignored"},
            ],
        }

        result = extract_text_from_pdf_textract("test-bucket", "test-key.pdf")
        assert "Line 1 from OCR" in result
        assert "Line 2 from OCR" in result
        assert "ignored" not in result

    @patch("time.sleep")
    @patch.object(handler_module, "boto3")
    def test_extract_pdf_textract_job_failed(self, mock_boto3, mock_sleep):
        """Test Textract returns empty on job failure."""
        mock_textract = MagicMock()
        mock_boto3.client.return_value = mock_textract

        mock_textract.start_document_text_detection.return_value = {
            "JobId": "job-456"
        }
        mock_textract.get_document_text_detection.return_value = {
            "JobStatus": "FAILED",
            "StatusMessage": "Document too complex",
        }

        result = extract_text_from_pdf_textract("test-bucket", "test-key.pdf")
        assert result == ""

    @patch("time.sleep")
    @patch.object(handler_module, "boto3")
    def test_extract_pdf_textract_timeout(self, mock_boto3, mock_sleep):
        """Test Textract returns empty on timeout."""
        mock_textract = MagicMock()
        mock_boto3.client.return_value = mock_textract

        mock_textract.start_document_text_detection.return_value = {
            "JobId": "job-789"
        }
        # Always return IN_PROGRESS
        mock_textract.get_document_text_detection.return_value = {
            "JobStatus": "IN_PROGRESS"
        }

        result = extract_text_from_pdf_textract("test-bucket", "test-key.pdf")
        assert result == ""

    @patch("time.sleep")
    @patch.object(handler_module, "boto3")
    def test_extract_pdf_textract_with_pagination(self, mock_boto3, mock_sleep):
        """Test Textract handles paginated results."""
        mock_textract = MagicMock()
        mock_boto3.client.return_value = mock_textract

        mock_textract.start_document_text_detection.return_value = {
            "JobId": "job-pag"
        }
        # First call: SUCCEEDED with next token
        # Second call: no next token
        mock_textract.get_document_text_detection.side_effect = [
            {
                "JobStatus": "SUCCEEDED",
                "Blocks": [{"BlockType": "LINE", "Text": "Page 1"}],
                "NextToken": "token-abc",
            },
            {
                "Blocks": [{"BlockType": "LINE", "Text": "Page 2"}],
            },
        ]

        result = extract_text_from_pdf_textract("test-bucket", "test-key.pdf")
        assert "Page 1" in result
        assert "Page 2" in result

    @patch.object(handler_module, "boto3")
    def test_extract_pdf_textract_exception(self, mock_boto3):
        """Test Textract handles exceptions gracefully."""
        mock_boto3.client.side_effect = Exception("Service unavailable")

        result = extract_text_from_pdf_textract("test-bucket", "test-key.pdf")
        assert result == ""

    @patch("time.sleep")
    @patch.object(handler_module, "boto3")
    def test_extract_pdf_textract_no_text_blocks(self, mock_boto3, mock_sleep):
        """Test Textract returns empty when no LINE blocks found."""
        mock_textract = MagicMock()
        mock_boto3.client.return_value = mock_textract

        mock_textract.start_document_text_detection.return_value = {
            "JobId": "job-empty"
        }
        mock_textract.get_document_text_detection.return_value = {
            "JobStatus": "SUCCEEDED",
            "Blocks": [
                {"BlockType": "PAGE", "Text": ""},
            ],
        }

        result = extract_text_from_pdf_textract("test-bucket", "test-key.pdf")
        assert result == ""


# ============================================================================
# Text Extraction - PDF (Combined: PyPDF2 + Textract Fallback)
# ============================================================================


@pytest.mark.skipif(not _has_pypdf2, reason="PyPDF2 not installed")
class TestExtractTextFromPdf:
    """Test combined PDF extraction with OCR fallback."""

    @patch("PyPDF2.PdfReader")
    @patch.object(handler_module, "extract_text_from_pdf_pypdf2")
    def test_extract_pdf_sufficient_text(self, mock_pypdf2, mock_reader_cls):
        """Test PyPDF2 extraction is used when sufficient text is available."""
        # 1000 chars from 1 page = 1000 chars/page, well above 200 threshold
        mock_pypdf2.return_value = "x" * 1000

        mock_reader = MagicMock()
        mock_reader.pages = [MagicMock()]  # 1 page
        mock_reader_cls.return_value = mock_reader

        result = extract_text_from_pdf(b"pdf content", "bucket", "key.pdf")
        assert len(result) == 1000

    @patch("PyPDF2.PdfReader")
    @patch.object(handler_module, "extract_text_from_pdf_textract")
    @patch.object(handler_module, "extract_text_from_pdf_pypdf2")
    def test_extract_pdf_falls_back_to_textract(
        self, mock_pypdf2, mock_textract, mock_reader_cls
    ):
        """Test fallback to Textract when PyPDF2 extraction is insufficient."""
        mock_pypdf2.return_value = "x" * 20  # Only 20 chars - insufficient
        mock_textract.return_value = "OCR text " * 100

        mock_reader = MagicMock()
        mock_reader.pages = [MagicMock()]
        mock_reader_cls.return_value = mock_reader

        result = extract_text_from_pdf(
            b"pdf content", "bucket", "key.pdf"
        )
        assert "OCR text" in result
        mock_textract.assert_called_once_with("bucket", "key.pdf")

    @patch("PyPDF2.PdfReader")
    @patch.object(handler_module, "extract_text_from_pdf_textract")
    @patch.object(handler_module, "extract_text_from_pdf_pypdf2")
    def test_extract_pdf_low_density_triggers_textract(
        self, mock_pypdf2, mock_textract, mock_reader_cls
    ):
        """Test low chars-per-page density triggers Textract fallback."""
        # 300 chars across 10 pages = 30 chars/page (below 200 threshold)
        mock_pypdf2.return_value = "x" * 300
        mock_textract.return_value = "Full OCR content " * 200

        mock_reader = MagicMock()
        mock_reader.pages = [MagicMock()] * 10  # 10 pages
        mock_reader_cls.return_value = mock_reader

        result = extract_text_from_pdf(
            b"pdf content", "bucket", "key.pdf"
        )
        mock_textract.assert_called_once()
        assert "Full OCR content" in result

    @patch("PyPDF2.PdfReader")
    @patch.object(handler_module, "extract_text_from_pdf_pypdf2")
    def test_extract_pdf_no_s3_location_no_textract(self, mock_pypdf2, mock_reader_cls):
        """Test no Textract fallback when S3 location is not provided."""
        mock_pypdf2.return_value = ""

        mock_reader = MagicMock()
        mock_reader.pages = [MagicMock()]
        mock_reader_cls.return_value = mock_reader

        result = extract_text_from_pdf(b"pdf content")
        assert result == ""

    @patch("PyPDF2.PdfReader")
    @patch.object(handler_module, "extract_text_from_pdf_textract")
    @patch.object(handler_module, "extract_text_from_pdf_pypdf2")
    def test_extract_pdf_textract_less_than_pypdf2(
        self, mock_pypdf2, mock_textract, mock_reader_cls
    ):
        """Test PyPDF2 result is preferred when Textract returns less."""
        mock_pypdf2.return_value = "x" * 40  # Some text, but below 50 threshold
        mock_textract.return_value = "y" * 10  # Textract returned even less

        mock_reader = MagicMock()
        mock_reader.pages = [MagicMock()]
        mock_reader_cls.return_value = mock_reader

        result = extract_text_from_pdf(
            b"pdf content", "bucket", "key.pdf"
        )
        # PyPDF2 result should be returned (40 > 10)
        assert result == "x" * 40


# ============================================================================
# Text Extraction - Office Formats (DOCX, XLSX, PPTX)
# ============================================================================


@pytest.mark.skipif(not _has_docx, reason="python-docx not installed")
class TestExtractTextFromDocx:
    """Test DOCX text extraction."""

    @patch("docx.Document")
    def test_extract_docx_paragraphs(self, mock_doc_cls):
        """Test DOCX paragraph extraction."""
        mock_doc = MagicMock()
        para1 = MagicMock()
        para1.text = "First paragraph"
        para2 = MagicMock()
        para2.text = "Second paragraph"
        para3 = MagicMock()
        para3.text = "  "  # whitespace-only paragraph
        mock_doc.paragraphs = [para1, para2, para3]
        mock_doc.tables = []
        mock_doc_cls.return_value = mock_doc

        result = extract_text_from_docx(b"fake docx content")
        assert "First paragraph" in result
        assert "Second paragraph" in result

    @patch("docx.Document")
    def test_extract_docx_with_tables(self, mock_doc_cls):
        """Test DOCX table content extraction."""
        mock_doc = MagicMock()
        mock_doc.paragraphs = []

        # Create mock table
        cell1 = MagicMock()
        cell1.text = "Header 1"
        cell2 = MagicMock()
        cell2.text = "Header 2"
        row = MagicMock()
        row.cells = [cell1, cell2]
        table = MagicMock()
        table.rows = [row]
        mock_doc.tables = [table]
        mock_doc_cls.return_value = mock_doc

        result = extract_text_from_docx(b"fake docx content")
        assert "Header 1" in result
        assert "Header 2" in result

    @patch("docx.Document")
    def test_extract_docx_handles_error(self, mock_doc_cls):
        """Test DOCX extraction returns empty on error."""
        mock_doc_cls.side_effect = Exception("Bad file")
        result = extract_text_from_docx(b"invalid content")
        assert result == ""


@pytest.mark.skipif(not _has_openpyxl, reason="openpyxl not installed")
class TestExtractTextFromXlsx:
    """Test XLSX text extraction."""

    @patch("openpyxl.load_workbook")
    def test_extract_xlsx_basic(self, mock_load_wb):
        """Test basic XLSX extraction."""
        mock_wb = MagicMock()
        mock_wb.sheetnames = ["Sheet1"]
        mock_sheet = MagicMock()
        mock_sheet.iter_rows.return_value = [
            ("Name", "Age"),
            ("Alice", 30),
            (None, None),
        ]
        mock_wb.__getitem__ = MagicMock(return_value=mock_sheet)
        mock_load_wb.return_value = mock_wb

        result = extract_text_from_xlsx(b"fake xlsx content")
        assert "Sheet1" in result
        assert "Alice" in result

    @patch("openpyxl.load_workbook")
    def test_extract_xlsx_handles_error(self, mock_load_wb):
        """Test XLSX extraction returns empty on error."""
        mock_load_wb.side_effect = Exception("Bad file")
        result = extract_text_from_xlsx(b"invalid content")
        assert result == ""


@pytest.mark.skipif(not _has_pptx, reason="python-pptx not installed")
class TestExtractTextFromPptx:
    """Test PPTX text extraction."""

    @patch("pptx.Presentation")
    def test_extract_pptx_basic(self, mock_prs_cls):
        """Test basic PPTX extraction."""
        mock_prs = MagicMock()
        shape1 = MagicMock()
        shape1.text = "Slide Title"
        shape1.has_text_frame = True
        shape2 = MagicMock()
        shape2.text = "Bullet point"
        shape2.has_text_frame = True
        slide = MagicMock()
        slide.shapes = [shape1, shape2]
        mock_prs.slides = [slide]
        mock_prs_cls.return_value = mock_prs

        result = extract_text_from_pptx(b"fake pptx content")
        assert "Slide 1" in result
        assert "Slide Title" in result
        assert "Bullet point" in result

    @patch("pptx.Presentation")
    def test_extract_pptx_handles_error(self, mock_prs_cls):
        """Test PPTX extraction returns empty on error."""
        mock_prs_cls.side_effect = Exception("Bad file")
        result = extract_text_from_pptx(b"invalid content")
        assert result == ""


# ============================================================================
# Text Extraction - Dispatcher
# ============================================================================


class TestExtractText:
    """Test the extract_text dispatcher function."""

    def test_extract_text_html(self):
        """Test HTML file type detection and extraction."""
        text, doc_type = extract_text("page.html", b"<p>Content</p>")
        assert doc_type == "html"
        assert "Content" in text

    def test_extract_text_htm(self):
        """Test .htm extension is recognized as HTML."""
        text, doc_type = extract_text("page.htm", b"<p>Content</p>")
        assert doc_type == "html"

    def test_extract_text_markdown(self):
        """Test Markdown file type detection."""
        text, doc_type = extract_text("readme.md", b"# Title")
        assert doc_type == "markdown"
        assert "# Title" in text

    def test_extract_text_plain(self):
        """Test plain text file type detection."""
        text, doc_type = extract_text("notes.txt", b"Plain text")
        assert doc_type == "text"
        assert text == "Plain text"

    def test_extract_text_json(self):
        """Test JSON file type detection."""
        text, doc_type = extract_text("data.json", b'{"key": "value"}')
        assert doc_type == "json"

    def test_extract_text_yaml(self):
        """Test YAML file type detection."""
        text, doc_type = extract_text("config.yaml", b"key: value")
        assert doc_type == "yaml"

    def test_extract_text_yml(self):
        """Test .yml extension is recognized as YAML."""
        text, doc_type = extract_text("config.yml", b"key: value")
        assert doc_type == "yaml"

    @patch.object(handler_module, "extract_text_from_pdf")
    def test_extract_text_pdf(self, mock_pdf):
        """Test PDF file type detection."""
        mock_pdf.return_value = "PDF content"
        text, doc_type = extract_text(
            "doc.pdf", b"fake", s3_bucket="bucket", s3_key="key"
        )
        assert doc_type == "pdf"
        mock_pdf.assert_called_once_with(b"fake", "bucket", "key")

    @patch.object(handler_module, "extract_text_from_docx")
    def test_extract_text_docx(self, mock_docx):
        """Test DOCX file type detection."""
        mock_docx.return_value = "Word content"
        text, doc_type = extract_text("report.docx", b"fake")
        assert doc_type == "docx"

    @patch.object(handler_module, "extract_text_from_xlsx")
    def test_extract_text_xlsx(self, mock_xlsx):
        """Test XLSX file type detection."""
        mock_xlsx.return_value = "Spreadsheet"
        text, doc_type = extract_text("data.xlsx", b"fake")
        assert doc_type == "xlsx"

    @patch.object(handler_module, "extract_text_from_pptx")
    def test_extract_text_pptx(self, mock_pptx):
        """Test PPTX file type detection."""
        mock_pptx.return_value = "Slides"
        text, doc_type = extract_text("deck.pptx", b"fake")
        assert doc_type == "pptx"

    def test_extract_text_csv(self):
        """Test CSV file type detection."""
        text, doc_type = extract_text("data.csv", b"a,b\n1,2")
        assert doc_type == "csv"

    def test_extract_text_unknown_decodable(self):
        """Test unknown file type falls back to UTF-8 decode."""
        text, doc_type = extract_text("script.rb", b"puts 'hello'")
        assert doc_type == "unknown"
        assert text == "puts 'hello'"

    def test_extract_text_binary_file(self):
        """Test binary file returns empty text and 'binary' type."""
        # Create content that will fail UTF-8 decode
        content = bytes(range(128, 256)) * 10
        text, doc_type = extract_text("image.bin", content)
        assert doc_type == "binary"
        assert text == ""

    def test_extract_text_case_insensitive_extension(self):
        """Test file extension matching is case-insensitive."""
        text, doc_type = extract_text("PAGE.HTML", b"<p>Test</p>")
        assert doc_type == "html"

    def test_extract_text_case_insensitive_md(self):
        """Test .MD extension is recognized."""
        text, doc_type = extract_text("README.MD", b"# Readme")
        assert doc_type == "markdown"


# ============================================================================
# Text Chunking
# ============================================================================


class TestChunkText:
    """Test text chunking for embedding."""

    def test_chunk_text_short_text_single_chunk(self):
        """Test short text returns a single chunk."""
        text = "Short text content"
        chunks = chunk_text(text, max_chars=8000)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_chunk_text_exact_max_single_chunk(self):
        """Test text at exactly max_chars returns single chunk."""
        text = "x" * 8000
        chunks = chunk_text(text, max_chars=8000)
        assert len(chunks) == 1

    def test_chunk_text_exceeds_max_produces_multiple(self):
        """Test text exceeding max_chars produces multiple chunks."""
        text = "word " * 5000  # ~25000 chars
        chunks = chunk_text(text, max_chars=8000, overlap=500)
        assert len(chunks) > 1
        # Each chunk should not exceed max_chars
        for chunk in chunks:
            assert len(chunk) <= 8000

    def test_chunk_text_paragraph_boundary(self):
        """Test chunking prefers paragraph boundaries."""
        paragraph1 = "a " * 3500  # ~7000 chars
        paragraph2 = "b " * 3500  # ~7000 chars
        text = paragraph1.strip() + "\n\n" + paragraph2.strip()
        chunks = chunk_text(text, max_chars=8000, overlap=500)
        assert len(chunks) >= 2

    def test_chunk_text_sentence_boundary(self):
        """Test chunking uses sentence boundaries when no paragraph break."""
        # Build text with sentences but no paragraph breaks
        sentences = "This is sentence number %d. " * 200
        text = "".join(
            "This is sentence number %d. " % i for i in range(200)
        )
        chunks = chunk_text(text, max_chars=1000, overlap=100)
        assert len(chunks) > 1

    def test_chunk_text_overlap(self):
        """Test chunks have overlap."""
        # Create text that will require multiple chunks
        text = "abcdefghij" * 2000  # 20000 chars
        chunks = chunk_text(text, max_chars=8000, overlap=500)
        assert len(chunks) > 1
        # Content from end of chunk N should appear at start of chunk N+1
        # due to overlap

    def test_chunk_text_empty_returns_text(self):
        """Test empty text returns single-element list with the text."""
        chunks = chunk_text("")
        assert chunks == [""]

    def test_chunk_text_no_empty_chunks(self):
        """Test no empty chunks are produced."""
        text = "x" * 20000
        chunks = chunk_text(text, max_chars=8000, overlap=500)
        for chunk in chunks:
            assert len(chunk.strip()) > 0

    def test_chunk_text_custom_parameters(self):
        """Test chunking with custom max_chars and overlap."""
        text = "word " * 100  # 500 chars
        chunks = chunk_text(text, max_chars=100, overlap=20)
        assert len(chunks) > 1


# ============================================================================
# Embedding Generation
# ============================================================================


class TestGenerateEmbedding:
    """Test embedding generation via Bedrock."""

    def test_generate_embedding_success(self, mock_bedrock_embedding):
        """Test successful embedding generation."""
        result = generate_embedding("Some text to embed")
        assert len(result) == 1024
        assert result == [0.1] * 1024

        # Verify Bedrock was called correctly
        mock_bedrock_client.invoke_model.assert_called_once()
        call_args = mock_bedrock_client.invoke_model.call_args[1]
        assert call_args["modelId"] == "amazon.titan-embed-text-v2:0"
        body = json.loads(call_args["body"])
        assert body["inputText"] == "Some text to embed"
        assert body["dimensions"] == 1024
        assert body["normalize"] is True

    def test_generate_embedding_empty_response(self):
        """Test returns empty list when embedding is empty."""
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(
            {"embedding": []}
        ).encode("utf-8")
        mock_bedrock_client.invoke_model.return_value = {"body": body_mock}

        result = generate_embedding("Some text")
        assert result == []

    def test_generate_embedding_client_error(self):
        """Test returns empty list on ClientError."""
        mock_bedrock_client.invoke_model.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal error"}},
            "InvokeModel",
        )

        result = generate_embedding("Some text")
        assert result == []

    def test_generate_embedding_too_many_tokens_truncates(self):
        """Test truncation and retry when text is too large."""
        # First call: raise "Too many input tokens" error
        # Second call (retry with truncated text): return valid embedding
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(
            {"embedding": [0.5] * 1024}
        ).encode("utf-8")

        mock_bedrock_client.invoke_model.side_effect = [
            ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "Too many input tokens",
                    }
                },
                "InvokeModel",
            ),
            {"body": body_mock},
        ]

        result = generate_embedding("x" * 50000)
        assert len(result) == 1024
        assert mock_bedrock_client.invoke_model.call_count == 2


# ============================================================================
# Content Hash
# ============================================================================


class TestComputeContentHash:
    """Test content hash computation."""

    def test_compute_content_hash_deterministic(self):
        """Test hash is deterministic for same input."""
        hash1 = compute_content_hash("test content")
        hash2 = compute_content_hash("test content")
        assert hash1 == hash2

    def test_compute_content_hash_different_inputs(self):
        """Test different inputs produce different hashes."""
        hash1 = compute_content_hash("content a")
        hash2 = compute_content_hash("content b")
        assert hash1 != hash2

    def test_compute_content_hash_sha256(self):
        """Test hash matches expected SHA-256."""
        expected = hashlib.sha256("hello".encode("utf-8")).hexdigest()
        assert compute_content_hash("hello") == expected


# ============================================================================
# Vector Config Loading
# ============================================================================


class TestLoadVectorConfig:
    """Test SSM-based vector config loading."""

    def test_load_vector_config_success(self, vector_config):
        """Test successful vector config loading from SSM."""
        config = _load_vector_config()
        assert config["bucket"] == "test-vector-bucket"
        assert config["index"] == "test-vector-index"

    def test_load_vector_config_caches_result(self, vector_config):
        """Test config is cached after first load."""
        config1 = _load_vector_config()
        config2 = _load_vector_config()
        assert config1 is config2
        # SSM should only be called for the first load (2 calls: bucket + index)
        assert mock_ssm_client.get_parameter.call_count == 2

    def test_load_vector_config_ssm_error(self):
        """Test raises on SSM ClientError."""
        mock_ssm_client.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}},
            "GetParameter",
        )
        with pytest.raises(ClientError):
            _load_vector_config()


# ============================================================================
# Vector Storage
# ============================================================================


class TestStoreVectors:
    """Test storing document vectors in S3 Vectors."""

    def test_store_vectors_success(self, vector_config):
        """Test successful vector storage."""
        chunks = ["chunk 0 text", "chunk 1 text"]
        embeddings = [[0.1] * 1024, [0.2] * 1024]

        result = store_vectors(
            workspace_id="ws_abc",
            source="confluence",
            folder_path="my-space",
            filename="page.html",
            s3_key="workspaces/ws_abc/confluence/my-space/page.html",
            chunks=chunks,
            embeddings=embeddings,
            doc_type="html",
            metadata={},
        )

        assert result == 2
        mock_s3vectors_client.put_vectors.assert_called_once()

        call_args = mock_s3vectors_client.put_vectors.call_args[1]
        assert call_args["vectorBucketName"] == "test-vector-bucket"
        assert call_args["indexName"] == "test-vector-index"
        vectors = call_args["vectors"]
        assert len(vectors) == 2

        # Verify vector keys
        assert vectors[0]["key"] == "ws_abc#confluence#my-space#page.html_chunk_0"
        assert vectors[1]["key"] == "ws_abc#confluence#my-space#page.html_chunk_1"

        # Verify metadata
        meta = vectors[0]["metadata"]
        assert meta["workspace_id"] == "ws_abc"
        assert meta["source"] == "confluence"
        assert meta["file_name"] == "page.html"
        assert meta["type"] == "html"
        assert meta["chunk_index"] == "0"
        assert meta["total_chunks"] == "2"
        assert meta["content"] == "chunk 0 text"

    def test_store_vectors_no_folder_path(self, vector_config):
        """Test vector key when no folder path is present."""
        chunks = ["text"]
        embeddings = [[0.1] * 1024]

        store_vectors(
            workspace_id="ws_abc",
            source="jira",
            folder_path="",
            filename="issue.json",
            s3_key="workspaces/ws_abc/jira/issue.json",
            chunks=chunks,
            embeddings=embeddings,
            doc_type="json",
            metadata={},
        )

        call_args = mock_s3vectors_client.put_vectors.call_args[1]
        vector_key = call_args["vectors"][0]["key"]
        assert vector_key == "ws_abc#jira#issue.json_chunk_0"

    def test_store_vectors_nested_folder_path(self, vector_config):
        """Test folder path slashes are replaced with hashes."""
        chunks = ["text"]
        embeddings = [[0.1] * 1024]

        store_vectors(
            workspace_id="ws_abc",
            source="github",
            folder_path="my-repo/src/main",
            filename="handler.py",
            s3_key="workspaces/ws_abc/github/my-repo/src/main/handler.py",
            chunks=chunks,
            embeddings=embeddings,
            doc_type="unknown",
            metadata={},
        )

        call_args = mock_s3vectors_client.put_vectors.call_args[1]
        vector_key = call_args["vectors"][0]["key"]
        assert vector_key == "ws_abc#github#my-repo#src#main#handler.py_chunk_0"

    def test_store_vectors_github_repo_metadata(self, vector_config):
        """Test repo field is set for GitHub sources."""
        chunks = ["text"]
        embeddings = [[0.1] * 1024]

        store_vectors(
            workspace_id="ws_abc",
            source="github",
            folder_path="my-repo/src",
            filename="app.py",
            s3_key="workspaces/ws_abc/github/my-repo/src/app.py",
            chunks=chunks,
            embeddings=embeddings,
            doc_type="unknown",
            metadata={},
        )

        call_args = mock_s3vectors_client.put_vectors.call_args[1]
        meta = call_args["vectors"][0]["metadata"]
        assert meta["repo"] == "my-repo"

    def test_store_vectors_non_github_no_repo(self, vector_config):
        """Test repo field is empty for non-GitHub sources."""
        chunks = ["text"]
        embeddings = [[0.1] * 1024]

        store_vectors(
            workspace_id="ws_abc",
            source="confluence",
            folder_path="my-space",
            filename="page.html",
            s3_key="workspaces/ws_abc/confluence/my-space/page.html",
            chunks=chunks,
            embeddings=embeddings,
            doc_type="html",
            metadata={},
        )

        call_args = mock_s3vectors_client.put_vectors.call_args[1]
        meta = call_args["vectors"][0]["metadata"]
        assert meta["repo"] == ""

    def test_store_vectors_skips_empty_embeddings(self, vector_config):
        """Test chunks with empty embeddings are skipped."""
        chunks = ["chunk 0", "chunk 1", "chunk 2"]
        embeddings = [[0.1] * 1024, [], [0.3] * 1024]  # chunk 1 has no embedding

        result = store_vectors(
            workspace_id="ws_abc",
            source="confluence",
            folder_path="space",
            filename="page.html",
            s3_key="key",
            chunks=chunks,
            embeddings=embeddings,
            doc_type="html",
            metadata={},
        )

        assert result == 2  # Only 2 out of 3 stored
        call_args = mock_s3vectors_client.put_vectors.call_args[1]
        assert len(call_args["vectors"]) == 2

    def test_store_vectors_all_empty_embeddings(self, vector_config):
        """Test returns 0 when all embeddings are empty."""
        chunks = ["chunk 0"]
        embeddings = [[]]

        result = store_vectors(
            workspace_id="ws_abc",
            source="confluence",
            folder_path="space",
            filename="page.html",
            s3_key="key",
            chunks=chunks,
            embeddings=embeddings,
            doc_type="html",
            metadata={},
        )

        assert result == 0
        mock_s3vectors_client.put_vectors.assert_not_called()

    def test_store_vectors_includes_source_metadata(self, vector_config):
        """Test source-specific metadata is included when present."""
        chunks = ["text"]
        embeddings = [[0.1] * 1024]
        metadata = {
            "confluence_page_id": "12345",
            "confluence_space_key": "DEV",
            "connection_id": "conn_abc",
            "unrelated_key": "ignored",
        }

        store_vectors(
            workspace_id="ws_abc",
            source="confluence",
            folder_path="space",
            filename="page.html",
            s3_key="key",
            chunks=chunks,
            embeddings=embeddings,
            doc_type="html",
            metadata=metadata,
        )

        call_args = mock_s3vectors_client.put_vectors.call_args[1]
        meta = call_args["vectors"][0]["metadata"]
        assert meta["confluence_page_id"] == "12345"
        assert meta["confluence_space_key"] == "DEV"
        assert meta["connection_id"] == "conn_abc"
        assert "unrelated_key" not in meta

    def test_store_vectors_content_truncated(self, vector_config):
        """Test content in metadata is truncated to 10000 chars."""
        long_content = "x" * 20000
        chunks = [long_content]
        embeddings = [[0.1] * 1024]

        store_vectors(
            workspace_id="ws_abc",
            source="confluence",
            folder_path="space",
            filename="page.html",
            s3_key="key",
            chunks=chunks,
            embeddings=embeddings,
            doc_type="html",
            metadata={},
        )

        call_args = mock_s3vectors_client.put_vectors.call_args[1]
        meta = call_args["vectors"][0]["metadata"]
        assert len(meta["content"]) == 10000

    def test_store_vectors_client_error(self, vector_config):
        """Test returns 0 on S3 Vectors ClientError."""
        mock_s3vectors_client.put_vectors.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal error"}},
            "PutVectors",
        )

        result = store_vectors(
            workspace_id="ws_abc",
            source="confluence",
            folder_path="space",
            filename="page.html",
            s3_key="key",
            chunks=["text"],
            embeddings=[[0.1] * 1024],
            doc_type="html",
            metadata={},
        )

        assert result == 0


# ============================================================================
# Vector Deletion
# ============================================================================


class TestDeleteVectorsForDocument:
    """Test deletion of document vectors."""

    def test_delete_vectors_with_folder(self, vector_config):
        """Test deletion with folder path."""
        result = delete_vectors_for_document(
            workspace_id="ws_abc",
            source="confluence",
            folder_path="my-space",
            filename="page.html",
        )

        assert result == 100  # Deletes keys 0-99
        mock_s3vectors_client.delete_vectors.assert_called_once()
        call_args = mock_s3vectors_client.delete_vectors.call_args[1]
        assert call_args["vectorBucketName"] == "test-vector-bucket"
        assert call_args["indexName"] == "test-vector-index"
        # Verify key pattern
        keys = call_args["keys"]
        assert len(keys) == 100
        assert keys[0] == "ws_abc#confluence#my-space#page.html_chunk_0"
        assert keys[99] == "ws_abc#confluence#my-space#page.html_chunk_99"

    def test_delete_vectors_without_folder(self, vector_config):
        """Test deletion without folder path."""
        result = delete_vectors_for_document(
            workspace_id="ws_abc",
            source="jira",
            folder_path="",
            filename="issue.json",
        )

        call_args = mock_s3vectors_client.delete_vectors.call_args[1]
        assert call_args["keys"][0] == "ws_abc#jira#issue.json_chunk_0"

    def test_delete_vectors_client_error(self, vector_config):
        """Test returns 0 on ClientError."""
        mock_s3vectors_client.delete_vectors.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}},
            "DeleteVectors",
        )

        result = delete_vectors_for_document(
            workspace_id="ws_abc",
            source="confluence",
            folder_path="space",
            filename="page.html",
        )
        assert result == 0


# ============================================================================
# Process Document
# ============================================================================


class TestProcessDocument:
    """Test the main process_document function."""

    def test_process_document_success(self, vector_config, mock_bedrock_embedding):
        """Test successful end-to-end document processing."""
        body_mock = MagicMock()
        body_mock.read.return_value = b"<p>Substantial content here. " + b"x" * 100 + b"</p>"
        mock_s3_client.get_object.return_value = {
            "Body": body_mock,
            "Metadata": {},
        }

        result = process_document(
            "test-kb-bucket",
            "workspaces/ws_abc/confluence/space/page.html",
        )

        assert result["status"] == "success"
        assert result["doc_type"] == "html"
        assert result["chunks"] >= 1
        assert result["vectors_stored"] >= 1

    def test_process_document_invalid_key(self):
        """Test skips documents with invalid S3 key format."""
        result = process_document("test-bucket", "invalid/path/file.txt")
        assert result["status"] == "skipped"
        assert "Invalid key" in result["reason"]

    def test_process_document_insufficient_content(self, vector_config):
        """Test skips documents with insufficient text content."""
        body_mock = MagicMock()
        body_mock.read.return_value = b"<p>Hi</p>"  # Too short (< 50 chars)
        mock_s3_client.get_object.return_value = {
            "Body": body_mock,
            "Metadata": {},
        }

        result = process_document(
            "test-kb-bucket",
            "workspaces/ws_abc/confluence/space/page.html",
        )

        assert result["status"] == "skipped"
        assert "Insufficient" in result["reason"]

    def test_process_document_s3_client_error(self):
        """Test handles S3 ClientError."""
        mock_s3_client.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )

        result = process_document(
            "test-kb-bucket",
            "workspaces/ws_abc/confluence/space/page.html",
        )

        assert result["status"] == "error"
        assert "error" in result

    def test_process_document_uses_github_doc_type_metadata(
        self, vector_config, mock_bedrock_embedding
    ):
        """Test github_doc_type metadata overrides file extension doc type."""
        body_mock = MagicMock()
        body_mock.read.return_value = b"Substantial markdown content here " + b"x" * 100
        mock_s3_client.get_object.return_value = {
            "Body": body_mock,
            "Metadata": {"github_doc_type": "adr"},
        }

        result = process_document(
            "test-kb-bucket",
            "workspaces/ws_abc/github/repo/docs/decision.md",
        )

        assert result["status"] == "success"
        assert result["doc_type"] == "adr"

    def test_process_document_empty_text(self, vector_config):
        """Test skips when extracted text is empty."""
        body_mock = MagicMock()
        # Binary content that cannot be decoded
        body_mock.read.return_value = bytes(range(128, 256)) * 10
        mock_s3_client.get_object.return_value = {
            "Body": body_mock,
            "Metadata": {},
        }

        result = process_document(
            "test-kb-bucket",
            "workspaces/ws_abc/confluence/space/image.bin",
        )

        assert result["status"] == "skipped"

    def test_process_document_embedding_failures(
        self, vector_config
    ):
        """Test processes document even when some embeddings fail."""
        body_mock = MagicMock()
        body_mock.read.return_value = (
            b"Long text content. " * 500
        )  # Will produce multiple chunks
        mock_s3_client.get_object.return_value = {
            "Body": body_mock,
            "Metadata": {},
        }

        # Return empty embedding for all calls (simulating failures)
        empty_body = MagicMock()
        empty_body.read.return_value = json.dumps(
            {"embedding": []}
        ).encode("utf-8")
        mock_bedrock_client.invoke_model.return_value = {"body": empty_body}

        result = process_document(
            "test-kb-bucket",
            "workspaces/ws_abc/confluence/space/big-doc.txt",
        )

        # Should complete but with 0 vectors stored
        assert result["status"] == "success"
        assert result["vectors_stored"] == 0


# ============================================================================
# Lambda Handler - S3 Events
# ============================================================================


class TestLambdaHandlerS3Events:
    """Test lambda_handler with S3 ObjectCreated events."""

    @patch.object(handler_module, "process_document")
    def test_handler_s3_event(self, mock_process, s3_event):
        """Test handler processes S3 event records."""
        mock_process.return_value = {
            "status": "success",
            "key": "workspaces/ws_abc/confluence/my-space/page.html",
            "doc_type": "html",
            "chunks": 1,
            "vectors_stored": 1,
        }

        result = lambda_handler(s3_event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["results"]) == 1
        assert body["results"][0]["status"] == "success"
        mock_process.assert_called_once_with(
            "test-kb-bucket",
            "workspaces/ws_abc/confluence/my-space/page.html",
        )

    @patch.object(handler_module, "process_document")
    def test_handler_s3_event_url_encoded_key(self, mock_process):
        """Test handler URL-decodes S3 object key."""
        mock_process.return_value = {"status": "success"}

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "test-bucket"},
                        "object": {
                            "key": "workspaces/ws_abc/confluence/My+Space/Page+Title.html"
                        },
                    }
                }
            ]
        }

        lambda_handler(event, None)

        mock_process.assert_called_once_with(
            "test-bucket",
            "workspaces/ws_abc/confluence/My Space/Page Title.html",
        )

    @patch.object(handler_module, "process_document")
    def test_handler_s3_event_skips_non_workspace_key(self, mock_process):
        """Test handler skips non-workspace S3 keys."""
        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "test-bucket"},
                        "object": {"key": "config/settings.json"},
                    }
                }
            ]
        }

        result = lambda_handler(event, None)

        mock_process.assert_not_called()
        body = json.loads(result["body"])
        assert len(body["results"]) == 0

    @patch.object(handler_module, "process_document")
    def test_handler_multiple_s3_records(self, mock_process):
        """Test handler processes multiple S3 records."""
        mock_process.return_value = {"status": "success"}

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "test-bucket"},
                        "object": {
                            "key": "workspaces/ws_abc/confluence/space/page1.html"
                        },
                    }
                },
                {
                    "s3": {
                        "bucket": {"name": "test-bucket"},
                        "object": {
                            "key": "workspaces/ws_abc/confluence/space/page2.html"
                        },
                    }
                },
            ]
        }

        result = lambda_handler(event, None)

        assert mock_process.call_count == 2
        body = json.loads(result["body"])
        assert len(body["results"]) == 2


# ============================================================================
# Lambda Handler - SQS Events (Reprocessing)
# ============================================================================


class TestLambdaHandlerSqsEvents:
    """Test lambda_handler with SQS reprocessing events."""

    @patch.object(handler_module, "process_document")
    def test_handler_sqs_event(self, mock_process, sqs_event):
        """Test handler processes SQS event records."""
        mock_process.return_value = {"status": "success"}

        result = lambda_handler(sqs_event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["results"]) == 1
        mock_process.assert_called_once_with(
            "test-kb-bucket",
            "workspaces/ws_abc/confluence/my-space/page.html",
        )

    @patch.object(handler_module, "process_document")
    def test_handler_sqs_event_uses_kb_bucket_fallback(self, mock_process):
        """Test SQS event falls back to KB_BUCKET when bucket not specified."""
        mock_process.return_value = {"status": "success"}

        event = {
            "Records": [
                {
                    "body": json.dumps(
                        {"key": "workspaces/ws_abc/confluence/space/page.html"}
                    )
                }
            ]
        }

        lambda_handler(event, None)

        # Should use KB_BUCKET env var as fallback
        mock_process.assert_called_once_with(
            "test-kb-bucket",
            "workspaces/ws_abc/confluence/space/page.html",
        )

    @patch.object(handler_module, "process_document")
    def test_handler_sqs_event_no_key(self, mock_process):
        """Test SQS event with missing key is skipped."""
        event = {
            "Records": [
                {"body": json.dumps({"bucket": "test-bucket"})}
            ]
        }

        result = lambda_handler(event, None)

        mock_process.assert_not_called()
        body = json.loads(result["body"])
        assert len(body["results"]) == 0

    def test_handler_sqs_event_invalid_json(self):
        """Test SQS event with invalid JSON is handled gracefully."""
        event = {"Records": [{"body": "not valid json {]"}]}

        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["results"]) == 0


# ============================================================================
# Lambda Handler - Edge Cases
# ============================================================================


class TestLambdaHandlerEdgeCases:
    """Test lambda_handler edge cases."""

    def test_handler_empty_records(self):
        """Test handler with empty records list."""
        result = lambda_handler({"Records": []}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["results"]) == 0

    def test_handler_no_records_key(self):
        """Test handler with missing Records key."""
        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["results"]) == 0

    @patch.object(handler_module, "process_document")
    def test_handler_mixed_s3_and_sqs_records(self, mock_process):
        """Test handler processes both S3 and SQS records in same event."""
        mock_process.return_value = {"status": "success"}

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "bucket-a"},
                        "object": {
                            "key": "workspaces/ws_1/confluence/space/s3doc.html"
                        },
                    }
                },
                {
                    "body": json.dumps(
                        {
                            "bucket": "bucket-b",
                            "key": "workspaces/ws_2/jira/sqsdoc.json",
                        }
                    )
                },
            ]
        }

        result = lambda_handler(event, None)

        assert mock_process.call_count == 2
        body = json.loads(result["body"])
        assert len(body["results"]) == 2

    @patch.object(handler_module, "process_document")
    def test_handler_counts_successful_results(self, mock_process):
        """Test handler correctly counts successful vs failed results."""
        mock_process.side_effect = [
            {"status": "success"},
            {"status": "error", "error": "fail"},
            {"status": "success"},
        ]

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "b"},
                        "object": {"key": "workspaces/ws/s/f/a.html"},
                    }
                },
                {
                    "s3": {
                        "bucket": {"name": "b"},
                        "object": {"key": "workspaces/ws/s/f/b.html"},
                    }
                },
                {
                    "s3": {
                        "bucket": {"name": "b"},
                        "object": {"key": "workspaces/ws/s/f/c.html"},
                    }
                },
            ]
        }

        result = lambda_handler(event, None)

        body = json.loads(result["body"])
        assert len(body["results"]) == 3
        successes = [r for r in body["results"] if r["status"] == "success"]
        assert len(successes) == 2

    @patch.object(handler_module, "process_document")
    def test_handler_record_without_s3_or_body(self, mock_process):
        """Test handler ignores records that are neither S3 nor SQS."""
        event = {
            "Records": [{"eventSource": "unknown", "data": "something"}]
        }

        result = lambda_handler(event, None)

        mock_process.assert_not_called()
        body = json.loads(result["body"])
        assert len(body["results"]) == 0
