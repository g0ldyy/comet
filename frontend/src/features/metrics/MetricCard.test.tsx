import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MetricCard } from "./MetricCard";

describe("MetricCard", () => {
  it("renders a real signal only when history has a direction", () => {
    const { container, rerender } = render(
      <MetricCard label="Requests" signal={[4, 8, 6]} value="8/s" />,
    );

    expect(container.querySelector(".metric-card__signal polyline")).toHaveAttribute(
      "points",
      "0,28 50,4 100,16",
    );

    rerender(<MetricCard label="Requests" signal={[4]} value="4/s" />);
    expect(container.querySelector(".metric-card__signal")).not.toBeInTheDocument();
  });
});
