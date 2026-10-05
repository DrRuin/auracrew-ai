FROM node:24-alpine AS build
WORKDIR /ui
COPY criteria-ui/package.json criteria-ui/package-lock.json ./
RUN npm ci
COPY criteria-ui ./
RUN npm run build

FROM nginx:alpine
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /ui/dist /usr/share/nginx/html
