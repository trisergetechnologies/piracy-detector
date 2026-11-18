"use client";

import { Chip } from "@mui/material";

const SEVERITY_CONFIG: Record<
  string,
  { color: "error" | "warning" | "info" | "success"; label: string }
> = {
  critical: { color: "error", label: "Critical" },
  high: { color: "error", label: "High" },
  medium: { color: "warning", label: "Medium" },
  low: { color: "info", label: "Low" },
};

export default function SeverityBadge({ severity }: { severity: string }) {
  const config = SEVERITY_CONFIG[severity] ?? {
    color: "info" as const,
    label: severity || "Unknown",
  };
  return <Chip label={config.label} color={config.color} size="small" />;
}
