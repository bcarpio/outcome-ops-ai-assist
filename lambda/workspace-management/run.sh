#!/bin/bash
# Bootstrap script for Lambda Web Adapter
# Starts uvicorn to serve the FastAPI workspace management app

PATH=$PATH:$LAMBDA_TASK_ROOT/bin \
    PYTHONPATH=$PYTHONPATH:/opt/python:$LAMBDA_RUNTIME_DIR \
    exec python -m uvicorn --port=$PORT main:app
