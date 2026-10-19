# Homework 2 workbench UI. nginx serves the Vite build and proxies /api.

FROM node:22-bookworm-slim AS build

WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine

RUN apk add --no-cache wget \
    && rm -f /etc/nginx/conf.d/default.conf

COPY --from=build /src/dist /usr/share/nginx/html

COPY <<'EOF' /etc/nginx/conf.d/default.conf
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;
    charset utf-8;

    # Larger than the app's 8 MiB upload cap so the API returns its own error.
    client_max_body_size 16m;

    location /api/ {
        resolver 127.0.0.11 valid=30s ipv6=off;
        set $hw2_backend backend;
        proxy_pass http://$hw2_backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        proxy_buffering off;
        proxy_cache off;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
EOF

EXPOSE 80
