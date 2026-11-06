# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# (uksb-pool7fymvx) Social Messaging Agent Runtime Toolkit

terraform {
  # 1.7 for the removed block in modules/security-logging.
  required_version = ">= 1.7.0"

  # Remote state — run bootstrap/ first, then:
  #   socialagent bootstrap && socialagent deploy   (the CLI passes the backend config)
  backend "s3" {}

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # The lock file is not committed, so a fresh clone resolves the newest
      # provider the constraint allows. 6.69 fails PublishLayerVersion on this
      # layer ("bufio.Scanner: token too long"); checkouts that deploy today are
      # on 6.37 through 6.66 and all work. Raise the ceiling on purpose, after a
      # deploy on the newer version.
      version = ">= 6.37, < 6.72"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.2.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5.0"
    }
  }
}
