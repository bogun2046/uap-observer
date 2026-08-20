ARG UAP_GO_IMAGE=golang:1.25.13-alpine3.23@sha256:4ce6af6747b07e99ca3a57eadb77565787390a41b0039dcc8e09ec4c57cfa125
ARG UAP_SEAWEEDFS_BASE_IMAGE=chrislusf/seaweedfs:4.41@sha256:43b768cd62b00d132439cda881b93fd1adebf1b315e996e794087743821d771d

FROM ${UAP_GO_IMAGE} AS builder

ARG UAP_SEAWEEDFS_COMMIT=de34a1a87c02893507f961cda9574172ee5064e9

RUN apk add --no-cache git
WORKDIR /src
RUN git init . \
    && git remote add origin https://github.com/seaweedfs/seaweedfs.git \
    && git fetch --depth=1 origin "${UAP_SEAWEEDFS_COMMIT}" \
    && git checkout --detach FETCH_HEAD
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/weed ./weed

FROM ${UAP_SEAWEEDFS_BASE_IMAGE}

COPY --from=builder --chmod=0755 /out/weed /usr/bin/weed
