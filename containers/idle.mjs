const lifetimeSeconds = Number.parseInt(process.argv[2] ?? "", 10);

if (!Number.isSafeInteger(lifetimeSeconds) || lifetimeSeconds < 1 || lifetimeSeconds > 3600) {
  process.exit(2);
}

// This is independent of the Python process. If the MCP client or host process
// disappears without cleanup, Docker stops and auto-removes the container when
// this hard maximum lifetime elapses.
setTimeout(() => process.exit(0), lifetimeSeconds * 1000);
