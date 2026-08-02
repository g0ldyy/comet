export interface SearchDocument {
  detail: string;
  id: string;
  label: string;
  search: string;
  targetLabel: string;
  to: string;
}

export interface SearchPage {
  detail: string;
  scopes: readonly string[];
  to: string;
}

export function normalizeSearch(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[^\p{Letter}\p{Number}]+/gu, " ")
    .toLocaleLowerCase()
    .trim();
}

function strings(value: unknown, path: string[], output: Array<[string, string]>) {
  if (typeof value === "string") {
    output.push([path.join(" "), value]);
    return;
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) return;
  for (const [key, child] of Object.entries(value)) strings(child, [...path, key], output);
}

export function translationDocuments(
  resources: Record<string, unknown>,
  pages: readonly SearchPage[],
): SearchDocument[] {
  const documents: SearchDocument[] = [];
  for (const page of pages) {
    const entries: Array<[string, string]> = [];
    for (const scope of page.scopes) strings(resources[scope], [scope], entries);
    const labels = new Set<string>();
    for (const [key, targetLabel] of entries) {
      const label = targetLabel.replace(/\{\{[^}]+\}\}/g, "…");
      const normalizedLabel = normalizeSearch(label);
      if (normalizedLabel === "" || labels.has(normalizedLabel)) continue;
      labels.add(normalizedLabel);
      documents.push({
        detail: page.detail,
        id: `content-${page.to}-${key}`,
        label,
        search: normalizeSearch(`${label} ${key} ${page.detail}`),
        targetLabel,
        to: page.to,
      });
    }
  }
  return documents;
}

export function searchScore(search: string, label: string, rawQuery: string): number {
  const query = normalizeSearch(rawQuery);
  if (query === "") return 0;
  const normalizedLabel = normalizeSearch(label);
  if (normalizedLabel === query) return 100;
  if (normalizedLabel.startsWith(query)) return 90;
  if (normalizedLabel.includes(query)) return 80;
  const terms = query.split(" ");
  if (terms.every((term) => search.includes(term))) return 65;
  if (terms.length !== 1 || query.length < 3) return 0;
  let position = 0;
  for (const character of search) {
    if (character === query[position]) position += 1;
    if (position === query.length) return 30;
  }
  return 0;
}
