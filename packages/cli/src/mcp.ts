import { createInterface } from "node:readline";
import type { HubClient } from "@caseflow/runtime";

/**
 * Minimal MCP server (stdio, JSON-RPC 2.0) — the wiki door for agents.
 * Tools: recall_knowledge(query, k?) and get_case(id). Read-only by design;
 * writes go through the receipted hub path exclusively.
 *
 * Wire it into any MCP-speaking agent, e.g. Claude Code:
 *   claude mcp add caseflow -- caseflow mcp
 */
export function runMcpServer(hub: HubClient): void {
  const TOOLS = [
    {
      name: "recall_knowledge",
      description: "Search this team's verified case knowledge (past decided cases: lessons, outcomes). Use when judging a case that may resemble past ones.",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", description: "what the case is about" },
          k: { type: "number", description: "max results (default 5)" },
        },
        required: ["query"],
      },
    },
    {
      name: "get_case",
      description: "Fetch a live caseflow case by id: payload, state, latest stage results.",
      inputSchema: {
        type: "object",
        properties: { id: { type: "string" } },
        required: ["id"],
      },
    },
  ];

  const reply = (id: unknown, result: unknown) =>
    process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n");
  const replyError = (id: unknown, code: number, message: string) =>
    process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }) + "\n");

  const rl = createInterface({ input: process.stdin });
  rl.on("line", (line) => {
    void (async () => {
      let msg: { jsonrpc?: string; id?: unknown; method?: string; params?: Record<string, unknown> };
      try { msg = JSON.parse(line); } catch { return; }
      if (msg.id === undefined) return; // notification; nothing to answer
      try {
        switch (msg.method) {
          case "initialize":
            return reply(msg.id, {
              protocolVersion: "2024-11-05",
              capabilities: { tools: {} },
              serverInfo: { name: "caseflow", version: "1.0.0" },
            });
          case "tools/list":
            return reply(msg.id, { tools: TOOLS });
          case "tools/call": {
            const { name, arguments: args = {} } = (msg.params ?? {}) as { name?: string; arguments?: Record<string, unknown> };
            if (name === "recall_knowledge") {
              const { results } = await hub.searchKnowledge(String(args.query ?? ""), Number(args.k ?? 5) || 5);
              const text = results.length
                ? results.map((r) =>
                    `## ${r.title} (${r.handler_id}, ${r.banked_at.slice(0, 10)})\n` +
                    `Lesson: ${r.lesson || "(none)"}\n` +
                    `Outcome: ${Object.entries(r.fields).map(([k, f]) => `${k}=${String(f.value)}`).join(", ")}`).join("\n\n")
                : "No matching knowledge.";
              return reply(msg.id, { content: [{ type: "text", text }] });
            }
            if (name === "get_case") {
              const item = await hub.item(String(args.id ?? ""));
              return reply(msg.id, { content: [{ type: "text", text: JSON.stringify(item, null, 2) }] });
            }
            return replyError(msg.id, -32602, `unknown tool: ${String(name)}`);
          }
          case "ping":
            return reply(msg.id, {});
          default:
            return replyError(msg.id, -32601, `unknown method: ${String(msg.method)}`);
        }
      } catch (e) {
        return replyError(msg.id, -32000, (e as Error).message);
      }
    })();
  });
}
