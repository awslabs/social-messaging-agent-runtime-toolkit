# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Templates page lists what EUM Social holds, so two reads with no sync in
between return the same rows: nothing in a row may come from the request's clock.
"""

import json
from unittest.mock import patch

# shared.registry reads required env vars at import time, and conftest's aws_env
# fixture only runs once collection is done — so import inside each test.

_REGISTRY = {
    "project": "my-bot",
    "environment": "dev",
    "region": "us-east-1",
    "phone_number_id": "phone-number-id-01",
    "template_prefix": "dev_my_bot_",
}


def test_two_reads_of_the_template_list_return_the_same_rows():
    from routes import templates

    with (
        patch.object(templates, "_get_registry", return_value=_REGISTRY),
        patch.object(templates, "boto3") as b3,
    ):
        eum = b3.client.return_value
        eum.get_linked_whatsapp_business_account_phone_number.return_value = {
            "linkedWhatsAppBusinessAccountId": "waba-1"
        }
        eum.list_whatsapp_message_templates.return_value = {
            "templates": [
                {
                    "templateName": "dev_my_bot_hello",
                    "metaTemplateId": "meta-1",
                    "templateStatus": "APPROVED",
                    "templateCategory": "UTILITY",
                }
            ]
        }
        eum.get_whatsapp_message_template.return_value = {
            "template": json.dumps({"category": "UTILITY"})
        }
        first = json.loads(templates.handle_get_templates({"id": "my-bot-dev"}, {})["body"])
        second = json.loads(templates.handle_get_templates({"id": "my-bot-dev"}, {})["body"])

    assert [t["name"] for t in first] == ["dev_my_bot_hello"]
    assert first == second
