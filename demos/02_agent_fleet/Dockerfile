# One image for every service in the fleet: the API, the dashboard, the controller, and
# the workers the controller starts. The agent runs inside each worker's container, so the
# image also carries what the agent needs: the Claude Agent SDK and pandas.
FROM python:3.12-slim

# The controller drives Docker with the ordinary docker command.
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker

# The agent runs as a regular user, never as root.
RUN useradd --create-home agent \
    && mkdir -p /work /artifacts \
    && chown agent:agent /work /artifacts

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Workers are started by the controller, not by Compose, so their input data is in the image.
COPY data /data
COPY api.py worker.py agent.py controller.py dashboard.py dashboard.html ./

USER agent
