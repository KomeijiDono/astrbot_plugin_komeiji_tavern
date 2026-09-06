# Komeiji Request Relay

GPT Sites / Cloudflare Worker temporary request relay for Komeiji's Tavern.

The public site exposes only a status page, health endpoint, and unguessable
read URLs. Authenticated management endpoints create, update, and delete
short-lived plaintext request pages in D1.

## Runtime binding

- D1 binding: `DB`
- Secret runtime value: `URL_REQUEST_API_KEY`

## API

- `GET /`
- `GET|HEAD /health`
- `GET|HEAD /request/{read_token}`
- `GET /api/v1/status`
- `POST /api/v1/requests`
- `PUT /api/v1/requests/{request_id}`
- `DELETE /api/v1/requests/{request_id}`

Management APIs require `Authorization: Bearer <URL_REQUEST_API_KEY>`.
Request bodies are limited to 2 MiB. TTL must be between 30 and 3600 seconds.

`POST` and `PUT` accept plaintext `role` + `content` messages for
compatibility. The AstrBot plugin uses `content_encoding: "base64_utf8"` with
`content_base64` fields to avoid false-positive edge WAF inspection. The
Worker decodes that transport form before validation and stores the same
short-lived plaintext in D1. This Base64 wrapper is not encryption.
