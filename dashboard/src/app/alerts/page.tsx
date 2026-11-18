"use client";

import { useEffect, useState } from "react";
import { listAlerts, markAlertsRead } from "@/lib/api";
import type { AlertRecord } from "@/lib/types";
import { Button, Chip, Alert as MuiAlert } from "@mui/material";
import DoneAllIcon from "@mui/icons-material/DoneAll";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import Link from "next/link";
import toast from "react-hot-toast";

export default function AlertsPage() {
  const [alerts, setAlerts] = useState<AlertRecord[]>([]);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [loading, setLoading] = useState(true);

  const refresh = () =>
    listAlerts(unreadOnly)
      .then((r) => setAlerts(r.alerts || []))
      .catch(() => {})
      .finally(() => setLoading(false));

  useEffect(() => {
    setLoading(true);
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unreadOnly]);

  const markAll = async () => {
    await markAlertsRead({ all: true });
    toast.success("All alerts marked read");
    refresh();
  };

  return (
    <div className="space-y-8">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Alerts</h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            New hits from scans and continuous monitors
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            size="small"
            variant={unreadOnly ? "contained" : "outlined"}
            onClick={() => setUnreadOnly((v) => !v)}
          >
            {unreadOnly ? "Unread only" : "Show all"}
          </Button>
          <Button size="small" startIcon={<DoneAllIcon />} onClick={markAll}>
            Mark all read
          </Button>
        </div>
      </div>

      {loading && <MuiAlert severity="info">Loading alerts…</MuiAlert>}
      {!loading && alerts.length === 0 && (
        <MuiAlert severity="info">No alerts yet. Run a scan or create a monitor.</MuiAlert>
      )}

      <div className="space-y-3">
        {alerts.map((a) => (
          <div
            key={a.alert_id}
            className={`card space-y-2 ${a.read ? "opacity-70" : "border-l-4 border-l-blue-500"}`}
          >
            <div className="flex flex-wrap items-center gap-2">
              <Chip label={a.type} size="small" />
              {a.platform && <Chip label={a.platform} size="small" variant="outlined" />}
              {a.severity && <Chip label={a.severity} size="small" color="warning" />}
              {!a.read && <Chip label="new" size="small" color="primary" />}
            </div>
            <p className="text-sm font-medium">{a.message || a.title}</p>
            <p className="text-xs text-gray-500">
              {a.entity_name} · {new Date(a.created_at).toLocaleString()}
            </p>
            <div className="flex flex-wrap gap-2">
              {a.scan_id && (
                <Link href={`/scan/${a.scan_id}`}>
                  <Button size="small">Open scan</Button>
                </Link>
              )}
              {a.url && (
                <Button
                  size="small"
                  href={a.url}
                  target="_blank"
                  startIcon={<OpenInNewIcon />}
                >
                  Open item
                </Button>
              )}
              {!a.read && (
                <Button
                  size="small"
                  onClick={async () => {
                    await markAlertsRead({ alert_id: a.alert_id, read: true });
                    refresh();
                  }}
                >
                  Mark read
                </Button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
