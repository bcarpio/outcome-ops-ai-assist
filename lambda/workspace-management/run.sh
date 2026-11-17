#!/bin/bash
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# Bootstrap script for Lambda Web Adapter
# Starts uvicorn to serve the FastAPI workspace management app

PATH=$PATH:$LAMBDA_TASK_ROOT/bin \
    PYTHONPATH=$PYTHONPATH:/opt/python:$LAMBDA_RUNTIME_DIR \
    exec python -m uvicorn --port=$PORT main:app
