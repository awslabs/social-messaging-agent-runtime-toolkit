# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cached boto3 client factory with adaptive retries.

Clients are keyed by service, Region and configuration. Cache survives for the
lifetime of the Lambda container, avoiding the ~100-300ms cost of
creating a new client per request.

Adaptive retry mode is AWS's recommended approach: it combines
exponential backoff with client-side rate limiting based on observed
throttling. See:
https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html
"""
from __future__ import annotations

from functools import lru_cache

import boto3
from botocore.client import BaseClient
from botocore.config import Config

_DEFAULT_CONFIG = Config(
    retries={"max_attempts": 5, "mode": "adaptive"},
    connect_timeout=3,
    read_timeout=10,
)


@lru_cache(maxsize=None)
def client(service: str, region: str, config: Config = _DEFAULT_CONFIG) -> BaseClient:
    """Return a cached boto3 client for the service, Region and configuration."""
    return boto3.client(service, region_name=region, config=config)


def clear_client_cache() -> None:
    """Clear the client cache. Used in tests."""
    client.cache_clear()
