"""Salesforce Sync Lambda -- stub for chunk 01 terraform scaffolding.

The real handler ships in chunk 03: SOQL walker for
Account/Contact/Opportunity + recent activities (Task/Event) + ContentVersion
files, with SystemModstamp cursor + rate-limit retry.
"""


def lambda_handler(event, context):
    return {"batchItemFailures": []}
