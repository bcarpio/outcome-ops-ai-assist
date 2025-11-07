"""
Hello World Lambda handler - intentionally missing ADR compliance for testing.
"""
import json

def handler(event, context):
    # Missing: Pydantic validation, error handling, type hints, logging
    body = json.loads(event['body'])
    name = body['name']

    result = {
        'message': f'Hello {name}'
    }

    return {
        'statusCode': 200,
        'body': json.dumps(result)
    }
