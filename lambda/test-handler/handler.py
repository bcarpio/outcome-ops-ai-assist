"""
Test handler to verify automatic PR analysis workflow.
This is intentionally minimal to test the workflow trigger.
"""

def handler(event, context):
    return {
        'statusCode': 200,
        'body': 'Test handler'
    }
