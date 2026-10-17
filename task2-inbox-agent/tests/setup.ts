import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// Tests write candidate prompts; keep them out of the real prompts/ folder.
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "inbox-prompts-"));
fs.cpSync(path.resolve(__dirname, "..", "prompts"), tmp, { recursive: true });
process.env.INBOX_PROMPT_DIR = tmp;
process.env.LLM_PROVIDER = "fake";
