import fs from "node:fs";
import { spawn } from "node:child_process";
import { performance } from "node:perf_hooks";

function decodeRequest() {
  try {
    return JSON.parse(Buffer.from(process.argv[2], "base64url").toString("utf8"));
  } catch {
    process.stdout.write(JSON.stringify({ ok: false, error: { code: "INVALID_REQUEST", message: "invalid runner request" } }));
    process.exit(2);
  }
}

function pids() {
  try {
    return new Set(fs.readdirSync("/proc").filter((name) => /^\d+$/.test(name)).map(Number));
  } catch {
    return null;
  }
}

function killProcessGroup(child) {
  if (!child?.pid) return;
  try { process.kill(-child.pid, "SIGKILL"); } catch {}
  try { child.kill("SIGKILL"); } catch {}
}

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function unexpectedPids(baseline) {
  const current = pids();
  if (current === null) return null;
  return [...current].filter((pid) => !baseline.has(pid) && pid !== process.pid);
}

async function reapNewProcesses(baseline) {
  if (baseline === null) return false;
  for (let pass = 0; pass < 6; pass += 1) {
    const unexpected = unexpectedPids(baseline);
    if (unexpected === null) return false;
    for (const pid of unexpected) {
      if (!baseline.has(pid) && pid !== process.pid) {
        try { process.kill(pid, "SIGKILL"); } catch {}
      }
    }
    await sleep(75);
  }
  const remaining = unexpectedPids(baseline);
  return remaining !== null && remaining.length === 0;
}

const request = decodeRequest();
const baseline = pids();
const started = performance.now();
let stdout = Buffer.alloc(0);
let stderr = Buffer.alloc(0);
let stdoutTruncated = false;
let stderrTruncated = false;
let timedOut = false;
let outputLimited = false;
let finished = false;
let responseWritten = false;

function append(current, chunk, limit, markTruncated) {
  const remaining = Math.max(0, limit - current.length);
  if (chunk.length > remaining) markTruncated();
  return remaining ? Buffer.concat([current, chunk.subarray(0, remaining)]) : current;
}

const child = spawn(request.executable, request.args, {
  cwd: "/workspace",
  detached: true,
  env: request.environment,
  shell: false,
  stdio: ["ignore", "pipe", "pipe"],
});

const timer = setTimeout(() => {
  timedOut = true;
  killProcessGroup(child);
}, request.timeout_ms);

child.stdout.on("data", (chunk) => {
  stdout = append(stdout, chunk, request.stdout_limit, () => { stdoutTruncated = true; });
  if (stdoutTruncated && !finished) { outputLimited = true; killProcessGroup(child); }
});
child.stderr.on("data", (chunk) => {
  stderr = append(stderr, chunk, request.stderr_limit, () => { stderrTruncated = true; });
  if (stderrTruncated && !finished) { outputLimited = true; killProcessGroup(child); }
});

async function finishWithError() {
  if (responseWritten) return;
  responseWritten = true;
  clearTimeout(timer);
  finished = true;
  await reapNewProcesses(baseline);
  process.stdout.write(JSON.stringify({ ok: false, error: { code: "INTERNAL_ERROR", message: "execution process could not start" } }));
}

async function finishWithResult(code, signal) {
  if (responseWritten) return;
  responseWritten = true;
  clearTimeout(timer);
  finished = true;
  const processNamespaceClean = await reapNewProcesses(baseline);
  if (!processNamespaceClean) {
    process.stdout.write(JSON.stringify({
      ok: false,
      error: { code: "PROCESS_CLEANUP_FAILED", message: "sandbox process cleanup could not be verified" },
    }));
    return;
  }
  process.stdout.write(JSON.stringify({
    ok: true,
    exit_code: timedOut ? 124 : (outputLimited ? 137 : (code ?? (signal ? 137 : 1))),
    stdout: stdout.toString("utf8"),
    stderr: stderr.toString("utf8"),
    timed_out: timedOut,
    stdout_truncated: stdoutTruncated,
    stderr_truncated: stderrTruncated,
    output_limited: outputLimited,
    stdout_bytes: stdout.length,
    stderr_bytes: stderr.length,
    duration_ms: Math.max(0, Math.round(performance.now() - started)),
  }));
}

child.once("error", () => { void finishWithError(); });
child.once("close", (code, signal) => { void finishWithResult(code, signal); });
