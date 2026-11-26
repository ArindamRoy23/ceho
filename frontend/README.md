# CEHO browser

Login, create or join a session, and send one microphone. The page calls the hot server on the same origin.

## Development

Start the hot server, then from `frontend/`:

```bash
npm install
npm run dev
```

Vite serves the page and proxies `POST /login`, `POST /sessions`, and `GET /health` to `http://127.0.0.1:8000`. Set `CEHO_DEV_PROXY` to point that proxy at a hot server on another address. A refresh of `/login` or `/sessions/:id` stays on the page.

## Production

`npm run build` writes `dist/`. Serve those files on the hot server's HTTPS origin. Forward `POST /login` and `POST /sessions` to the hot server, and serve `index.html` for the page paths.

## Checks

```bash
npm test
npm run typecheck
npm run lint
```
