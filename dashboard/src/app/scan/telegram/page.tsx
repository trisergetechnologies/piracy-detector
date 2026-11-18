"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { useRouter } from "next/navigation";
import {
  startTelegramScan,
  getScanStatus,
  getTelegramSeeds,
  getHistory,
} from "@/lib/api";
import type { TelegramScanParams } from "@/lib/types";
import {
  Button,
  TextField,
  Switch,
  FormControlLabel,
  CircularProgress,
  Alert,
  LinearProgress,
  MenuItem,
} from "@mui/material";
import SendIcon from "@mui/icons-material/Send";
import toast from "react-hot-toast";

export default function TelegramScanPage() {
  const router = useRouter();
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState("");
  const [progress, setProgress] = useState("");
  const [scanId, setScanId] = useState("");
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const [entityName, setEntityName] = useState("");
  const [telegramSeeds, setTelegramSeeds] = useState("");
  const [officialChannelIds, setOfficialChannelIds] = useState("");
  const [officialPlaylistIds, setOfficialPlaylistIds] = useState("");
  const [bridgeScanId, setBridgeScanId] = useState("");
  const [enableFingerprinting, setEnableFingerprinting] = useState(true);
  const [minConfidence, setMinConfidence] = useState(50);
  const [ytScans, setYtScans] = useState<{ scan_id: string; entity_name: string }[]>([]);
  const [storedSeedCount, setStoredSeedCount] = useState(0);

  const stopPolling = useCallback(() => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

  useEffect(() => {
    getHistory(30)
      .then((res) => {
        const scans = (res.scans || [])
          .filter((s) => s.scan_type !== "telegram")
          .map((s) => ({ scan_id: s.scan_id, entity_name: s.entity_name }));
        setYtScans(scans);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!entityName.trim()) {
      setStoredSeedCount(0);
      return;
    }
    const t = setTimeout(() => {
      getTelegramSeeds(entityName.trim())
        .then((r) => setStoredSeedCount(r.seeds?.length || 0))
        .catch(() => setStoredSeedCount(0));
    }, 400);
    return () => clearTimeout(t);
  }, [entityName]);

  const splitIds = (raw: string): string[] =>
    raw
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter(Boolean);

  const pollStatus = useCallback(
    (id: string) => {
      pollingRef.current = setInterval(async () => {
        try {
          const status = await getScanStatus(id);
          setProgress(status.progress || "Processing...");
          if (status.status === "completed") {
            stopPolling();
            setScanning(false);
            const s = status.summary;
            if (s?.error && !s?.telegram) {
              setError(s.error);
              toast.error("Scan failed: " + s.error);
            } else {
              const pirated = s?.pirated_found ?? 0;
              const seeds = s?.telegram?.seeds ?? 0;
              toast.success(
                `Telegram scan done — ${pirated} hits, ${seeds} seeds`
              );
              router.push(`/scan/${id}`);
            }
          }
        } catch {
          // keep polling
        }
      }, 4000);
    },
    [router, stopPolling]
  );

  const handleScan = async () => {
    if (!entityName.trim()) {
      setError("Entity name is required");
      return;
    }
    setError("");
    setScanning(true);
    setProgress("Starting Telegram scan...");

    const params: TelegramScanParams = {
      entity_name: entityName.trim(),
      entity_type: "educator",
      telegram_seeds: splitIds(telegramSeeds),
      enable_fingerprinting: enableFingerprinting,
      min_confidence: minConfidence,
      include_stored_seeds: true,
    };
    const officialChannels = splitIds(officialChannelIds);
    const officialPlaylists = splitIds(officialPlaylistIds);
    if (officialChannels.length) params.official_channel_ids = officialChannels;
    if (officialPlaylists.length) params.official_playlist_ids = officialPlaylists;
    if (bridgeScanId) params.bridge_youtube_scan_id = bridgeScanId;

    try {
      const result = await startTelegramScan(params);
      if (result.ok && result.scan_id) {
        setScanId(result.scan_id);
        setProgress("Telegram scan queued...");
        pollStatus(result.scan_id);
      } else {
        setError(result.error || "Failed to start Telegram scan");
        setScanning(false);
      }
    } catch (err: any) {
      const msg =
        err?.response?.data?.error || err?.message || "Connection failed";
      setError(msg);
      toast.error(msg);
      setScanning(false);
    }
  };

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Telegram Scan</h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Phase C — sibling platform to YouTube. Harvests{" "}
          <code className="text-xs">t.me</code> seeds from YouTube bridges,
          scans channels for dumps, and reuses audio fingerprints for proof.
        </p>
      </div>

      <div className="card max-w-2xl space-y-6">
        <Alert severity="info">
          Full channel reading needs Telethon login (
          <code className="text-xs">python scripts/telegram_login.py</code>
          ). Without it, you still get seed harvest from YouTube bridges.
        </Alert>

        <TextField
          label="Entity Name"
          placeholder="e.g. Physics Wallah"
          fullWidth
          required
          value={entityName}
          onChange={(e) => setEntityName(e.target.value)}
          disabled={scanning}
          size="small"
          helperText={
            storedSeedCount > 0
              ? `${storedSeedCount} stored Telegram seeds will be included`
              : "Stored seeds from past YouTube piracy scans are auto-included"
          }
        />

        <TextField
          label="Telegram seeds (manual)"
          placeholder={"@channel\nhttps://t.me/some_dump\nhttps://t.me/+inviteHash"}
          fullWidth
          multiline
          minRows={3}
          value={telegramSeeds}
          onChange={(e) => setTelegramSeeds(e.target.value)}
          disabled={scanning}
          size="small"
        />

        <TextField
          label="Bridge from YouTube scan"
          select
          fullWidth
          value={bridgeScanId}
          onChange={(e) => setBridgeScanId(e.target.value)}
          disabled={scanning}
          size="small"
          helperText="Reuse t.me links found on a previous YouTube piracy scan"
        >
          <MenuItem value="">None</MenuItem>
          {ytScans.map((s) => (
            <MenuItem key={s.scan_id} value={s.scan_id}>
              {s.entity_name} — {s.scan_id}
            </MenuItem>
          ))}
        </TextField>

        <div className="space-y-3 rounded-lg border border-dashed border-gray-300 p-4 dark:border-gray-600">
          <p className="text-sm font-semibold">Official catalog (fingerprint proof)</p>
          <TextField
            label="Official Channel IDs"
            placeholder="UCxxxxxxxx"
            fullWidth
            multiline
            minRows={2}
            value={officialChannelIds}
            onChange={(e) => setOfficialChannelIds(e.target.value)}
            disabled={scanning}
            size="small"
          />
          <TextField
            label="Official Playlist IDs"
            placeholder="PLxxxxxxxx"
            fullWidth
            multiline
            minRows={2}
            value={officialPlaylistIds}
            onChange={(e) => setOfficialPlaylistIds(e.target.value)}
            disabled={scanning}
            size="small"
          />
        </div>

        <FormControlLabel
          control={
            <Switch
              checked={enableFingerprinting}
              onChange={(e) => setEnableFingerprinting(e.target.checked)}
              disabled={scanning}
            />
          }
          label="Fingerprint Telegram media vs official catalog"
        />

        <TextField
          label="Min Confidence"
          type="number"
          fullWidth
          value={minConfidence}
          onChange={(e) => setMinConfidence(Number(e.target.value))}
          disabled={scanning}
          size="small"
          inputProps={{ min: 0, max: 100 }}
        />

        {error && (
          <Alert severity="error" onClose={() => setError("")}>
            {error}
          </Alert>
        )}

        <Button
          variant="contained"
          size="large"
          fullWidth
          onClick={handleScan}
          disabled={scanning || !entityName.trim()}
          startIcon={
            scanning ? (
              <CircularProgress size={18} color="inherit" />
            ) : (
              <SendIcon />
            )
          }
        >
          {scanning ? "Scanning Telegram..." : "Start Telegram Scan"}
        </Button>

        {scanning && (
          <div className="space-y-3">
            <LinearProgress />
            <div className="rounded-lg bg-blue-50 px-4 py-3 dark:bg-blue-950">
              <p className="text-sm font-medium text-blue-800 dark:text-blue-200">
                {progress}
              </p>
              {scanId && (
                <p className="mt-1 text-xs text-blue-600 dark:text-blue-400">
                  Scan ID: {scanId}
                </p>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
