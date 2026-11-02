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

// The same patterns as a test, for a contributed value a page leaves out instead of masking it (the activity map
// shows a model id that names a banned word as "another model"). Built on displayMask, so BANNED_TEXT has one
// reader. An id such as muse-spark-1.3 is also read as the words a page would show ("muse spark 1 3").
export function displayTest(env) {
  const mask = displayMask(env);
  const known = new Map();
  return (value) => {
    const text = String(value);
    if (!known.has(text)) known.set(text, [text, text.replace(/[_./-]+/g, " ")].some((t) => mask(t) !== t));
    return known.get(text);
  };
}
