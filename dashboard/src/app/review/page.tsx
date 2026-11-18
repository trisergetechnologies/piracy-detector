"use client";

import { useEffect, useState } from "react";
import { getReviewQueue, submitReview } from "@/lib/api";
import type { DetectedVideo } from "@/lib/types";
import {
  Button,
  Chip,
  Alert,
  Tabs,
  Tab,
  TextField,
} from "@mui/material";
import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import toast from "react-hot-toast";

export default function ReviewPage() {
  const [status, setStatus] = useState<"pending" | "confirmed" | "false_positive">("pending");
  const [items, setItems] = useState<DetectedVideo[]>([]);
  const [loading, setLoading] = useState(true);
  const [entityFilter, setEntityFilter] = useState("");

  const refresh = () => {
    setLoading(true);
    getReviewQueue(status, entityFilter.trim() || undefined)
      .then((r) => setItems(r.items || []))
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const decide = async (
    item: DetectedVideo,
    decision: "confirmed" | "false_positive" | "pending"
  ) => {
    if (!item.scan_id || !item.video_id) {
      toast.error("Missing scan_id / video_id");
      return;
    }
    try {
      await submitReview({
        scan_id: item.scan_id,
        video_id: item.video_id,
        decision,
      });
      toast.success(decision === "confirmed" ? "Confirmed for takedown" : "Marked false positive");
      refresh();
    } catch (e: any) {
      toast.error(e?.response?.data?.error || "Review failed");
    }
  };

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Review Queue</h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Confirm real piracy / defamation or reject false positives before DMCA packs
        </p>
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <TextField
          label="Filter by entity"
          size="small"
          value={entityFilter}
          onChange={(e) => setEntityFilter(e.target.value)}
          className="sm:min-w-[240px]"
        />
        <Button variant="outlined" onClick={refresh}>
          Apply
        </Button>
      </div>

      <Tabs
        value={status}
        onChange={(_, v) => setStatus(v)}
        textColor="primary"
        indicatorColor="primary"
      >
        <Tab value="pending" label="Pending" />
        <Tab value="confirmed" label="Confirmed" />
        <Tab value="false_positive" label="False positives" />
      </Tabs>

      {loading && <Alert severity="info">Loading…</Alert>}
      {!loading && items.length === 0 && (
        <Alert severity="info">No items in this queue.</Alert>
      )}

      <div className="space-y-3">
        {items.map((item) => (
          <div key={`${item.scan_id}-${item.video_id}`} className="card space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <Chip label={item.platform || "youtube"} size="small" />
              <Chip
                label={item.classification}
                size="small"
                color={item.classification === "PIRATED" ? "secondary" : "error"}
              />
              <Chip label={`${item.confidence}%`} size="small" variant="outlined" />
              {item.fingerprint_match?.is_match && (
                <Chip
                  label={`FP ${(item.fingerprint_match.similarity * 100).toFixed(0)}%`}
                  size="small"
                  color="success"
                />
              )}
            </div>
            <h3 className="text-sm font-semibold">{item.title}</h3>
            <p className="text-xs text-gray-500">
              {item.channel_title} · scan {item.scan_id}
            </p>
            {item.reason && (
              <p className="text-xs text-gray-600 dark:text-gray-300">{item.reason}</p>
            )}
            <div className="flex flex-wrap gap-2">
              {item.url && (
                <Button
                  size="small"
                  href={item.url}
                  target="_blank"
                  startIcon={<OpenInNewIcon />}
                >
                  Open
                </Button>
              )}
              {status === "pending" && (
                <>
                  <Button
                    size="small"
                    variant="contained"
                    color="success"
                    startIcon={<CheckIcon />}
                    onClick={() => decide(item, "confirmed")}
                  >
                    Confirm
                  </Button>
                  <Button
                    size="small"
                    variant="outlined"
                    color="error"
                    startIcon={<CloseIcon />}
                    onClick={() => decide(item, "false_positive")}
                  >
                    False positive
                  </Button>
                </>
              )}
              {status !== "pending" && (
                <Button
                  size="small"
                  onClick={() => decide(item, "pending")}
                >
                  Reset to pending
                </Button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
