# One image for the API and the worker. The agent runs inside the worker's container,
# so the image also carries what the agent needs: the Claude Agent SDK and pandas.
FROM python:3.12-slim

# The agent runs as a regular user, never as root.
RUN useradd --create-home agent \
    && mkdir -p /work /artifacts \
    && chown agent:agent /work /artifacts

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api.py worker.py agent.py ./

USER agent
