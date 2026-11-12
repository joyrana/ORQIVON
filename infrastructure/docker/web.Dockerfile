# Web workspace: build static assets, serve with nginx, proxy /api to the API service.
FROM node:24-alpine AS build
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web ./
RUN npm run build

FROM nginx:1.29-alpine
COPY infrastructure/docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/agentic-sdlc/
EXPOSE 8080
