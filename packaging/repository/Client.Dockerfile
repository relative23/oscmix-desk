ARG BASE
FROM ${BASE}
USER root
ARG TARGET
RUN case "$TARGET" in \
      debian*|ubuntu*) apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y gnupg gpgv python3-apt ;; \
      fedora44) dnf install -y gcc-c++ libdnf5-devel gnupg2 ;; \
      opensuse16) zypper --non-interactive install gnupg ;; \
      *) exit 1 ;; \
    esac
