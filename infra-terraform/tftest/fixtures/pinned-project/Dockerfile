# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY requirements.txt wheels* ./
RUN pip install --no-cache-dir -r requirements.txt $(ls *.whl 2>/dev/null)
COPY agent.py .
