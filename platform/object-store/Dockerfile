ARG UAP_GO_IMAGE=golang:1.26-alpine3.23@sha256:33ce311e5eecedee48ec1b84419c1306e9fbd71009f0d5c3f2a6904b579c1ecc
ARG UAP_SEAWEEDFS_BASE_IMAGE=chrislusf/seaweedfs:4.41@sha256:43b768cd62b00d132439cda881b93fd1adebf1b315e996e794087743821d771d

FROM ${UAP_GO_IMAGE} AS builder

ARG UAP_SEAWEEDFS_COMMIT=e919bec9d194b61aa07cef75f46b207dd687a019
ARG UAP_GRPC_VERSION=v1.85.0-dev.0.20260825072537-93e31b48545e

RUN apk add --no-cache git
WORKDIR /src
RUN git init . \
    && git remote add origin https://github.com/seaweedfs/seaweedfs.git \
    && git fetch --depth=1 origin "${UAP_SEAWEEDFS_COMMIT}" \
    && git checkout --detach FETCH_HEAD
RUN go get "google.golang.org/grpc@${UAP_GRPC_VERSION}" \
    && go mod tidy
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/weed ./weed

FROM ${UAP_SEAWEEDFS_BASE_IMAGE}

USER root
RUN apk add --no-cache --upgrade 'libcrypto3>=3.5.8-r0' 'libssl3>=3.5.8-r0'
COPY --from=builder --chmod=0755 /out/weed /usr/bin/weed
