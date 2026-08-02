import { ConfigureBoundary } from "../auth/ConfigureBoundary";
import { ConfigurePage } from "./ConfigurePage";

export function ConfigureRoute() {
  return (
    <ConfigureBoundary>
      <ConfigurePage />
    </ConfigureBoundary>
  );
}
