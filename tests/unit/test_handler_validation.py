"""
Unit tests for input validation errors in the Lambda handler.
"""

import json
import pytest
from pydantic import ValidationError

from src.handler import handler
from src.models import QueryParameters


class TestInputValidationErrors:
    """
    Test cases for input validation errors.
    """

    def test_limit_below_minimum(self):
        """
        Test that limit=0 returns 400 error.
        """
        event = {
            "queryStringParameters": {
                "limit": "0"
            }
        }

        response = handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body
        assert "limit" in body["error"].lower()
        assert "greater than or equal to 1" in body["error"].lower()

    def test_limit_above_maximum(self):
        """
        Test that limit=101 returns 400 error.
        """
        event = {
            "queryStringParameters": {
                "limit": "101"
            }
        }

        response = handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body
        assert "limit" in body["error"].lower()
        assert "less than or equal to 100" in body["error"].lower()

    def test_limit_invalid_type(self):
        """
        Test that non-integer limit returns 400 error.
        """
        event = {
            "queryStringParameters": {
                "limit": "invalid"
            }
        }

        response = handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body
        assert "limit" in body["error"].lower()

    def test_limit_negative_value(self):
        """
        Test that negative limit returns 400 error.
        """
        event = {
            "queryStringParameters": {
                "limit": "-10"
            }
        }

        response = handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body
        assert "limit" in body["error"].lower()

    def test_limit_float_value(self):
        """
        Test that float limit returns 400 error.
        """
        event = {
            "queryStringParameters": {
                "limit": "10.5"
            }
        }

        response = handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body
        assert "limit" in body["error"].lower()


class TestPydanticModelValidation:
    """
    Direct tests of Pydantic model validation.
    """

    def test_query_parameters_limit_below_minimum(self):
        """
        Test that QueryParameters rejects limit < 1.
        """
        with pytest.raises(ValidationError) as exc_info:
            QueryParameters(limit=0)

        errors = exc_info.value.errors()
        assert any(e["loc"][0] == "limit" for e in errors)

    def test_query_parameters_limit_above_maximum(self):
        """
        Test that QueryParameters rejects limit > 100.
        """
        with pytest.raises(ValidationError) as exc_info:
            QueryParameters(limit=101)

        errors = exc_info.value.errors()
        assert any(e["loc"][0] == "limit" for e in errors)

    def test_query_parameters_limit_invalid_type(self):
        """
        Test that QueryParameters rejects non-integer limit.
        """
        with pytest.raises(ValidationError) as exc_info:
            QueryParameters(limit="invalid")  # type: ignore

        errors = exc_info.value.errors()
        assert any(e["loc"][0] == "limit" for e in errors)

    def test_query_parameters_limit_negative(self):
        """
        Test that QueryParameters rejects negative limit.
        """
        with pytest.raises(ValidationError) as exc_info:
            QueryParameters(limit=-10)

        errors = exc_info.value.errors()
        assert any(e["loc"][0] == "limit" for e in errors)

    def test_query_parameters_valid_limits(self):
        """
        Test that QueryParameters accepts valid limit values.
        """
        # Test boundary values
        params1 = QueryParameters(limit=1)
        assert params1.limit == 1

        params100 = QueryParameters(limit=100)
        assert params100.limit == 100

        # Test middle value
        params50 = QueryParameters(limit=50)
        assert params50.limit == 50

    def test_query_parameters_default_limit(self):
        """
        Test that QueryParameters uses default limit when not provided.
        """
        params = QueryParameters()
        assert params.limit == 10
