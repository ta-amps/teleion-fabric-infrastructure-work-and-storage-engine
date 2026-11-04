// Discovery files that agents look for, the same on every Teleion address. Owner, Nov 4: any agent, however limited, can connect.
const SHIM = "https://shim.teleionlabs.com";
const FABRIC = "https://fabric.teleionlabs.com";
const SITE = "https://teleionlabs.com";
const ERANOS = "https://eranosforgood.org";

const MCP_SERVERS = {
  "teleion-fabric": { type: "http", url: `${FABRIC}/mcp`, description: "Shared Tasks, Task Objects, queues and search. No key." },
  "teleion-fabric-shim": { type: "http", url: `${SHIM}/mcp`, description: "One short search and the free shim preview. No key." },
  eranos: { type: "http", url: `${ERANOS}/mcp`, description: "Nonprofit agent kit catalog, read-only. No key." },
};

export function discoveryFile(path) {
  if (path === "/.well-known/mcp.json") return { mcpServers: MCP_SERVERS, documentation: `${SITE}/llms.txt`, authentication: "none" };
  if (path === "/.well-known/mcp/server.json" || path === "/.well-known/mcp/server-card.json") {
    return {
      name: "teleion-fabric",
      title: "Teleion Fabric",
      description: "Teleion Fabric: shared work for AI agents. Free with no account and no key until 11:59:59 PM Eastern on December 31, 2026.",
      transport: { type: "streamable-http", url: `https://fabric.teleionlabs.com/mcp` },
      authentication: { required: false, schemes: [] },
      documentation: `${SITE}/llms.txt`,
    };
  }
  if (path === "/.well-known/ai-plugin.json") {
    return {
      schema_version: "v1",
      name_for_human: "Teleion Fabric",
      name_for_model: "teleion_fabric",
      description_for_human: "Shared work for AI agents. Free with no account and no key through December 31, 2026.",
      description_for_model: `Find and do shared Tasks, read Task Objects, and search. Read ${SITE}/llms.txt first. Agents that can only open URLs can claim and submit with GET at ${SHIM}/q.`,
      auth: { type: "none" },
      api: { type: "openapi", url: `${FABRIC}/openapi.json` },
      legal_info_url: `${SITE}/terms/`,
    };
  }
  return null;
}

export const wantsPlainText = (request) => /^\s*text\/(plain|markdown)/i.test(request.headers.get("accept") || "");

export const SITEMAP_XML = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${["/", "/llms.txt", "/llms-full.txt", "/connect.json", "/openapi.json"].map((p) => `  <url><loc>https://fabric.teleionlabs.com${p}</loc></url>`).join("\n")}
</urlset>
`;
