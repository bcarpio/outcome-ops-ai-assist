#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit tests for DynamoDB utilities in list_recent_docs handler.

Coverage:
- Successful scans with multiple items
- Empty results
- Error handling
- Filtering logic
- Pagination
"""

import boto3
import pytest
from botocore.exceptions import ClientError
from datetime import datetime, timedelta
from decimal import Decimal
from moto import mock_dynamodb
import os
import sys

# Add lambda directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from handlers.list_recent_docs.db_utils import (
    scan_recent_documents,
    format_document_response,
)


@pytest.fixture
def dynamodb_table():
    """Create a mock DynamoDB table for testing."""
    with mock_dynamodb():
        dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        
        table = dynamodb.create_table(
            TableName='test-table',
            KeySchema=[
                {'AttributeName': 'PK', 'KeyType': 'HASH'},
                {'AttributeName': 'SK', 'KeyType': 'RANGE'}
            ],
            AttributeDefinitions=[
                {'AttributeName': 'PK', 'AttributeType': 'S'},
                {'AttributeName': 'SK', 'AttributeType': 'S'},
                {'AttributeName': 'createdAt', 'AttributeType': 'N'},
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'GSI1',
                    'KeySchema': [
                        {'AttributeName': 'SK', 'KeyType': 'HASH'},
                        {'AttributeName': 'createdAt', 'KeyType': 'RANGE'}
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                    'ProvisionedThroughput': {
                        'ReadCapacityUnits': 5,
                        'WriteCapacityUnits': 5
                    }
                }
            ],
            ProvisionedThroughput={
                'ReadCapacityUnits': 5,
                'WriteCapacityUnits': 5
            }
        )
        
        yield table


def create_test_document(doc_id: str, days_ago: int = 0, status: str = 'active') -> dict:
    """Create a test document item."""
    created_at = int((datetime.utcnow() - timedelta(days=days_ago)).timestamp())
    
    return {
        'PK': f'DOCUMENT# {doc_id}',
        'SK': 'META',
        'documentId': doc_id,
        'title': f'Test Document {doc_id}',
        'createdAt': Decimal(str(created_at)),
        'status': status,
        'userId': 'user123',
        'content': 'Test content'
    }


class TestScanRecentDocuments:
    """Tests for scan_recent_documents function."""
    
    def test_successful_scan_with_multiple_items(self, dynamodb_table):
        """Test successful scan with multiple documents."""
        # Arrange
        documents = [
            create_test_document('doc1', days_ago=1),
            create_test_document('doc2', days_ago=2),
            create_test_document('doc3', days_ago=3),
        ]
        
        for doc in documents:
            dynamodb_table.put_item(Item=doc)
        
        # Act
        result = scan_recent_documents(dynamodb_table, limit=10)
        
        # Assert
        assert len(result) == 3
        assert result[0]['documentId'] == 'doc1'  # Most recent
        assert result[1]['documentId'] == 'doc2'
        assert result[2]['documentId'] == 'doc3'
        
    def test_empty_results(self, dynamodb_table):
        """Test scan with no documents in table."""
        # Act
        result = scan_recent_documents(dynamodb_table, limit=10)
        
        # Assert
        assert result == []
        
    def test_filter_by_status(self, dynamodb_table):
        """Test filtering by document status."""
        # Arrange
        documents = [
            create_test_document('doc1', days_ago=1, status='active'),
            create_test_document('doc2', days_ago=2, status='archived'),
            create_test_document('doc3', days_ago=3, status='active'),
        ]
        
        for doc in documents:
            dynamodb_table.put_item(Item=doc)
        
        # Act
        result = scan_recent_documents(dynamodb_table, limit=10, status='active')
        
        # Assert
        assert len(result) == 2
        assert all(doc['status'] == 'active' for doc in result)
        assert result[0]['documentId'] == 'doc1'
        assert result[1]['documentId'] == 'doc3'
        
    def test_limit_results(self, dynamodb_table):
        """Test limiting number of results."""
        # Arrange
        documents = [
            create_test_document(f'doc{i}', days_ago=i)
            for i in range(1, 6)
        ]
        
        for doc in documents:
            dynamodb_table.put_item(Item=doc)
        
        # Act
        result = scan_recent_documents(dynamodb_table, limit=3)
        
        # Assert
        assert len(result) == 3
        assert result[0]['documentId'] == 'doc1'  # Most recent
        
    def test_sorting_by_created_at(self, dynamodb_table):
        """Test that results are sorted by createdAt descending."""
        # Arrange
        documents = [
            create_test_document('doc1', days_ago=5),
            create_test_document('doc2', days_ago=1),
            create_test_document('doc3', days_ago=3),
        ]
        
        for doc in documents:
            dynamodb_table.put_item(Item=doc)
        
        # Act
        result = scan_recent_documents(dynamodb_table, limit=10)
        
        # Assert
        assert len(result) == 3
        assert result[0]['documentId'] == 'doc2'  # Most recent
        assert result[1]['documentId'] == 'doc3'
        assert result[2]['documentId'] == 'doc1'  # Oldest
        
        # Verify timestamps are in descending order
        for i in range(len(result) - 1):
            assert result[i]['createdAt'] >= result[i + 1]['createdAt']
        
    def test_filter_meta_items_only(self, dynamodb_table):
        """Test that only META items are returned."""
        # Arrange
        items = [
            create_test_document('doc1', days_ago=1),
            {
                'PK': 'DOCUMENT# doc1',
                'SK': 'CONTENT#1',
                'content': 'Some content'
            },
            create_test_document('doc2', days_ago=2),
        ]
        
        for item in items:
            dynamodb_table.put_item(Item=item)
        
        # Act
        result = scan_recent_documents(dynamodb_table, limit=10)
        
        # Assert
        assert len(result) == 2  # Only META items
        assert all(doc['documentId'] in ['doc1', 'doc2'] for doc in result)
        
    def test_error_handling_client_error(self, dynamodb_table, monopatch):
        """Test error handling for ClientError."""
        # Arrange
        def mock_scan(**kwargs):
            raise ClientError(
                {'Error': {'Code': 'ResourceNotFoundException', 'Message': 'Table not found'}},
                'Scan'
            )
        
        monopatch.setattr(dynamodb_table, 'scan', mock_scan)
        
        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            scan_recent_documents(dynamodb_table, limit=10)
        
        assert 'ResourceNotFoundException' in str(exc_info.value)
        
    def test_error_handling_generic_exception(self, dynamodb_table, monopatch):
        """Test error handling for generic exception."""
        # Arrange
        def mock_scan(**kwargs):
            raise Exception('Unexpected error')
        
        monopatch.setattr(dynamodb_table, 'scan', mock_scan)
        
        # Act & Assert
        with pytest.raises(Exception) as exc_info:
            scan_recent_documents(dynamodb_table, limit=10)
        
        assert 'Unexpected error' in str(exc_info.value)


class TestFormatDocumentResponse:
    """Tests for format_document_response function."""
    
    def test_format_complete_document(self):
        """Test formatting a complete document with all fields."""
        # Arrange
        created_at = int(datetime.utcnow().timestamp())
        document = {
            'documentId': 'doc123',
            'title': 'Test Document',
            'createdAt': Decimal(str(created_at)),
            'status': 'active',
            'userId': 'user456',
            'content': 'Test content'
        }
        
        # Act
        result = format_document_response(document)
        
        # Assert
        assert result['documentId'] == 'doc123'
        assert result['title'] == 'Test Document'
        assert result['createdAt'] == created_at
        assert result['content'] == 'Test content'
        assert result['userId'] == 'user456'
        assert result['content'] == 'Test content'
        
    def test_format_minimal_document(self):
        """Test formatting a document with only required fields."""
        # Arrange
        created_at = int(datetime.utcnow().timestamp())
        document = {
            'documentId': 'doc123',
            'title': 'Minimal Doc',
            'createdAt': Decimal(str(created_at))
        }
        
        # Act
        result = format_document_response(document)
        
        # Assert
        assert result['documentId'] == 'doc123'
        assert result['title'] == 'Minimal Doc'
        assert result['createdAt'] == created_at
        assert 'status' not in result or result['status'] is None
        
    def test_format_decimal_conversion(self):
        """Test that Decimal values are converted to int/float."""
        # Arrange
        document = {
            'documentId': 'doc123',
            'title': 'Test',
            'createdAt': Decimal('1700000000'),
            'version': Decimal('1.0')
        }
        
        # Act
        result = format_document_response(document)
        
        # Assert
        assert isinstance(result['createdAt'], int)
        assert result['createdAt'] == 1700000000
        assert isinstance(result['version'], float)
        assert result['version'] == 1.0
        
    def test_format_empty_document(self):
        """Test formatting an empty document."""
        # Arrange
        document = {}
        
        # Act
        result = format_document_response(document)
        
        # Assert
        assert result == {}
        
    def test_format_preserves_all_fields(self):
        """Test that all fields are preserved in formatting."""
        # Arrange
        document = {
            'documentId': 'doc123',
            'title': 'Test',
            'createdAt': Decimal('1700000000'),
            'customField1': 'value1',
            'customField2': 123,
            'nested': {'data': 'test'}
        }
        
        # Act
        result = format_document_response(document)
        
        # Assert
        assert result['documentId'] == 'doc123'
        assert result['customField1'] == 'value1'
        assert result['customField2'] == 123
        assert result['nested'] == {'data': 'test'}
