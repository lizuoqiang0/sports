# 多阶段：静态构建 + nginx，容器秒级启动。
# 默认使用 DaoCloud 国内镜像；私有镜像仓库可通过 BASE_IMAGE_REGISTRY 覆盖。
ARG BASE_IMAGE_REGISTRY=docker.m.daocloud.io
FROM ${BASE_IMAGE_REGISTRY}/library/node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --prefer-offline --no-audit --no-fund
COPY . .
RUN npm run build

FROM ${BASE_IMAGE_REGISTRY}/library/nginx:1.27-alpine
COPY nginx.main.conf /etc/nginx/nginx.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 3000
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
    CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1
CMD ["nginx", "-g", "daemon off;"]
