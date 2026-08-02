import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MultiSelect } from "./MultiSelect";

const options = [
  { label: "English", value: "en" },
  { label: "Français", value: "fr" },
  { label: "Deutsch", value: "de" },
] as const;

describe("MultiSelect", () => {
  it("keeps every selection visible and removes a badge directly", () => {
    const onChange = vi.fn();
    render(
      <MultiSelect
        emptyLabel="None"
        label="Languages"
        onChange={onChange}
        options={options}
        removeLabel={(label) => `Remove ${label}`}
        searchLabel="Search languages"
        selected={["en", "fr", "de"]}
      />,
    );

    expect(screen.getByText("English")).toBeVisible();
    expect(screen.getByText("Français")).toBeVisible();
    expect(screen.getByText("Deutsch")).toBeVisible();
    expect(screen.queryByText("3 selected")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Remove Français" }));
    expect(onChange).toHaveBeenCalledWith(["en", "de"]);
  });
});
