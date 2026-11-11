# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py .
COPY socialagent_runtime ./socialagent_runtime
# Like the blueprints: a non-root user and a health check, so scanners hold
# the fixtures to the same rules as the images they stand in for.
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping')" || exit 1
