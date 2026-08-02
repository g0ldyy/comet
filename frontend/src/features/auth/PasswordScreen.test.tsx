import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { ApiClientError } from "../../api/client";
import { initializeI18n } from "../../i18n";
import { PasswordScreen } from "./PasswordScreen";

beforeAll(() => initializeI18n());

describe("PasswordScreen", () => {
  it("submits credentials and reports a rejected login", async () => {
    const apiError = new ApiClientError(new Response(null, { status: 401 }), {
      error: {
        code: "authentication_required",
        message: "Authentication required",
        request_id: "request-7",
      },
    });
    const submit = vi.fn().mockRejectedValue(apiError);
    render(<PasswordScreen eyebrow="Operator" onSubmit={submit} title="Sign in" />);

    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "component-only-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith("component-only-secret"));
    expect(await screen.findByRole("alert")).toHaveTextContent("request-7");
  });
});
