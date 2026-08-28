# Dev scaffolding for the Linux compat layer. NOT in package.sh's MANIFEST —
# this never ships to a teammate, same as wrap-* and lg-probe.
#
# A minimal linux/amd64 host with the real tools the compat layer depends on
# (git, curl, diff, python3, and all three port backends) and nothing else. The
# Goodword dev stack (bun/pnpm/mise/gh/aws/claude/agent-browser/archon) is stubbed
# by run.sh at test time, so the smoke exercises install.sh's own logic rather
# than a real environment. What it therefore CANNOT prove is listed in run.sh.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
# LANG is deliberately left unset: the default C locale is exactly the condition
# under which the Python helpers used to raise UnicodeDecodeError on em-dashes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         ca-certificates curl diffutils findutils git iproute2 lsof psmisc python3 python3-yaml xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Prove the image has what the compat layer probes for, at build time.
RUN set -eux; \
    command -v git; command -v curl; command -v diff; command -v python3; \
    command -v lsof; command -v ss; command -v fuser; command -v xdg-open; \
    python3 -c 'import sys, yaml; assert sys.version_info >= (3, 7), sys.version'

WORKDIR /smoke
CMD ["bash", "/smoke/inside.sh"]
