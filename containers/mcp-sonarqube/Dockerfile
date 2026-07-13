# OutcomeOps-owned minimal SonarQube Cloud MCP server.
# Replaces the third-party mcp/sonarqube image (fatal I/O-reactor bug under load).
# Self-contained build context (containers/mcp-sonarqube/), matching mcp-snyk.
FROM python:3.12-slim

WORKDIR /app

# Only boto3 (optional SSM token fallback); everything else is stdlib.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sonarcloud.py .
COPY server.py .

# Trust a BYO internal CA bundle if the platform layer provides one (parity with
# the other MCP images); harmless if absent.
ENV REQUESTS_CA_BUNDLE=/opt/ca-bundle.pem
ENV SSL_CERT_FILE=/opt/ca-bundle.pem

EXPOSE 8080
ENTRYPOINT ["python", "server.py"]
