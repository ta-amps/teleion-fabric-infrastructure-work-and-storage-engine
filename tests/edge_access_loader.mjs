// Minimal Cloudflare DurableObject class for deterministic Node SQLite tests.
import { register } from "node:module";
const source =
  "export class DurableObject { constructor(ctx, env) { this.ctx=ctx; this.env=env; } }";
const target = "data:text/javascript," + encodeURIComponent(source);
register(
  "data:text/javascript," +
    encodeURIComponent(`export async function resolve(specifier, context, nextResolve) {
  if (specifier === 'cloudflare:workers') return {shortCircuit:true,url:${JSON.stringify(target)}};
  return nextResolve(specifier,context);
}`),
);
