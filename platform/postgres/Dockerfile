ARG POSTGRES_IMAGE=postgres:16.14-alpine
FROM ${POSTGRES_IMAGE}

# The upstream 16.14 image ships gosu built with a vulnerable Go standard
# library. The service always runs as postgres, so the root-only drop-privilege
# helper is unnecessary in the deployed image.
USER root
RUN rm -f /usr/local/bin/gosu
USER postgres
