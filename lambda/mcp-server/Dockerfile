FROM python:3.12-slim

WORKDIR /app

# Build context is `lambda/` (not `lambda/mcp-server/`) so we can also
# copy lambda/shared/ for audit-writer access. See Makefile target
# `build-mcp-server-image` and plan/audit-completeness.
COPY mcp-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp-server/handler.py .
COPY shared/ ./shared/

EXPOSE 8080

CMD ["python", "handler.py"]
