/* Usage: tsx src/cli.ts <command>
 *   seed [all|dev|test]        load the 50-email dataset into the inbox
 *   process <emailId|new>      draft one email, or every new one
 *   learn                      propose a candidate drafting prompt from reviewer edits
 *   activate <draft|classify> <vN>   switch prompt version (rollback = activate the old one)
 */
import { createDeps, seedInbox } from "./app.js";
import { proposeRevision } from "./feedback/learn.js";

async function main() {
  const [cmd, ...args] = process.argv.slice(2);
  const app = createDeps();
  switch (cmd) {
    case "seed": {
      const n = seedInbox(app, (args[0] as "all" | "dev" | "test") ?? "all");
      console.log(`inserted ${n} email(s)`);
      break;
    }
    case "process": {
      const ids = args[0] === "new" || !args[0]
        ? (app.db.prepare("SELECT id FROM emails WHERE status IN ('new','error') ORDER BY received_at").all() as { id: string }[]).map((r) => r.id)
        : [args[0]];
      for (const id of ids) {
        try {
          const d = await app.pipeline.process(id);
          console.log(`${id}: ${d.category} / ${d.risk} / kb=${d.kb_status} / ${d.status} / guards=${d.guards.map((g) => g.code).join(",") || "none"}`);
        } catch (e) {
          console.log(`${id}: FAILED ${(e as Error).message}`);
        }
      }
      break;
    }
    case "learn": {
      const r = await proposeRevision(app);
      console.log(`Candidate prompt draft.${r.version} (from draft.${r.from})\n\n${r.analysis}\n\nNew guidelines:\n${r.guidelines.map((g) => `  - ${g}`).join("\n")}\n`);
      console.log(`Next: npm run eval -- --prompt ${r.version}   (compare with the active version on the held-out emails)`);
      console.log(`Then activate it in the UI (Learning tab) or: npx tsx src/cli.ts activate draft ${r.version}`);
      break;
    }
    case "activate": {
      app.prompts.activate(args[0] as "draft" | "classify", args[1]);
      app.audit.log("cli", "prompt_activated", "prompt", `${args[0]}.${args[1]}`);
      console.log(`${args[0]}.${args[1]} is active`);
      break;
    }
    default:
      console.log("commands: seed [all|dev|test] | process <id|new> | learn | activate <draft|classify> <vN>");
      process.exitCode = 1;
  }
}

main().catch((e) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
