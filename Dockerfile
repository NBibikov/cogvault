# Runs the stdio MCP server in a container (used by MCP directories to start the
# server and read its tools). Mount your memory at /memory to keep it.
FROM python:3.12-slim
RUN pip install --no-cache-dir cogvault
ENV COGVAULT_LOG=off
VOLUME ["/memory"]
ENTRYPOINT ["cogvault", "mcp", "--tenant", "/memory"]
