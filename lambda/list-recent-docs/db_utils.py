"""
DynamoDB utility functions for listing recent documents.

Provides functions to load table name from SSM, scan DynamoDB for documents
with embeddings, and sort results by timestamp.
"""

import boto3
from boto3.dynamodb.conditions import Attr
from typing import List, Dict, Any
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def get_table_name_from_ssm(parameter_name: str) -> str:
    """
    Load DynamoDB table name from SSM Parameter Store.
    
    Args:
        parameter_name: SSM parameter name (e.g., '/app/dynamodb/table-name')
        
    Returns:
        Table name string
        
    Raises:
        Exception: If SSM parameter cannot be retrieved
    """
    try:
        ssm = boto3.client('ssm')
        response = ssm.get_parameter(Name=parameter_name)
        table_name = response['Parameter']['Value']
        logger.info(f"Loaded table name from SSM: {table_name}")
        return table_name
    except Exception as e:
        logger.error(f"Failed to load table name from SSM parameter {parameter_name}: {str(e)}")
        raise


def full_table_scan(
    table,
    filter_expression=None,
    projection_expression=None,
    expression_attribute_names=None,
) -> List[Dict[Any, Any]]:
    """
    Perform a full table scan with pagination.
    
    Args:
        table: DynamoDB table resource
        filter_expression: Optional filter expression
        projection_expression: Optional projection expression
        expression_attribute_names: Optional attribute name mappings
        
    Returns:
        List of all items matching the filter
    """
    items = []
    scan_kwargs = {}
    
    if filter_expression is not None:
        scan_kwargs['FilterExpression'] = filter_expression
    if projection_expression is not None:
        scan_kwargs['ProjectionExpression'] = projection_expression
    if expression_attribute_names is not None:
        scan_kwargs['ExpressionAttributeNames'] = expression_attribute_names
    
    try:
        while True:
            response = table.scan(**scan_kwargs)
            items.extend(response.get('Items', []))
            
            # Check for more pages
            if 'LastEvaluatedKey' not in response:
                break
                
            scan_kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
            
        logger.info(f"Scanned table and found {len(items)} items")
        return items
    except Exception as e:
        logger.error(f"Error scanning table: {str(e)}")
        raise


def scan_documents_with_embeddings(table_name: str) -> List[Dict[Any, Any]]:
    """
    Scan DynamoDB for documents that have embeddings generated.
    
    Args:
        table_name: DynamoDB table name
        
    Returns:
        List of document items with embeddings
        
    Raises:
        Exception: If scan fails
    """
    try:
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)
        
        # Scan for items where the embeddings attribute exists
        # This indicates the document has been processed and has embeddings
        filter_expression = Attr('embeddings').exists()
        
        # Project only the fields we need
        projection_expression = "PK, SK, document_id, file_name, uploaded_at, #status, user_id"
        
        # Use ExpressionAttributeNames for 'status' (reserved word in DynamoDB)
        expression_attribute_names = {
            '#status': 'status'
        }
        
        items = full_table_scan(
            table,
            filter_expression=filter_expression,
            projection_expression=projection_expression,
            expression_attribute_names=expression_attribute_names,
        )
        
        logger.info(f"Found {len(items)} documents with embeddings")
        return items
    except Exception as e:
        logger.error(f"Error scanning for documents with embeddings: {str(e)}")
        raise


def sort_documents_by_timestamp(documents: List[Dict[Any, Any]], limit: int = None) -> List[Dict[Any, Any]]:
    """
    Sort documents by uploaded_at timestamp in descending order (most recent first).
    
    Args:
        documents: List of document items
        limit: Optional limit on number of results to return
        
    Returns:
        Sorted list of documents (most recent first)
    """
    try:
        # Sort by uploaded_at in descending order (most recent first)
        # ISO 8601 format timestamps are lexicographically sortable
        sorted_docs = sorted(
            documents,
            key=lambda x: x.get('uploaded_at', ''),
            reverse=True  # Descending order
        )
        
        # Apply limit if specified
        if limit is not None and limit > 0:
            sorted_docs = sorted_docs[:limit]
            logger.info(f"Limited results to {limit} documents")
        
        logger.info(f"Sorted {len(sorted_docs)} documents by timestamp")
        return sorted_docs
    except Exception as e:
        logger.error(f"Error sorting documents: {str(e)}")
        raise
