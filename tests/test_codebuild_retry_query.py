# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The CodeBuild build scripts' never-ran query, against the phase shapes it meets (#211).

On a stack's first deploy CodeBuild can try a build before IAM applies its new
service role's policy, and fails it in ``QUEUED`` having run nothing; the build
provisioners start such a build again. The query that tells such a build from
one that ran is evaluated here with JMESPath, as the AWS CLI evaluates it. It
lives apart from ``test_terraform_hardening.py``, whose imports
``tests/test_ci_terraform_job.py`` limits to ``pytest`` and ``yaml``. The real API reports ``phaseStatus: CLIENT_ERROR``
with an ``ACCESS_DENIED`` context; its reference documents ``CLIENT_ERROR`` as
a context ``statusCode`` instead, so the query reads either.
"""

from __future__ import annotations

import re
from pathlib import Path

import jmespath
import pytest

INFRA = Path(__file__).resolve().parents[1] / "infra-terraform"

# Each build script's file, and how many build provisioners it holds.
_SCRIPTS = {
    "packaging.tf": 1,
    "modules/runtime/codebuild.tf": 2,
    "modules/templates/main.tf": 1,
}
_QUERY = re.compile(r'--query "(length\(builds\[0\]\.phases[^"]*)"')

_PHASES = {
    "phaseStatus CLIENT_ERROR (what the API returns today)": (
        [
            {"phaseType": "SUBMITTED", "phaseStatus": "SUCCEEDED"},
            {
                "phaseType": "QUEUED",
                "phaseStatus": "CLIENT_ERROR",
                "contexts": [
                    {"message": "not authorized to perform: logs:CreateLogStream"}
                ],
            },
            {"phaseType": "COMPLETED"},
        ],
        1,
    ),
    "phaseStatus FAILED with a CLIENT_ERROR context (the documented shape)": (
        [
            {
                "phaseType": "QUEUED",
                "phaseStatus": "FAILED",
                "contexts": [{"statusCode": "CLIENT_ERROR", "message": "..."}],
            },
            {"phaseType": "COMPLETED"},
        ],
        1,
    ),
    "queued fine, failed in BUILD": (
        [
            {"phaseType": "QUEUED", "phaseStatus": "SUCCEEDED"},
            {
                "phaseType": "BUILD",
                "phaseStatus": "FAILED",
                "contexts": [{"statusCode": "COMMAND_EXECUTION_ERROR"}],
            },
        ],
        0,
    ),
    "CLIENT_ERROR in a later phase": (
        [
            {"phaseType": "QUEUED", "phaseStatus": "SUCCEEDED"},
            {"phaseType": "DOWNLOAD_SOURCE", "phaseStatus": "CLIENT_ERROR"},
        ],
        0,
    ),
    "no QUEUED phase": ([{"phaseType": "SUBMITTED", "phaseStatus": "SUCCEEDED"}], 0),
    "no phases": ([], 0),
}


@pytest.mark.parametrize("path", sorted(_SCRIPTS))
@pytest.mark.parametrize("shape", sorted(_PHASES))
def test_the_never_ran_query_reads_either_field(path: str, shape: str):
    """The query counts a QUEUED phase whose ``phaseStatus`` is CLIENT_ERROR, as
    the API returns it, or that has a CLIENT_ERROR context, as its reference
    documents it, and nothing in a later phase."""
    queries = _QUERY.findall((INFRA / path).read_text())
    assert len(queries) == _SCRIPTS[path], (
        f"{path} has {len(queries)} never-ran queries"
    )
    phases, expected = _PHASES[shape]
    for query in queries:
        assert jmespath.search(query, {"builds": [{"phases": phases}]}) == expected
