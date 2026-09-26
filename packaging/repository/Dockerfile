FROM debian:13
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 apt-utils dpkg-dev createrepo-c rpm gnupg gpgv ca-certificates && \
    rm -rf /var/lib/apt/lists/*
