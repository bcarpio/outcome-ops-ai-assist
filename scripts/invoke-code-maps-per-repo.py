#!/usr/bin/env python3

"""
Invoke generate-code-maps Lambda once per repo from SSM allowlist.

This script reads the repos allowlist from SSM Parameter Store,
filters out "standards" repos, and invokes the Lambda once for each
application/internal repo.

Usage:
  ENV=dev python3 scripts/invoke-code-maps-per-repo.py
  ENV=prd python3 scripts/invoke-code-maps-per-repo.py
"""

import json
import os
import subprocess
import sys
import time


def get_ssm_parameter(parameter_name: str, env: str) -> dict:
    """Fetch parameter from SSM Parameter Store"""
    cmd = [
        "aws", "ssm", "get-parameter",
        "--name", parameter_name,
        "--output", "json"
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )
        response = json.loads(result.stdout)
        return json.loads(response["Parameter"]["Value"])
    except subprocess.CalledProcessError as e:
        print(f"Error fetching SSM parameter {parameter_name}: {e.stderr}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error parsing SSM parameter value: {e}")
        sys.exit(1)


def get_application_repos(allowlist):
    """Filter out standards repos, return only application/internal repos"""
    all_repos = allowlist.get('repos', [])
    application_repos = [repo for repo in all_repos if repo.get('type') != 'standards']

    print(f"Total repos: {len(all_repos)}")
    print(f"Application/internal repos: {len(application_repos)}")
    print(f"Standards repos (excluded): {len(all_repos) - len(application_repos)}")

    return application_repos


def invoke_lambda_for_repo(repo_name: str, function_name: str):
    """Invoke generate-code-maps Lambda for a specific repo"""
    payload = json.dumps({"repos": [repo_name]})

    print(f"\n{'=' * 80}")
    print(f"Invoking Lambda for repo: {repo_name}")
    print(f"Function: {function_name}")
    print(f"Payload: {payload}")
    print(f"{'=' * 80}\n")

    cmd = [
        "aws", "lambda", "invoke",
        "--function-name", function_name,
        "--payload", payload,
        "--cli-binary-format", "raw-in-base64-out",
        "--cli-read-timeout", "900",  # 15 minutes to match Lambda timeout
        f"/tmp/lambda-response-{repo_name}.json"
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900  # 15 minute timeout per repo
        )

        if result.returncode != 0:
            print(f"✗ Lambda invocation failed for {repo_name}")
            print(f"Error: {result.stderr}")
            return False

        # Check response for errors
        with open(f"/tmp/lambda-response-{repo_name}.json", 'r') as f:
            response = json.load(f)
            if 'errorMessage' in response:
                print(f"✗ Lambda execution failed for {repo_name}")
                print(f"Error: {response['errorMessage']}")
                return False

            # Parse success response
            body = json.loads(response.get('body', '{}'))
            if body.get('success'):
                files_analyzed = body.get('total_files_analyzed', 0)
                print(f"✓ Successfully processed {repo_name} ({files_analyzed} files analyzed)")
                return True
            else:
                print(f"✗ Unexpected response for {repo_name}: {response}")
                return False

    except subprocess.TimeoutExpired:
        print(f"✗ Lambda invocation timed out for {repo_name} (15 minutes)")
        return False
    except Exception as e:
        print(f"✗ Error invoking Lambda for {repo_name}: {e}")
        return False


def main():
    env = os.environ.get('ENV', 'dev')
    app_name = 'outcome-ops-ai-assist'

    print(f"Environment: {env}")
    print(f"Application: {app_name}\n")

    # Load allowlist from SSM
    parameter_name = f"/{env}/{app_name}/config/repos-allowlist"
    print(f"Loading repos from SSM: {parameter_name}")

    allowlist = get_ssm_parameter(parameter_name, env)
    repos = get_application_repos(allowlist)

    if not repos:
        print("No application/internal repos found in allowlist")
        sys.exit(1)

    print(f"\nProcessing {len(repos)} repos:")
    for repo in repos:
        print(f"  - {repo['name']}")

    # Confirm before proceeding
    response = input(f"\nInvoke Lambda for {len(repos)} repos? This may take a while. [y/N]: ")
    if response.lower() != 'y':
        print("Aborted")
        sys.exit(0)

    # Function name
    function_name = f"{env}-{app_name}-generate-code-maps"
    print(f"\nUsing Lambda function: {function_name}\n")

    # Process each repo
    start_time = time.time()
    successes = 0
    failures = 0

    for i, repo in enumerate(repos, 1):
        repo_name = repo['name']
        print(f"\n[{i}/{len(repos)}] Processing {repo_name}...")

        success = invoke_lambda_for_repo(repo_name, function_name)
        if success:
            successes += 1
        else:
            failures += 1

        # Brief pause between invocations to avoid throttling
        if i < len(repos):
            time.sleep(2)

    elapsed_time = time.time() - start_time
    minutes = int(elapsed_time // 60)
    seconds = int(elapsed_time % 60)

    print(f"\n{'=' * 80}")
    print(f"Summary:")
    print(f"  Total repos: {len(repos)}")
    print(f"  Successes: {successes}")
    print(f"  Failures: {failures}")
    print(f"  Elapsed time: {minutes}m {seconds}s")
    print(f"{'=' * 80}")

    if failures > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
