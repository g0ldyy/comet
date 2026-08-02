import { describe, expect, it } from "vitest";
import { normalizeSearch, searchScore, translationDocuments } from "./admin-search";

describe("admin search index", () => {
  it("indexes content from every declared page scope", () => {
    const documents = translationDocuments(
      {
        analytics: { inventory: { title: "Torrent candidates" } },
        system: { maintenance: { title: "Data retention" } },
      },
      [
        { detail: "Analytics", scopes: ["analytics"], to: "/admin/analytics" },
        { detail: "System", scopes: ["system"], to: "/admin/system" },
      ],
    );

    expect(documents).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ label: "Torrent candidates", to: "/admin/analytics" }),
        expect.objectContaining({ label: "Data retention", to: "/admin/system" }),
      ]),
    );
  });

  it("matches accents, identifiers, multiple terms and restrained fuzzy input", () => {
    expect(normalizeSearch("Débrid_HTTPClient")).toBe("debrid http client");
    expect(searchScore("http client limit settings", "HTTP_CLIENT_LIMIT", "client limit")).toBe(80);
    expect(searchScore("torrent candidates analytics", "Torrent candidates", "torent")).toBe(30);
    expect(searchScore("torrent candidates analytics", "Torrent candidates", "proxy queue")).toBe(
      0,
    );
  });
});
