FROM node:22-slim

# Install Snyk CLI globally
RUN npm install -g snyk@latest

# Install supergateway to bridge stdio -> HTTP/SSE on a fixed port
RUN npm install -g supergateway

EXPOSE 8080

# Run Snyk MCP in stdio mode, proxied via supergateway on port 8080
# supergateway exposes /mcp/snyk as Streamable HTTP endpoint
CMD ["supergateway", "--port", "8080", "--host", "0.0.0.0", "--stdio", "snyk mcp -t stdio --experimental"]
