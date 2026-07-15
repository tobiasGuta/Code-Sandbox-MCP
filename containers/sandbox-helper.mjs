import fs from "node:fs";
import path from "node:path";

const ROOT = "/workspace";

function fail(code, message) {
  process.stdout.write(JSON.stringify({ ok: false, error: { code, message } }));
  process.exit(2);
}

function decode(index) {
  try {
    return JSON.parse(Buffer.from(process.argv[index], "base64url").toString("utf8"));
  } catch {
    fail("INVALID_REQUEST", "invalid helper request");
  }
}

function components(relative) {
  if (typeof relative !== "string" || relative === "" || relative.includes("\0")) {
    fail("INVALID_PATH", "invalid workspace path");
  }
  if (relative === ".") return [];
  const parts = relative.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) {
    fail("INVALID_PATH", "invalid workspace path");
  }
  return parts;
}

function checkedPath(relative, { missingFinal = false } = {}) {
  const parts = components(relative);
  let current = ROOT;
  for (let index = 0; index < parts.length; index += 1) {
    current = path.join(current, parts[index]);
    let stat;
    try {
      stat = fs.lstatSync(current);
    } catch (error) {
      if (missingFinal && index === parts.length - 1 && error?.code === "ENOENT") return current;
      fail("FILE_NOT_FOUND", "workspace path does not exist");
    }
    if (stat.isSymbolicLink() || (!stat.isDirectory() && !stat.isFile())) {
      fail("UNSAFE_FILE_TYPE", "symlinks and special files are forbidden");
    }
    if (stat.isFile() && stat.nlink > 1) {
      fail("UNSAFE_FILE_TYPE", "hard-linked files are forbidden");
    }
    if (index < parts.length - 1 && !stat.isDirectory()) {
      fail("INVALID_PATH", "a path component is not a directory");
    }
  }
  return current;
}

function entryType(stat) {
  if (stat.isFile()) return "file";
  if (stat.isDirectory()) return "directory";
  return "unsafe";
}

function prepareWritePath(relative) {
  const parts = components(relative);
  let current = ROOT;
  for (let index = 0; index < parts.length - 1; index += 1) {
    current = path.join(current, parts[index]);
    if (!fs.existsSync(current)) fs.mkdirSync(current, { mode: 0o700 });
    const stat = fs.lstatSync(current);
    if (!stat.isDirectory() || stat.isSymbolicLink()) {
      fail("UNSAFE_FILE_TYPE", "write path contains a link or non-directory component");
    }
  }
  const target = path.join(current, parts.at(-1));
  if (fs.existsSync(target)) {
    const stat = fs.lstatSync(target);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink > 1) {
      fail("UNSAFE_FILE_TYPE", "write target is not a regular, non-linked file");
    }
  }
  return target;
}

async function readStdin(expected) {
  if (expected === 0) return Buffer.alloc(0);
  return new Promise((resolve, reject) => {
    const chunks = [];
    let received = 0;
    const onData = (chunk) => {
      chunks.push(chunk);
      received += chunk.length;
      if (received >= expected) {
        process.stdin.pause();
        process.stdin.off("data", onData);
        if (received !== expected) reject(new Error("file payload length mismatch"));
        else resolve(Buffer.concat(chunks));
      }
    };
    process.stdin.on("data", onData);
    process.stdin.on("end", () => reject(new Error("file payload ended early")), { once: true });
    process.stdin.resume();
  });
}

function inventory(start = ".", recursive = true, maxDepth = 20, responseLimit = 1001) {
  const base = checkedPath(start);
  const rootStat = fs.lstatSync(base);
  if (!rootStat.isDirectory()) {
    return [{ path: start, type: entryType(rootStat), size: rootStat.size, links: rootStat.nlink }];
  }
  const output = [];
  const queue = [{ absolute: base, relative: start === "." ? "" : start, depth: 0 }];
  while (queue.length && output.length < responseLimit) {
    const item = queue.shift();
    const names = fs.readdirSync(item.absolute).sort();
    for (const name of names) {
      const absolute = path.join(item.absolute, name);
      const relative = item.relative ? `${item.relative}/${name}` : name;
      const stat = fs.lstatSync(absolute);
      const type = entryType(stat);
      output.push({ path: relative, type, size: stat.isFile() ? stat.size : 0, links: stat.nlink });
      if (output.length >= responseLimit) break;
      if (type === "directory" && recursive && item.depth < maxDepth) {
        queue.push({ absolute, relative, depth: item.depth + 1 });
      }
    }
  }
  return output;
}

const operation = process.argv[2];
const request = decode(3);

try {
  if (operation === "write") {
    const expected = request.files.reduce((total, item) => total + item.size, 0);
    const payload = await readStdin(expected);
    if (payload.length !== expected) fail("INVALID_REQUEST", "file payload length mismatch");
    let offset = 0;
    const written = [];
    for (const item of request.files) {
      const target = prepareWritePath(item.path);
      const content = payload.subarray(offset, offset + item.size);
      offset += item.size;
      fs.writeFileSync(target, content, { mode: 0o600, flag: "w" });
      fs.chmodSync(target, 0o600);
      written.push(item.path);
    }
    process.stdout.write(JSON.stringify({ ok: true, written }));
  } else if (operation === "inventory" || operation === "list") {
    const entries = inventory(
      request.path ?? ".",
      operation === "inventory" ? true : request.recursive,
      operation === "inventory" ? 20 : request.max_depth,
      request.limit,
    );
    process.stdout.write(JSON.stringify({ ok: true, entries, truncated: entries.length >= request.limit }));
  } else if (operation === "read") {
    const target = checkedPath(request.path);
    const stat = fs.lstatSync(target);
    if (!stat.isFile()) fail("INVALID_PATH", "requested path is not a file");
    if (stat.nlink > 1) fail("UNSAFE_FILE_TYPE", "hard-linked files are forbidden");
    const handle = fs.openSync(target, "r");
    const buffer = Buffer.alloc(Math.min(stat.size, request.max_bytes + 1));
    const read = fs.readSync(handle, buffer, 0, buffer.length, 0);
    fs.closeSync(handle);
    process.stdout.write(JSON.stringify({
      ok: true,
      content_base64: buffer.subarray(0, Math.min(read, request.max_bytes)).toString("base64"),
      size: stat.size,
      truncated: stat.size > request.max_bytes,
    }));
  } else if (operation === "delete") {
    const deleted = [];
    const missing = [];
    for (const relative of request.paths) {
      const lexicalTarget = path.join(ROOT, ...components(relative));
      if (!fs.existsSync(lexicalTarget)) { missing.push(relative); continue; }
      const target = checkedPath(relative);
      const stat = fs.lstatSync(target);
      if (!stat.isFile() || stat.nlink > 1) fail("UNSAFE_FILE_TYPE", "only regular, non-linked files may be deleted");
      fs.unlinkSync(target);
      deleted.push(relative);
    }
    process.stdout.write(JSON.stringify({ ok: true, deleted, missing }));
  } else {
    fail("INVALID_REQUEST", "unknown helper operation");
  }
} catch {
  fail("INTERNAL_ERROR", "sandbox helper operation failed");
}
