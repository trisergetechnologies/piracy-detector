"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { useRouter } from "next/navigation";
import { startScan, getScanStatus } from "@/lib/api";
import type { ScanParams } from "@/lib/types";
import {
  Button,
  TextField,
  MenuItem,
  Slider,
  Switch,
  FormControlLabel,
  RadioGroup,
  Radio,
  FormLabel,
  CircularProgress,
  Alert,
  LinearProgress,
  Chip,
  Stack,
} from "@mui/material";
import SearchIcon from "@mui/icons-material/Search";
import toast from "react-hot-toast";

function formatDate(d: Date): string {
  return d.toISOString().split("T")[0];
}

const ENTITY_TYPES = [
  { value: "educator", label: "Educator" },
  { value: "politician", label: "Politician" },
  { value: "influencer", label: "Influencer" },
  { value: "generic", label: "Generic" },
];

export default function NewScanPage() {
  const router = useRouter();
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState("");
  const [progress, setProgress] = useState("");
  const [scanId, setScanId] = useState("");
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const [entityName, setEntityName] = useState("");
  const [entityType, setEntityType] = useState<ScanParams["entity_type"]>("generic");
  const [scanType, setScanType] = useState<ScanParams["scan_type"]>("both");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [maxResults, setMaxResults] = useState(200);
  const [minConfidence, setMinConfidence] = useState(60);
  const [analyzeThumbnails, setAnalyzeThumbnails] = useState(true);
  const [officialChannelIds, setOfficialChannelIds] = useState("");
  const [officialPlaylistIds, setOfficialPlaylistIds] = useState("");
  const [knownPirateChannels, setKnownPirateChannels] = useState("");
  const [enableDeepCrawl, setEnableDeepCrawl] = useState(true);
  const [enableFingerprinting, setEnableFingerprinting] = useState(true);

  const splitIds = (raw: string): string[] =>
    raw
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter(Boolean);

  const stopPolling = useCallback(() => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

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
            if (s?.error) {
              setError(s.error);
              toast.error("Scan failed: " + s.error);
            } else {
              toast.success(
                `Scan complete! Found ${s?.defamatory_found ?? 0} defamatory, ${s?.pirated_found ?? 0} pirated`
              );
              router.push(`/scan/${id}`);
            }
          }
        } catch {
          // Network blip -- keep polling
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
    setProgress("Starting scan...");

    const params: ScanParams = {
      entity_name: entityName.trim(),
      entity_type: entityType,
      scan_type: scanType,
      max_results: maxResults,
      min_confidence: minConfidence,
      analyze_thumbnails: analyzeThumbnails,
      enable_deep_crawl: enableDeepCrawl,
      enable_fingerprinting: enableFingerprinting,
    };
    if (dateFrom) params.date_from = dateFrom;
    if (dateTo) params.date_to = dateTo;
    const officialChannels = splitIds(officialChannelIds);
    const officialPlaylists = splitIds(officialPlaylistIds);
    const pirateChannels = splitIds(knownPirateChannels);
    if (officialChannels.length) params.official_channel_ids = officialChannels;
    if (officialPlaylists.length) params.official_playlist_ids = officialPlaylists;
    if (pirateChannels.length) params.known_channels_to_monitor = pirateChannels;

    try {
      const result = await startScan(params);
      if (result.ok && result.scan_id) {
        setScanId(result.scan_id);
        setProgress("Scan queued, discovering videos...");
        pollStatus(result.scan_id);
      } else {
        setError(result.error || "Failed to start scan");
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
        <h1 className="text-2xl font-bold tracking-tight">New Scan</h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Configure and run a content detection scan
        </p>
      </div>

      <div className="card max-w-2xl space-y-6">
        <TextField
          label="Entity Name"
          placeholder="e.g. John Doe, Physics Wallah"
          fullWidth
          required
          value={entityName}
          onChange={(e) => setEntityName(e.target.value)}
          disabled={scanning}
          size="small"
        />

        <TextField
          label="Entity Type"
          select
          fullWidth
          value={entityType}
          onChange={(e) =>
            setEntityType(e.target.value as ScanParams["entity_type"])
          }
          disabled={scanning}
          size="small"
        >
          {ENTITY_TYPES.map((t) => (
            <MenuItem key={t.value} value={t.value}>
              {t.label}
            </MenuItem>
          ))}
        </TextField>

        <div>
          <FormLabel className="!text-sm !font-semibold">Scan Type</FormLabel>
          <RadioGroup
            row
            value={scanType}
            onChange={(e) =>
              setScanType(e.target.value as ScanParams["scan_type"])
            }
          >
            <FormControlLabel
              value="both"
              control={<Radio size="small" />}
              label="Full Scan"
              disabled={scanning}
            />
            <FormControlLabel
              value="defamatory"
              control={<Radio size="small" />}
              label="Defamatory Only"
              disabled={scanning}
            />
            <FormControlLabel
              value="piracy"
              control={<Radio size="small" />}
              label="Piracy Only"
              disabled={scanning}
            />
          </RadioGroup>
        </div>

        <div>
          <label className="mb-2 block text-sm font-semibold text-gray-700 dark:text-gray-300">
            Date Range
          </label>
          <Stack direction="row" spacing={1} className="mb-3" flexWrap="wrap" useFlexGap>
            {[
              { label: "Last 7 days", days: 7 },
              { label: "Last 15 days", days: 15 },
              { label: "Last 30 days", days: 30 },
              { label: "All time", days: 0 },
            ].map((preset) => {
              const today = new Date();
              const from = preset.days > 0
                ? formatDate(new Date(today.getTime() - preset.days * 86400000))
                : "";
              const isActive =
                preset.days === 0
                  ? !dateFrom && !dateTo
                  : dateFrom === from && dateTo === formatDate(today);
              return (
                <Chip
                  key={preset.label}
                  label={preset.label}
                  clickable
                  color={isActive ? "primary" : "default"}
                  variant={isActive ? "filled" : "outlined"}
                  disabled={scanning}
                  onClick={() => {
                    if (preset.days === 0) {
                      setDateFrom("");
                      setDateTo("");
                    } else {
                      setDateFrom(from);
                      setDateTo(formatDate(today));
                    }
                  }}
                />
              );
            })}
          </Stack>
          <div className="grid gap-4 sm:grid-cols-2">
            <TextField
              label="Date From"
              type="date"
              fullWidth
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
              disabled={scanning}
              size="small"
              InputLabelProps={{ shrink: true }}
            />
            <TextField
              label="Date To"
              type="date"
              fullWidth
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
              disabled={scanning}
              size="small"
              InputLabelProps={{ shrink: true }}
            />
          </div>
        </div>

        <div>
          <label className="mb-2 block text-sm font-semibold text-gray-700 dark:text-gray-300">
            Max Results: {maxResults}
          </label>
          <Slider
            value={maxResults}
            onChange={(_, v) => setMaxResults(v as number)}
            min={10}
            max={500}
            step={10}
            disabled={scanning}
            valueLabelDisplay="auto"
          />
        </div>

        <div>
          <label className="mb-2 block text-sm font-semibold text-gray-700 dark:text-gray-300">
            Min Confidence: {minConfidence}%
          </label>
          <Slider
            value={minConfidence}
            onChange={(_, v) => setMinConfidence(v as number)}
            min={0}
            max={100}
            step={5}
            disabled={scanning}
            valueLabelDisplay="auto"
          />
        </div>

        <FormControlLabel
          control={
            <Switch
              checked={analyzeThumbnails}
              onChange={(e) => setAnalyzeThumbnails(e.target.checked)}
              disabled={scanning}
            />
          }
          label="Analyze Thumbnails (uses GPT-4o Vision)"
        />

        <FormControlLabel
          control={
            <Switch
              checked={enableDeepCrawl}
              onChange={(e) => setEnableDeepCrawl(e.target.checked)}
              disabled={scanning}
            />
          }
          label="Deep-crawl suspicious channels (finds low-view copies)"
        />

        <FormControlLabel
          control={
            <Switch
              checked={enableFingerprinting}
              onChange={(e) => setEnableFingerprinting(e.target.checked)}
              disabled={scanning}
            />
          }
          label="Audio fingerprinting vs official catalog (Phase B proof)"
        />

        <div className="space-y-3 rounded-lg border border-dashed border-gray-300 p-4 dark:border-gray-600">
          <div>
            <p className="text-sm font-semibold text-gray-800 dark:text-gray-200">
              Phase A/B – Official catalog (required for fingerprint proof)
            </p>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Paste the creator&apos;s official YouTube channel IDs / playlist IDs.
              We exclude their uploads, match titles, and fingerprint audio against
              this library so low-view re-uploads can be proven — not just guessed.
            </p>
          </div>
          <TextField
            label="Official Channel IDs"
            placeholder="UCxxxxxxxx (one per line or comma-separated)"
            fullWidth
            multiline
            minRows={2}
            value={officialChannelIds}
            onChange={(e) => setOfficialChannelIds(e.target.value)}
            disabled={scanning}
            size="small"
            helperText="Find under YouTube Studio → Settings → Channel → Advanced"
          />
          <TextField
            label="Official Playlist IDs"
            placeholder="PLxxxxxxxx (course / batch playlists)"
            fullWidth
            multiline
            minRows={2}
            value={officialPlaylistIds}
            onChange={(e) => setOfficialPlaylistIds(e.target.value)}
            disabled={scanning}
            size="small"
          />
          <TextField
            label="Known Pirate Channels to Monitor"
            placeholder="UCyyyyyyyy (optional seed dump channels)"
            fullWidth
            multiline
            minRows={2}
            value={knownPirateChannels}
            onChange={(e) => setKnownPirateChannels(e.target.value)}
            disabled={scanning}
            size="small"
            helperText="RSS + deep-crawl these channels every scan"
          />
        </div>

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
              <SearchIcon />
            )
          }
        >
          {scanning ? "Scanning..." : "Start Scan"}
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
            <p className="text-center text-xs text-gray-500">
              The scan is running in the background. This page will
              automatically redirect when complete.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
