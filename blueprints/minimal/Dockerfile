# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# requirements.txt pins the SDK, which provides socialagent_runtime. The
# deploy puts an unreleased SDK wheel in wheels/ when it has one; pip takes that
# file over the index for the pinned version. The wildcard lets a build with
# no wheels/ directory (a local `docker build`) copy just requirements.txt.
COPY requirements.txt wheels* ./
RUN pip install --no-cache-dir -r requirements.txt $(ls *.whl 2>/dev/null)

RUN useradd -m -u 1000 bedrock_agentcore

COPY agent.py .

RUN chown -R bedrock_agentcore:bedrock_agentcore /app

USER bedrock_agentcore
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping', timeout=2)" || exit 1

CMD ["opentelemetry-instrument", "python", "agent.py"]
