type ParsedString = {
  readonly value: string;
  readonly end: number;
  readonly complete: boolean;
};

export type ProviderTextProjection = {
  readonly text: string;
  readonly complete: boolean;
};

function skipWhitespace(source: string, start: number): number {
  let cursor = start;
  while (cursor < source.length && /\s/.test(source[cursor] ?? "")) cursor += 1;
  return cursor;
}

function parseJsonString(source: string, openingQuote: number): ParsedString {
  let cursor = openingQuote + 1;
  let value = "";
  while (cursor < source.length) {
    const character = source[cursor] ?? "";
    if (character === '"') {
      return { value, end: cursor + 1, complete: true };
    }
    if (character !== "\\") {
      value += character;
      cursor += 1;
      continue;
    }
    const escape = source[cursor + 1];
    if (escape === undefined) break;
    const simpleEscapes: Readonly<Record<string, string>> = {
      '"': '"',
      "\\": "\\",
      "/": "/",
      b: "\b",
      f: "\f",
      n: "\n",
      r: "\r",
      t: "\t",
    };
    const simple = simpleEscapes[escape];
    if (simple !== undefined) {
      value += simple;
      cursor += 2;
      continue;
    }
    if (escape === "u") {
      const hexadecimal = source.slice(cursor + 2, cursor + 6);
      if (hexadecimal.length < 4) break;
      if (!/^[0-9a-fA-F]{4}$/.test(hexadecimal)) {
        cursor += 2;
        continue;
      }
      value += String.fromCharCode(Number.parseInt(hexadecimal, 16));
      cursor += 6;
      continue;
    }
    cursor += 2;
  }
  return { value, end: cursor, complete: false };
}

/**
 * Projects the top-level `text` field from a partially streamed structured JSON
 * response. Tool-call JSON never becomes visible chat content.
 */
export function projectProviderText(source: string): ProviderTextProjection | undefined {
  let cursor = 0;
  let depth = 0;
  while (cursor < source.length) {
    const character = source[cursor] ?? "";
    if (character === "{") {
      depth += 1;
      cursor += 1;
      continue;
    }
    if (character === "}") {
      depth = Math.max(0, depth - 1);
      cursor += 1;
      continue;
    }
    if (character !== '"') {
      cursor += 1;
      continue;
    }
    const property = parseJsonString(source, cursor);
    if (!property.complete) return undefined;
    cursor = property.end;
    if (depth !== 1 || property.value !== "text") continue;
    cursor = skipWhitespace(source, cursor);
    if (source[cursor] !== ":") continue;
    cursor = skipWhitespace(source, cursor + 1);
    if (source[cursor] !== '"') return undefined;
    const text = parseJsonString(source, cursor);
    return { text: text.value, complete: text.complete };
  }
  return undefined;
}
