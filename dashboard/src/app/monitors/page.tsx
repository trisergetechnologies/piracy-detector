"use client";

import { useEffect, useState } from "react";
import {
  listMonitors,
  createMonitor,
  updateMonitor,
  deleteMonitor,
  runMonitorNow,
} from "@/lib/api";
import type { MonitorRecord } from "@/lib/types";
import {
  Button,
  TextField,
  Switch,
  FormControlLabel,
  Chip,
  Alert,
  MenuItem,
  CircularProgress,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import DeleteIcon from "@mui/icons-material/Delete";
import toast from "react-hot-toast";

export default function MonitorsPage() {
  const [monitors, setMonitors] = useState<MonitorRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [entityName, setEntityName] = useState("");
  const [platforms, setPlatforms] = useState("youtube");
  const [intervalHours, setIntervalHours] = useState(24);
  const [officialIds, setOfficialIds] = useState("");
  const [creating, setCreating] = useState(false);

  const refresh = () =>
    listMonitors()
      .then((r) => setMonitors(r.monitors || []))
      .catch(() => {})
      .finally(() => setLoading(false));

  useEffect(() => {
    refresh();
  }, []);

  const handleCreate = async () => {
    if (!entityName.trim()) {
      toast.error("Entity name required");
      return;
    }
    setCreating(true);
    try {
      const official_channel_ids = officialIds
        .split(/[\n,]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      await createMonitor({
        entity_name: entityName.trim(),
        platforms: platforms.split(",").map((p) => p.trim()),
        interval_hours: intervalHours,
        official_channel_ids,
        enable_fingerprinting: true,
        enable_deep_crawl: true,
      });
      toast.success("Monitor created");
      setEntityName("");
      setOfficialIds("");
      refresh();
    } catch (e: any) {
      toast.error(e?.response?.data?.error || "Failed to create monitor");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Monitors</h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Phase D — continuous re-scans that alert when new low-view copies appear
        </p>
      </div>

      <div className="card max-w-2xl space-y-4">
        <h2 className="text-sm font-semibold">Create monitor</h2>
        <TextField
          label="Entity name"
          size="small"
          fullWidth
          value={entityName}
          onChange={(e) => setEntityName(e.target.value)}
        />
        <TextField
          label="Platforms"
          select
          size="small"
          fullWidth
          value={platforms}
          onChange={(e) => setPlatforms(e.target.value)}
        >
          <MenuItem value="youtube">YouTube</MenuItem>
          <MenuItem value="telegram">Telegram</MenuItem>
          <MenuItem value="youtube,telegram">YouTube + Telegram</MenuItem>
        </TextField>
        <TextField
          label="Interval (hours)"
          type="number"
          size="small"
          fullWidth
          value={intervalHours}
          onChange={(e) => setIntervalHours(Number(e.target.value))}
        />
        <TextField
          label="Official channel IDs (for fingerprint proof)"
          size="small"
          fullWidth
          multiline
          minRows={2}
          value={officialIds}
          onChange={(e) => setOfficialIds(e.target.value)}
        />
        <Button
          variant="contained"
          startIcon={creating ? <CircularProgress size={16} color="inherit" /> : <AddIcon />}
          onClick={handleCreate}
          disabled={creating}
        >
          Create Monitor
        </Button>
      </div>

      <div className="space-y-3">
        {loading && <Alert severity="info">Loading monitors…</Alert>}
        {!loading && monitors.length === 0 && (
          <Alert severity="info">No monitors yet. Create one to watch an entity around the clock.</Alert>
        )}
        {monitors.map((m) => (
          <div key={m.monitor_id} className="card flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{m.entity_name}</span>
                {(m.platforms || []).map((p) => (
                  <Chip key={p} label={p} size="small" />
                ))}
                <Chip
                  label={m.enabled ? "enabled" : "paused"}
                  size="small"
                  color={m.enabled ? "success" : "default"}
                />
              </div>
              <p className="text-xs text-gray-500">
                Every {m.interval_hours}h · runs {m.run_count || 0} · next{" "}
                {m.next_run_at ? new Date(m.next_run_at).toLocaleString() : "—"}
              </p>
              {m.last_error && (
                <p className="text-xs text-red-500">{m.last_error}</p>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              <FormControlLabel
                control={
                  <Switch
                    checked={!!m.enabled}
                    onChange={async (e) => {
                      await updateMonitor(m.monitor_id, { enabled: e.target.checked });
                      refresh();
                    }}
                    size="small"
                  />
                }
                label="On"
              />
              <Button
                size="small"
                startIcon={<PlayArrowIcon />}
                onClick={async () => {
                  await runMonitorNow(m.monitor_id);
                  toast.success("Queued for next tick");
                  refresh();
                }}
              >
                Run
              </Button>
              <Button
                size="small"
                color="error"
                startIcon={<DeleteIcon />}
                onClick={async () => {
                  await deleteMonitor(m.monitor_id);
                  toast.success("Deleted");
                  refresh();
                }}
              >
                Delete
              </Button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
