import { mkdirSync, writeFileSync, readFileSync, readdirSync, existsSync, statSync, cpSync } from "node:fs";
import { join } from "node:path";
import { parse, stringify } from "yaml";
import type { Grade } from "./states.js";
import type { IntakeItem } from "./intake.js";

/**
 * The knowledge-package file contract. One decided case = one directory:
 *
 *   knowledge/<date>-<slug>/
 *   ├── CASE.md              the PROBLEM — all a handler may see at replay
 *   ├── evidence/
 *   │   ├── case.json        the frozen intake metadata (replay input)
 *   │   ├── source/…         the case home's source/ zone, frozen as decided
 *   │   ├── artifacts/…      the pipeline's keepers, frozen as decided
 *   │   └── logs/…           the execution record — how the pipeline got here
 *   └── ANSWER.md            the VERIFICATION — per-field {value, grade} + analysis
 *
 * Git is the source of truth; any index over these files is rebuildable.
 * The split is what makes benchmarking possible: replay feeds CASE +
 * evidence/case.json + evidence/source (never evidence/artifacts — those are
 * the PREVIOUS pipeline's outputs); ANSWER never enters the handler's context.
 */
export interface KnowledgeFields {
  [field: string]: { value: unknown; grade: Grade };
}

export interface KnowledgePackage {
  dir: string;                    // package directory name
  case_id: string;
  handler_id: string;
  source_id: string;
  title: string;
  tags: string[];
  banked_at: string;
  case: IntakeItem;               // the frozen intake metadata (from evidence/case.json)
  fields: KnowledgeFields;        // ANSWER frontmatter
  decided_by: string;
  lesson: string;
  analysis: string;
  archived?: boolean;
}

export function knowledgeSlug(title: string, date: string): string {
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48) || "case";
  return `${date.slice(0, 10)}-${slug}`;
}

/** Total budget for artifact evidence copied into a package. */
const EVIDENCE_BUDGET_BYTES = 20 * 1024 * 1024;
const EVIDENCE_FILE_CAP_BYTES = 5 * 1024 * 1024;

export function writeKnowledgePackage(
  root: string, pkg: Omit<KnowledgePackage, "dir">,
  dirs: { sourceDir?: string; artifactsDir?: string; logsDir?: string } = {},
): string {
  let dir = knowledgeSlug(pkg.title, pkg.banked_at);
  let path = join(root, dir);
  for (let n = 2; existsSync(path); n++) { path = join(root, `${dir}-${n}`); }
  dir = path.slice(root.length + 1);
  mkdirSync(join(path, "evidence"), { recursive: true });

  // Promote the case home's durable zones into permanent evidence (size-capped;
  // context/ never crosses — it's rebuildable by contract).
  let budget = EVIDENCE_BUDGET_BYTES;
  const copyDir = (src: string, dst: string) => {
    for (const entry of readdirSync(src, { withFileTypes: true })) {
      const s0 = join(src, entry.name), d0 = join(dst, entry.name);
      if (entry.isDirectory()) { mkdirSync(d0, { recursive: true }); copyDir(s0, d0); continue; }
      const size = statSync(s0).size;
      if (size > EVIDENCE_FILE_CAP_BYTES || size > budget) continue; // skip oversized; pointers stay in ANSWER analysis
      cpSync(s0, d0);
      budget -= size;
    }
  };
  for (const [name, src] of [["source", dirs.sourceDir], ["artifacts", dirs.artifactsDir], ["logs", dirs.logsDir]] as const) {
    if (!src || !existsSync(src)) continue;
    const dst = join(path, "evidence", name);
    mkdirSync(dst, { recursive: true });
    copyDir(src, dst);
  }

  const caseFm = {
    case: pkg.case_id, handler: pkg.handler_id, source: pkg.source_id,
    title: pkg.title, tags: pkg.tags, banked_at: pkg.banked_at,
  };
  writeFileSync(join(path, "CASE.md"),
    `---\n${stringify(caseFm)}---\n# ${pkg.title}\n\n` +
    `The problem as it arrived (reporter's words verbatim; full record in evidence/case.json):\n\n` +
    "```json\n" + JSON.stringify(pkg.case, null, 2) + "\n```\n");

  writeFileSync(join(path, "evidence", "case.json"), JSON.stringify(pkg.case, null, 2));

  const answerFm = { fields: pkg.fields, decided_by: pkg.decided_by, decided_at: pkg.banked_at, archived: false };
  writeFileSync(join(path, "ANSWER.md"),
    `---\n${stringify(answerFm)}---\n## Lesson\n${pkg.lesson || "(none recorded)"}\n\n## Analysis\n${pkg.analysis || "(none recorded)"}\n`);
  return path;
}

function frontmatter(md: string): { fm: Record<string, unknown>; body: string } {
  const m = md.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/);
  if (!m) return { fm: {}, body: md };
  return { fm: (parse(m[1]) as Record<string, unknown>) ?? {}, body: m[2] };
}

export function readKnowledgePackage(path: string): KnowledgePackage | undefined {
  try {
    const caseMd = frontmatter(readFileSync(join(path, "CASE.md"), "utf8"));
    const answerMd = frontmatter(readFileSync(join(path, "ANSWER.md"), "utf8"));
    const env = JSON.parse(readFileSync(join(path, "evidence", "case.json"), "utf8")) as IntakeItem;
    const cf = caseMd.fm as Record<string, string> & { tags?: string[] };
    const af = answerMd.fm as { fields?: KnowledgeFields; decided_by?: string; archived?: boolean };
    const lesson = /## Lesson\n([\s\S]*?)(\n## |$)/.exec(answerMd.body)?.[1]?.trim() ?? "";
    const analysis = /## Analysis\n([\s\S]*?)$/.exec(answerMd.body)?.[1]?.trim() ?? "";
    return {
      dir: path.split("/").pop()!,
      case_id: cf.case ?? "", handler_id: cf.handler ?? "", source_id: cf.source ?? "",
      title: cf.title ?? env.title, tags: cf.tags ?? [], banked_at: cf.banked_at ?? "",
      case: env, fields: af.fields ?? {}, decided_by: af.decided_by ?? "",
      lesson, analysis, archived: af.archived === true,
    };
  } catch {
    return undefined; // not a well-formed package; skip
  }
}

/**
 * Archive every non-archived package for a case — called before banking a new
 * generation, so recall and the bench always see the current truth while
 * history stays inspectable (append-only friendly: a frontmatter flag flips,
 * nothing is deleted).
 */
export function archivePackagesForCase(root: string, caseId: string): number {
  let n = 0;
  for (const pkg of scanKnowledge(root)) {
    if (pkg.case_id !== caseId) continue;
    const answerPath = join(root, pkg.dir, "ANSWER.md");
    const md = readFileSync(answerPath, "utf8");
    writeFileSync(answerPath, md.replace(/^archived: false$/m, "archived: true"));
    n++;
  }
  return n;
}

/** Scan a knowledge root. Archived packages are excluded unless asked for. */
export function scanKnowledge(root: string, opts: { handler?: string; includeArchived?: boolean } = {}): KnowledgePackage[] {
  if (!existsSync(root)) return [];
  const out: KnowledgePackage[] = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const pkg = readKnowledgePackage(join(root, entry.name));
    if (!pkg) continue;
    if (pkg.archived && !opts.includeArchived) continue;
    if (opts.handler && pkg.handler_id !== opts.handler) continue;
    out.push(pkg);
  }
  return out.sort((a, b) => (a.banked_at < b.banked_at ? 1 : -1));
}

/**
 * Naive lexical scorer over packages — the recall plugin's grep. Rebuildable
 * by construction (it IS a scan); swap for FTS/embeddings behind the same
 * signature when scale demands.
 */
export function searchKnowledge(root: string, query: string, k = 5, handler?: string):
  (KnowledgePackage & { score: number })[] {
  const terms = query.toLowerCase().split(/\W+/).filter((t) => t.length > 1);
  if (!terms.length) return [];
  return scanKnowledge(root, { handler })
    .map((pkg) => {
      const hay = `${pkg.title} ${pkg.tags.join(" ")} ${pkg.lesson} ${pkg.analysis} ${JSON.stringify(pkg.case.meta)}`.toLowerCase();
      const score = terms.reduce((s, t) => s + (hay.split(t).length - 1) * (pkg.title.toLowerCase().includes(t) ? 2 : 1), 0);
      return Object.assign(pkg, { score });
    })
    .filter((p) => p.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, k);
}
