/**
 * Lightweight zero-dependency HTTP + WebSocket reverse proxy on 0.0.0.0:80.
 * Routes API/Media/Admin/Health/WebSocket traffic to Daphne (127.0.0.1:8000)
 * and all UI traffic to Next.js (127.0.0.1:3100).
 */
import http from "node:http";
import net from "node:net";

const LISTEN_HOST = process.env.GATEWAY_HOST || "0.0.0.0";
const LISTEN_PORT = Number(process.env.GATEWAY_PORT || 80);
const BACKEND_HOST = process.env.BACKEND_HOST || "127.0.0.1";
const BACKEND_PORT = Number(process.env.BACKEND_PORT || 8000);
const FRONTEND_HOST = process.env.FRONTEND_HOST || "127.0.0.1";
const FRONTEND_PORT = Number(process.env.FRONTEND_PORT || 3100);

function isBackendPath(url = "/") {
  return (
    url === "/api" ||
    url.startsWith("/api/") ||
    url.startsWith("/media/") ||
    url.startsWith("/health/") ||
    url === "/metrics" ||
    url.startsWith("/metrics?") ||
    url.startsWith("/admin/") ||
    url === "/admin" ||
    url.startsWith("/static/") ||
    url.startsWith("/ws")
  );
}

const server = http.createServer((clientReq, clientRes) => {
  const toBackend = isBackendPath(clientReq.url || "/");
  const targetHost = toBackend ? BACKEND_HOST : FRONTEND_HOST;
  const targetPort = toBackend ? BACKEND_PORT : FRONTEND_PORT;

  const headers = { ...clientReq.headers };
  const remoteIp = clientReq.socket.remoteAddress || "";
  headers["x-forwarded-for"] = headers["x-forwarded-for"]
    ? `${headers["x-forwarded-for"]}, ${remoteIp}`
    : remoteIp;
  headers["x-forwarded-proto"] = headers["x-forwarded-proto"] || "http";
  if (clientReq.headers.host) {
    headers["x-forwarded-host"] = headers["x-forwarded-host"] || clientReq.headers.host;
  }

  const proxyReq = http.request(
    {
      host: targetHost,
      port: targetPort,
      path: clientReq.url,
      method: clientReq.method,
      headers,
    },
    (proxyRes) => {
      clientRes.writeHead(proxyRes.statusCode || 502, proxyRes.headers);
      proxyRes.pipe(clientRes, { end: true });
    }
  );

  proxyReq.on("error", (err) => {
    if (!clientRes.headersSent) {
      clientRes.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
    }
    clientRes.end(JSON.stringify({ success: false, message: `Upstream unavailable: ${err.message}` }));
  });

  clientReq.pipe(proxyReq, { end: true });
});

server.on("upgrade", (req, clientSocket, head) => {
  const toBackend = isBackendPath(req.url || "/");
  const targetHost = toBackend ? BACKEND_HOST : FRONTEND_HOST;
  const targetPort = toBackend ? BACKEND_PORT : FRONTEND_PORT;

  const upstream = net.connect(targetPort, targetHost, () => {
    const headerLines = [`${req.method} ${req.url} HTTP/${req.httpVersion}`];
    for (let i = 0; i < req.rawHeaders.length; i += 2) {
      headerLines.push(`${req.rawHeaders[i]}: ${req.rawHeaders[i + 1]}`);
    }
    headerLines.push("", "");
    upstream.write(headerLines.join("\r\n"));
    if (head && head.length) upstream.write(head);
    upstream.pipe(clientSocket);
    clientSocket.pipe(upstream);
  });

  upstream.on("error", () => clientSocket.destroy());
  clientSocket.on("error", () => upstream.destroy());
});

server.listen(LISTEN_PORT, LISTEN_HOST, () => {
  console.log(
    `Al-Nahrain HTTP+WS Gateway listening on http://${LISTEN_HOST}:${LISTEN_PORT} -> UI(${FRONTEND_HOST}:${FRONTEND_PORT}) API(${BACKEND_HOST}:${BACKEND_PORT})`
  );
});
