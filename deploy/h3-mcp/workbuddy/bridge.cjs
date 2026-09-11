"use strict";

const fs = require("node:fs");
const path = require("node:path");
const https = require("node:https");
const crypto = require("node:crypto");
const {execFile} = require("node:child_process");

const MAX_BYTES = 128 * 1024 * 1024;
const ENDPOINT = "https://ai-x10drg.taild500c8.ts.net:4001/mcp/h3";
const KINDS = ["first_frame", "last_frame", "reference_image", "reference_video", "reference_audio"];
const UPLOAD_TOOL = {name: "h3_upload_asset", description: "Upload an explicitly provided local attachment to H3; returns immutable asset_id and SHA256. Never converts an image to text. Outside attachment roots, use choose_file=true to let the user pick the exact file. Retry by the same operation_id only after checking upload receipt.",
  inputSchema: {type: "object", additionalProperties: false, required: ["operation_id", "kind"], properties: {
    operation_id: {type: "string", pattern: "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"},
    kind: {type: "string", enum: KINDS}, local_path: {type: "string", maxLength: 4096}, choose_file: {type: "boolean"}}},
  annotations: {readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false}};
const UPLOAD_STATUS_TOOL = {name: "h3_get_upload", description: "Reconcile this account's original upload operation without uploading or generating.",
  inputSchema: {type: "object", additionalProperties: false, required: ["operation_id"], properties: {
    operation_id: UPLOAD_TOOL.inputSchema.properties.operation_id}},
  annotations: {readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false}};

function settings(filename) {
  const value = JSON.parse(fs.readFileSync(filename, "utf8"));
  if (value.endpoint !== ENDPOINT || !Array.isArray(value.attachment_roots)) throw new Error("invalid_private_bridge_configuration");
  const credentials = JSON.parse(fs.readFileSync(value.credential_file, "utf8"));
  if (typeof credentials.token !== "string" || credentials.token.length < 32) throw new Error("authentication_required");
  return {...value, token: credentials.token};
}

function request(config, suffix, body, headers = {}, method = "POST") {
  return new Promise((resolve, reject) => {
    const outgoing = https.request(config.endpoint + suffix, {method, headers: {
      Authorization: "Bearer " + config.token, Accept: "application/json, text/event-stream",
      "MCP-Protocol-Version": "2025-03-26", ...headers}}, response => {
      let size = 0;
      const chunks = [];
      response.on("data", chunk => {
        size += chunk.length;
        if (size > 2 * 1024 * 1024) response.destroy(new Error("response_limit_exceeded"));
        else chunks.push(chunk);
      });
      response.on("error", () => reject(new Error("transport_result_unknown")));
      response.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        if (response.statusCode >= 300) return resolve({http_status: response.statusCode, error: "server_rejected_or_result_unknown"});
        try { resolve(JSON.parse(text)); } catch { reject(new Error("invalid_server_response")); }
      });
    });
    const timer = setTimeout(() => outgoing.destroy(new Error("timeout")), 180000);
    outgoing.once("close", () => clearTimeout(timer));
    outgoing.on("error", () => reject(new Error("transport_result_unknown_reconcile_operation")));
    if (body && typeof body.pipe === "function") {
      body.on("error", () => outgoing.destroy());
      outgoing.on("close", () => body.destroy());
      body.pipe(outgoing);
    } else outgoing.end(body);
  });
}

function chooseFile() {
  if (process.platform !== "win32") return Promise.reject(new Error("native_file_picker_requires_windows"));
  const script = "Add-Type -AssemblyName System.Windows.Forms; $dialog=New-Object System.Windows.Forms.OpenFileDialog; $dialog.Title='选择明确授权上传到 H3 的素材'; $dialog.Multiselect=$false; if($dialog.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8; [Console]::Write($dialog.FileName)}";
  return new Promise((resolve, reject) => execFile("powershell.exe", ["-NoProfile", "-Sta", "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")],
    {timeout: 120000, windowsHide: false, maxBuffer: 16384}, (error, stdout) => {
      if (error || !stdout.trim()) reject(new Error("file_selection_cancelled"));
      else resolve(stdout.trim());
    }));
}

function permittedFile(filename, roots, selected = false) {
  if (typeof filename !== "string" || !path.isAbsolute(filename) || filename.startsWith("\\\\") || filename.includes("\0")) throw new Error("explicit_local_attachment_required");
  const resolved = fs.realpathSync(filename);
  const comparable = value => process.platform === "win32" ? value.toLowerCase() : value;
  if (comparable(resolved) !== comparable(path.resolve(filename)) || fs.lstatSync(filename).isSymbolicLink()) throw new Error("symlink_attachment_denied");
  const segments = resolved.split(/[\\/]/).map(value => value.toLowerCase());
  if (segments.some(value => [".ssh", ".aws", ".azure", "credentials", "secrets"].includes(value)) || !/\.(png|jpe?g|webp|mp4|mov|mkv|webm|wav|mp3|m4a|aac|flac|ogg)$/i.test(resolved)) throw new Error("not_a_supported_media_file");
  const allowed = roots.some(root => {
    try {
      const relative = path.relative(comparable(fs.realpathSync(root)), comparable(resolved));
      return relative && !relative.startsWith("..") && !path.isAbsolute(relative);
    } catch { return false; }
  });
  if (!selected && !allowed) throw new Error("outside_attachment_roots_use_native_file_selection");
  const stat = fs.statSync(resolved);
  if (!stat.isFile() || stat.size <= 0 || stat.size > MAX_BYTES) throw new Error("asset_size_limit_exceeded");
  return resolved;
}

async function upload(config, args) {
  if (!args || Object.keys(args).some(key => !["operation_id", "kind", "local_path", "choose_file"].includes(key)) ||
      !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(args.operation_id || "") || !KINDS.includes(args.kind) ||
      (args.choose_file !== undefined && typeof args.choose_file !== "boolean")) throw new Error("invalid_upload_arguments");
  const filename = permittedFile(args.choose_file ? await chooseFile() : args.local_path, config.attachment_roots, args.choose_file === true);
  const descriptor = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  try {
    const before = fs.fstatSync(descriptor);
    const verified = permittedFile(filename, config.attachment_roots, args.choose_file === true);
    const current = fs.statSync(verified);
    if (before.dev !== current.dev || before.ino !== current.ino || before.nlink !== 1) throw new Error("attachment_identity_changed");
    if (!before.isFile() || before.size <= 0 || before.size > MAX_BYTES) throw new Error("asset_size_limit_exceeded");
    const hash = crypto.createHash("sha256");
    const chunk = Buffer.alloc(1024 * 1024);
    for (let position = 0; position < before.size;) {
      const count = fs.readSync(descriptor, chunk, 0, Math.min(chunk.length, before.size - position), position);
      if (!count) throw new Error("attachment_changed");
      hash.update(chunk.subarray(0, count)); position += count;
    }
    const after = fs.fstatSync(descriptor);
    if (before.size !== after.size || before.mtimeMs !== after.mtimeMs) throw new Error("attachment_changed");
    const metadata = {operation_id: args.operation_id, kind: args.kind, filename: path.basename(filename), size: before.size, sha256: hash.digest("hex")};
    const receipt = await request(config, "/assets/uploads/" + encodeURIComponent(args.operation_id), undefined, {}, "GET");
    if (!receipt.http_status) {
      if (receipt.sha256 !== metadata.sha256 || receipt.kind !== metadata.kind || receipt.filename !== metadata.filename || receipt.size !== metadata.size) throw new Error("upload_operation_conflict");
      return receipt;
    }
    if (receipt.http_status !== 404) return receipt;
    const body = fs.createReadStream(filename, {fd: descriptor, autoClose: false, start: 0, end: before.size - 1});
    return await request(config, "/assets/uploads", body, {"Content-Type": "application/octet-stream", "Content-Length": before.size,
      "X-H3-Upload-Metadata": JSON.stringify(metadata).replace(/[\u007f-\uffff]/g, character => "\\u" + character.charCodeAt(0).toString(16).padStart(4, "0"))});
  } finally { fs.closeSync(descriptor); }
}

function toolResult(value) {
  return {content: [{type: "text", text: JSON.stringify(value)}], structuredContent: value,
    ...(value.error || ["failed", "uploading"].includes(value.state) ? {isError: true} : {})};
}

async function* messages(input) {
  let pending = Buffer.alloc(0);
  for await (const chunk of input) {
    let start = 0;
    while (start < chunk.length) {
      const end = chunk.indexOf(10, start);
      const part = chunk.subarray(start, end < 0 ? chunk.length : end);
      if (pending.length + part.length > 256 * 1024) throw new Error("request_limit_exceeded");
      pending = Buffer.concat([pending, part]);
      if (end < 0) break;
      if (pending.length) yield pending.toString("utf8");
      pending = Buffer.alloc(0);
      start = end + 1;
    }
  }
  if (pending.length) yield pending.toString("utf8");
}

async function handle(config, message) {
  if (message.id === undefined) return null;
  if (message.method === "tools/call" && ["h3_upload_asset", "h3_get_upload"].includes(message.params?.name)) {
    const args = message.params.arguments || {};
    let value;
    if (message.params.name === "h3_upload_asset") value = await upload(config, args);
    else {
      if (Object.keys(args).length !== 1 || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(args.operation_id || "")) throw new Error("invalid_upload_operation");
      value = await request(config, "/assets/uploads/" + encodeURIComponent(args.operation_id), undefined, {}, "GET");
    }
    return {jsonrpc: "2.0", id: message.id, result: toolResult(value)};
  }
  if (!["initialize", "ping", "tools/list", "tools/call"].includes(message.method)) return {jsonrpc: "2.0", id: message.id, error: {code: -32601, message: "unsupported_method"}};
  const response = await request(config, "", JSON.stringify(message), {"Content-Type": "application/json"});
  if (response.http_status) throw new Error("authentication_or_service_unavailable");
  if (message.method === "tools/list" && response.result?.tools) response.result.tools.push(UPLOAD_TOOL, UPLOAD_STATUS_TOOL);
  return response;
}

async function main() {
  const config = settings(process.argv[2]);
  for await (const line of messages(process.stdin)) {
    let message;
    try {
      if (line.length > 256 * 1024) throw new Error("request_limit_exceeded");
      message = JSON.parse(line);
      const response = await handle(config, message);
      if (response) process.stdout.write(JSON.stringify(response) + "\n");
    } catch (error) {
      if (message?.id !== undefined) {
        const reason = String(error.message).match(/^[a-z_]+$/) ? error.message : "local_attachment_operation_failed";
        const failure = message.method === "tools/call" ? {result: toolResult({error: reason, status: "needs_context"})}
          : {error: {code: -32000, message: reason}};
        process.stdout.write(JSON.stringify({jsonrpc: "2.0", id: message.id, ...failure}) + "\n");
      }
    }
  }
}

module.exports = {permittedFile, upload, handle, settings, messages, toolResult, UPLOAD_TOOL, UPLOAD_STATUS_TOOL};
if (require.main === module) main().catch(() => { process.stderr.write("H3 bridge configuration unavailable\n"); process.exitCode = 1; });
