FROM public.ecr.aws/lambda/python:3.12

ARG TERRAFORM_VERSION="1.9.5"
ENV TERRAFORM_VERSION=${TERRAFORM_VERSION}

# Install system dependencies required for git/terraform/make workflows
RUN dnf -y update && \
    dnf install -y \
      git \
      tar \
      gzip \
      unzip \
      make \
      gcc \
      gcc-c++ \
      openssl-devel \
      libffi-devel \
      which \
      findutils \
      shadow-utils \
      util-linux \
      procps-ng && \
    dnf clean all && \
    rm -rf /var/cache/dnf

# Install Terraform CLI
RUN curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" -o /tmp/terraform.zip && \
    unzip /tmp/terraform.zip -d /usr/local/bin && \
    rm /tmp/terraform.zip && \
    terraform -version

# Copy run-tests Lambda source
WORKDIR /var/task
COPY lambda/run-tests/ /var/task/

# Install Lambda + runtime Python dependencies
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir \
      pytest \
      pytest-cov \
      coverage \
      moto[boto3] \
      boto3 \
      requests \
      gitpython \
      awscli \
      pip-tools

CMD ["handler.handler"]
