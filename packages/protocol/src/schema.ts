/**
 * Minimal declarative output-schema language used in handler manifests.
 * Deliberately tiny: string | number | boolean | enum. The hub validates
 * every stage result against these SERVER-SIDE (runtimes are untrusted for
 * validation); the runtime also validates client-side for fast feedback.
 *
 * If a real handler needs more (arrays, nesting), replace this module with
 * full JSON Schema — keep the surface small until then.
 */
export type FieldSpec =
  | "string"
  | "number"
  | "boolean"
  | { enum: string[] }
  | { type: "string" | "number" | "boolean"; optional?: boolean };

export type OutputSchema = Record<string, FieldSpec>;

export interface ValidationError {
  field: string;
  message: string;
}

export function validateAgainstSchema(
  schema: OutputSchema,
  value: unknown,
): ValidationError[] {
  const errors: ValidationError[] = [];
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return [{ field: "$", message: "result must be a JSON object" }];
  }
  const obj = value as Record<string, unknown>;
  for (const [field, spec] of Object.entries(schema)) {
    const v = obj[field];
    const optional = typeof spec === "object" && "type" in spec && spec.optional === true;
    if (v === undefined || v === null) {
      if (!optional) errors.push({ field, message: "missing required field" });
      continue;
    }
    if (typeof spec === "string" || (typeof spec === "object" && "type" in spec)) {
      const t = typeof spec === "string" ? spec : spec.type;
      if (typeof v !== t) errors.push({ field, message: `expected ${t}, got ${typeof v}` });
    } else if ("enum" in spec) {
      if (typeof v !== "string" || !spec.enum.includes(v)) {
        errors.push({ field, message: `expected one of [${spec.enum.join(", ")}], got ${JSON.stringify(v)}` });
      }
    }
  }
  return errors;
}
