import { AdminShell } from "../../app/AdminShell";
import { AdminBoundary } from "../auth/AdminBoundary";

export function AdminRoute() {
  return (
    <AdminBoundary>
      <AdminShell />
    </AdminBoundary>
  );
}
