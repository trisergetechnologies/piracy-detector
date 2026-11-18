"use client";

import { useState, useEffect, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { generateTakedownReport, getHistory } from "@/lib/api";
import type { TakedownReport, ScanRecord } from "@/lib/types";
import ReportViewer from "@/components/ReportViewer";
import {
  TextField,
  Button,
  MenuItem,
  CircularProgress,
  Alert,
  FormControlLabel,
  Switch,
} from "@mui/material";
import GavelIcon from "@mui/icons-material/Gavel";
import toast from "react-hot-toast";

export default function ReportsPage() {
  return (
    <Suspense fallback={<div className="p-8 text-gray-400">Loading...</div>}>
      <ReportsContent />
    </Suspense>
  );
}

function ReportsContent() {
  const searchParams = useSearchParams();
  const prefillScanId = searchParams.get("scan_id") || "";

  const [scans, setScans] = useState<ScanRecord[]>([]);
  const [scanId, setScanId] = useState(prefillScanId);
  const [entityName, setEntityName] = useState("");
  const [entityType, setEntityType] = useState("individual");
  const [complainantName, setComplainantName] = useState("");
  const [contactEmail, setContactEmail] = useState("");

  const [generating, setGenerating] = useState(false);
  const [report, setReport] = useState<TakedownReport | null>(null);
  const [error, setError] = useState("");
  const [requireConfirmed, setRequireConfirmed] = useState(false);

  useEffect(() => {
    getHistory(100)
      .then((data) => {
        const completedScans = (data.scans || []).filter(
          (s) => s.status === "completed"
        );
        setScans(completedScans);

        if (prefillScanId) {
          const match = completedScans.find(
            (s) => s.scan_id === prefillScanId
          );
          if (match) {
            setEntityName(match.entity_name);
            setEntityType(match.entity_type || "individual");
          }
        }
      })
      .catch(() => {});
  }, [prefillScanId]);

  const handleScanSelect = (id: string) => {
    setScanId(id);
    const match = scans.find((s) => s.scan_id === id);
    if (match) {
      setEntityName(match.entity_name);
      setEntityType(match.entity_type || "individual");
    }
  };

  const handleGenerate = async () => {
    if (!scanId) {
      setError("Please select a scan");
      return;
    }
    if (!complainantName.trim()) {
      setError("Complainant name is required");
      return;
    }

    setError("");
    setGenerating(true);

    try {
      const data = await generateTakedownReport({
        scan_id: scanId,
        entity_name: entityName,
        entity_type: entityType,
        complainant_name: complainantName.trim(),
        contact_email: contactEmail.trim(),
        require_confirmed: requireConfirmed,
      });

      if (data.ok) {
        setReport(data);
        toast.success(`Report generated (${data.video_count} videos)`);
      } else {
        setError("Failed to generate report");
      }
    } catch (err: any) {
      const msg =
        err?.response?.data?.error || err?.message || "Generation failed";
      setError(msg);
      toast.error(msg);
    } finally {
      setGenerating(false);
    }
  };

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">
          Takedown Reports
        </h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Generate DMCA and defamation takedown reports from scan results
        </p>
      </div>

      {/* Form */}
      <div className="card max-w-2xl space-y-5">
        <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
          Report Configuration
        </h2>

        {/* Scan selector */}
        <TextField
          label="Select Scan"
          select
          fullWidth
          value={scanId}
          onChange={(e) => handleScanSelect(e.target.value)}
          size="small"
          disabled={generating}
        >
          {scans.length === 0 && (
            <MenuItem value="" disabled>
              No completed scans available
            </MenuItem>
          )}
          {scans.map((s) => (
            <MenuItem key={s.scan_id} value={s.scan_id}>
              {s.entity_name} &mdash;{" "}
              {new Date(s.created_at).toLocaleDateString()} (
              {s.summary?.defamatory_found || 0}D / {s.summary?.pirated_found || 0}P)
            </MenuItem>
          ))}
        </TextField>

        <TextField
          label="Entity Name"
          fullWidth
          value={entityName}
          onChange={(e) => setEntityName(e.target.value)}
          size="small"
          disabled={generating}
        />

        <TextField
          label="Complainant Name"
          fullWidth
          required
          value={complainantName}
          onChange={(e) => setComplainantName(e.target.value)}
          placeholder="Legal name of the person filing the report"
          size="small"
          disabled={generating}
        />

        <TextField
          label="Contact Email"
          fullWidth
          type="email"
          value={contactEmail}
          onChange={(e) => setContactEmail(e.target.value)}
          placeholder="email@example.com"
          size="small"
          disabled={generating}
        />

        <FormControlLabel
          control={
            <Switch
              checked={requireConfirmed}
              onChange={(e) => setRequireConfirmed(e.target.checked)}
              disabled={generating}
            />
          }
          label="Only include Review Queue → Confirmed hits (excludes false positives)"
        />

        {error && (
          <Alert severity="error" onClose={() => setError("")}>
            {error}
          </Alert>
        )}

        <Button
          variant="contained"
          fullWidth
          onClick={handleGenerate}
          disabled={generating || !scanId}
          startIcon={
            generating ? (
              <CircularProgress size={18} color="inherit" />
            ) : (
              <GavelIcon />
            )
          }
        >
          {generating ? "Generating..." : "Generate Takedown Report"}
        </Button>
      </div>

      {/* Report output */}
      {report && (
        <div className="card space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
              Generated Report
            </h2>
            <span className="text-xs text-gray-400">
              ID: {report.report_id} &middot; {report.video_count} videos
            </span>
          </div>
          <ReportViewer
            dmcaReport={report.dmca_report}
            defamationReport={report.defamation_report}
            csv={report.csv}
          />
        </div>
      )}
    </div>
  );
}
