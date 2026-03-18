FROM node:22-slim

# Install Snyk CLI globally
RUN npm install -g snyk@latest

# Install supergateway to bridge stdio -> Streamable HTTP on a fixed port
RUN npm install -g supergateway

EXPOSE 8080

# Run Snyk MCP in stdio mode, proxied via supergateway as Streamable HTTP.
# --outputTransport streamableHttp: speaks JSON-RPC 2.0 over HTTP POST (what the Lambda expects)
# --streamableHttpPath /mcp/snyk: matches the ALB path pattern so requests route correctly
CMD ["supergateway", \
     "--port", "8080", \
     "--host", "0.0.0.0", \
     "--stdio", "snyk mcp -t stdio --experimental", \
     "--outputTransport", "streamableHttp", \
     "--streamableHttpPath", "/mcp/snyk"]
