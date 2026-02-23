# ============================================================================
# Run-Tests Lambda Container Image
# ============================================================================
# Multi-language test runner for OutcomeOps code generation platform.
# Supports: Python, Java, TypeScript/Node.js, Terraform
#
# Base: Amazon Linux 2023 (compatible with Lambda container runtime)
# Size: ~1.5GB (Java is the largest component)
#
# Build:
#   ./scripts/build-run-tests-image.sh --env dev
#
# ============================================================================

FROM public.ecr.aws/lambda/python:3.12 AS builder

# Swap libcurl-minimal for full libcurl (needed for git HTTPS support)
RUN dnf swap -y libcurl-minimal libcurl

# Install build dependencies for git (including HTTPS support)
RUN dnf install -y \
    make \
    gcc \
    libcurl-devel \
    expat-devel \
    gettext-devel \
    openssl-devel \
    zlib-devel \
    pcre2-devel \
    perl-ExtUtils-MakeMaker \
    tar \
    gzip \
    && dnf clean all

# Build git from source with HTTPS support (Lambda base doesn't include git)
ARG GIT_VERSION=2.43.0
RUN curl -sL https://github.com/git/git/archive/refs/tags/v${GIT_VERSION}.tar.gz | tar xz && \
    cd git-${GIT_VERSION} && \
    make prefix=/opt/git NO_TCLTK=1 CURLDIR=/usr USE_LIBPCRE2=1 all && \
    make prefix=/opt/git NO_TCLTK=1 install && \
    cd .. && rm -rf git-${GIT_VERSION} && \
    # Verify git-remote-https was built (required for HTTPS clone/push)
    test -f /opt/git/libexec/git-core/git-remote-https || (echo "ERROR: git-remote-https not built" && exit 1)

# ============================================================================
# Final Image
# ============================================================================

FROM public.ecr.aws/lambda/python:3.12

# Install system packages
# Note: openssl libs already provided by base image (openssl-snapsafe-libs)
# Swap libcurl-minimal for full libcurl (required for git-remote-https)
RUN dnf swap -y libcurl-minimal libcurl

RUN dnf install -y \
    # Build essentials for native Python packages
    make \
    gcc \
    gcc-c++ \
    # Git runtime dependencies (for HTTPS clone support)
    expat \
    pcre2 \
    # Java 21 (Amazon Corretto)
    java-21-amazon-corretto-headless \
    # Terraform
    unzip \
    # Utilities
    which \
    tar \
    gzip \
    findutils \
    && dnf clean all

# Install Node.js 20 from NodeSource (includes working npm)
RUN curl -fsSL https://rpm.nodesource.com/setup_20.x | bash - && \
    dnf install -y nodejs && \
    dnf clean all

# Install Terraform
ARG TERRAFORM_VERSION=1.9.8
RUN curl -sL https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip -o /tmp/terraform.zip && \
    unzip /tmp/terraform.zip -d /usr/local/bin && \
    rm /tmp/terraform.zip && \
    terraform version

# Copy git from builder
COPY --from=builder /opt/git /opt/git

# Set up environment variables
ENV JAVA_HOME=/usr/lib/jvm/java-21-amazon-corretto
ENV PATH=/opt/git/bin:${JAVA_HOME}/bin:/usr/local/bin:${PATH}
ENV GIT_EXEC_PATH=/opt/git/libexec/git-core

# Verify git HTTPS support works at runtime (catches missing library issues)
RUN git --version && \
    ldd /opt/git/libexec/git-core/git-remote-https | grep -q "not found" && \
    (echo "ERROR: git-remote-https missing libraries:" && ldd /opt/git/libexec/git-core/git-remote-https && exit 1) || \
    echo "git-remote-https libraries OK"

# Install global npm packages for TypeScript
RUN npm install -g typescript ts-node @types/node

# Install Python dependencies
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Copy Lambda function code
COPY handler.py ${LAMBDA_TASK_ROOT}/
COPY auto_fix.py ${LAMBDA_TASK_ROOT}/
COPY backends/ ${LAMBDA_TASK_ROOT}/backends/

# Copy shared module for audit logging
COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

# Ensure all files are readable by Lambda runtime
RUN chmod -R 644 ${LAMBDA_TASK_ROOT}/*.py && \
    chmod -R 644 ${LAMBDA_TASK_ROOT}/backends/*.py && \
    chmod 755 ${LAMBDA_TASK_ROOT}/backends && \
    chmod -R 644 ${LAMBDA_TASK_ROOT}/shared/*.py && \
    chmod 755 ${LAMBDA_TASK_ROOT}/shared

# Set the handler
CMD ["handler.handler"]
