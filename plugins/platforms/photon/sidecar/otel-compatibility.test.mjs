import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import test from "node:test";
import { setupOtel } from "@photon-ai/otel";

test("Photon telemetry exports and shuts down with the installed dependency graph", { timeout: 15000 }, async () => {
  const received = new Map();
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received.set(request.url, Buffer.concat(chunks));
    response.writeHead(200, { "content-type": "application/json" });
    response.end("{}");
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const previous = Object.fromEntries(Object.entries(process.env).filter(([key]) => key.startsWith("OTEL_")));
  for (const key of Object.keys(previous)) delete process.env[key];
  let handle;
  try {
    handle = setupOtel({
      serviceName: "hermes-photon-local-test",
      endpoint: `http://127.0.0.1:${server.address().port}`,
      register: false,
      instrumentFetch: false,
    });
    handle.tracerProvider.getTracer("test").startSpan("local-span").end();
    handle.loggerProvider.getLogger("test").emit({ body: "local-log" });
    handle.getMeter("test").createCounter("local_counter").add(1);
    await Promise.all([
      handle.tracerProvider.forceFlush(),
      handle.loggerProvider.forceFlush(),
      handle.meterProvider.forceFlush(),
    ]);
    for (const signal of ["traces", "logs", "metrics"]) {
      assert.ok(received.get(`/v1/${signal}`)?.length, `${signal} must reach the local receiver`);
    }
    // Photon intentionally settles provider failures in handle.shutdown().
    // Check the providers directly so an incompatible exporter cannot hide.
    await Promise.all([
      handle.tracerProvider.shutdown(),
      handle.loggerProvider.shutdown(),
      handle.meterProvider.shutdown(),
    ]);
  } finally {
    await handle?.shutdown();
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    for (const key of Object.keys(process.env)) if (key.startsWith("OTEL_")) delete process.env[key];
    Object.assign(process.env, previous);
  }
});
