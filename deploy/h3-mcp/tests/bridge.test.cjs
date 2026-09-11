const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const bridge = require("../workbuddy/bridge.cjs");

test("bridge reads only explicit media in an attachment root", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "h3-bridge-test-"));
  try {
    const attachments = path.join(root, "attachments");
    fs.mkdirSync(attachments);
    const photo = path.join(attachments, "photo.png");
    fs.writeFileSync(photo, "synthetic-file");
    assert.equal(bridge.permittedFile(photo, [attachments]), photo);
    assert.throws(() => bridge.permittedFile(photo, []), /outside_attachment/);
    assert.equal(bridge.permittedFile(photo, [], true), photo);
    const secret = path.join(attachments, "token.json");
    fs.writeFileSync(secret, "private-placeholder");
    assert.throws(() => bridge.permittedFile(secret, [attachments], true), /not_a_supported/);
    assert.throws(() => bridge.permittedFile("https://example.invalid/image.png", [attachments]), /explicit_local/);
    assert.throws(() => bridge.permittedFile("photo.png", [attachments]), /explicit_local/);
  } finally { fs.rmSync(root, {recursive: true, force: true}); }
});

test("bridge rejects symbolic links and empty files", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "h3-bridge-links-"));
  try {
    const target = path.join(root, "photo.png");
    fs.writeFileSync(target, "media");
    const link = path.join(root, "alias.png");
    fs.symlinkSync(target, link);
    assert.throws(() => bridge.permittedFile(link, [root]), /symlink_attachment/);
    fs.writeFileSync(target, "");
    assert.throws(() => bridge.permittedFile(target, [root]), /asset_size/);
  } finally { fs.rmSync(root, {recursive: true, force: true}); }
});

test("upload interface requires role and stable operation and exposes no credentials", () => {
  assert.deepEqual(bridge.UPLOAD_TOOL.inputSchema.required, ["operation_id", "kind"]);
  assert.equal(bridge.UPLOAD_TOOL.inputSchema.additionalProperties, false);
  assert.equal(bridge.UPLOAD_TOOL.inputSchema.properties.token, undefined);
  assert.equal(bridge.UPLOAD_STATUS_TOOL.annotations.readOnlyHint, true);
});

test("request framing limits incomplete lines and preserves chunked UTF8", async () => {
  const {Readable} = require("node:stream");
  const source = Buffer.from('{"prompt":"粉色房间"}\n{"id":2}\n');
  const actual = [];
  for await (const line of bridge.messages(Readable.from([source.subarray(0, 13), source.subarray(13)]))) actual.push(JSON.parse(line));
  assert.deepEqual(actual, [{prompt: "粉色房间"}, {id: 2}]);
  await assert.rejects(async () => {
    for await (const line of bridge.messages(Readable.from([Buffer.alloc(262145, 65)]))) assert.fail(line);
  }, /request_limit_exceeded/);
});

test("incomplete upload receipts cannot be mistaken for ready assets", () => {
  assert.equal(bridge.toolResult({state: "uploading"}).isError, true);
  assert.equal(bridge.toolResult({state: "failed"}).isError, true);
  assert.equal(bridge.toolResult({state: "ready"}).isError, undefined);
});
