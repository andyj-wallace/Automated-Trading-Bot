import type { JsonSchema } from "../../types/api";

export function defaultsFromSchema(schema: JsonSchema): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, prop] of Object.entries(schema.properties ?? {})) {
    if (prop.default !== undefined && prop.default !== null) out[key] = prop.default;
  }
  return out;
}
