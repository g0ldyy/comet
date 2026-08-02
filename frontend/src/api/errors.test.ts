import { describe, expect, it } from "vitest";
import { ApiClientError } from "./client";
import { apiErrorReason, apiErrorSummary, apiValidationErrors } from "./errors";

const error = new ApiClientError(new Response(null, { status: 422 }), {
  error: {
    code: "validation_failed",
    details: [
      {
        location: ["pool", "display_name"],
        message: "String should have at least 1 character",
        type: "string_too_short",
      },
      { location: ["pool", "description"], type: "value_error" },
    ],
    message: "The request did not pass validation.",
    request_id: "request-1",
  },
});

describe("API error presentation", () => {
  it("uses the precise validation reason", () => {
    expect(apiErrorReason(error)).toBe("String should have at least 1 character");
  });

  it("associates every structured validation error with its leaf field", () => {
    expect(apiValidationErrors(error, "Invalid value")).toEqual({
      description: "Invalid value",
      display_name: "String should have at least 1 character",
    });
  });

  it("keeps the code and request identifier available for support", () => {
    expect(apiErrorSummary(error, "Failed", (id) => `Request ID: ${id}`)).toBe(
      "String should have at least 1 character · validation_failed · Request ID: request-1",
    );
  });
});
