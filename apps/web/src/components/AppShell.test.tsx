import { FluentProvider } from "@fluentui/react-components";
import { render, screen } from "@testing-library/react";
import { lightTheme } from "../theme";
import { LicenceBanner } from "./AppShell";

const show = (licence: { state: string; days_left?: number | null }) =>
  render(
    <FluentProvider theme={lightTheme}>
      <LicenceBanner licence={licence} />
    </FluentProvider>,
  );

describe("LicenceBanner", () => {
  it("is silent while the licence is fine or not enforced", () => {
    const { container } = show({ state: "valid", days_left: 200 });
    expect(container.textContent).toBe("");
    show({ state: "unenforced" });
    expect(screen.queryByText(/licence/i)).not.toBeInTheDocument();
  });

  it("warns ahead of expiry and during the grace period", () => {
    show({ state: "valid", days_left: 12 });
    expect(screen.getByText("The licence expires in 12 day(s).")).toBeInTheDocument();
    show({ state: "grace", days_left: -3 });
    expect(screen.getByText(/expired 3 day\(s\) ago/)).toBeInTheDocument();
  });

  it("says what still works once runs are stopped", () => {
    show({ state: "expired", days_left: -40 });
    expect(screen.getByText(/all existing work stays readable/)).toBeInTheDocument();
  });
});
