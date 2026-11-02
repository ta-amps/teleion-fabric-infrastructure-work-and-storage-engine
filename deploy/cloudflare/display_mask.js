// Display-time masking for pages that render stored data (shared banned list, data_rendering rule). The patterns
// come from the BANNED_TEXT var, which `make deploy` sets from the shared list at deploy time; no pattern is kept in
// this repository. Without the var nothing is masked. Stored records are never altered.
export function displayMask(env) {
  let spec = null;
  try {
    spec = env && env.BANNED_TEXT ? JSON.parse(env.BANNED_TEXT) : null;
  } catch {
    spec = null;
  }
  const patterns = spec ? [...(spec.always || []), ...(spec.visible || [])].map((p) => new RegExp(p, "gi")) : [];
  if (!patterns.length) return (value) => value;
  return (value) => {
    if (value === null || value === undefined) return value;
    let text = String(value);
    for (const pattern of patterns) text = text.replace(pattern, "\u2026");
    return text;
  };
}
