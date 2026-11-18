"use client";

import { useState } from "react";
import { Button, Tabs, Tab } from "@mui/material";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import DownloadIcon from "@mui/icons-material/Download";
import toast from "react-hot-toast";

interface ReportViewerProps {
  dmcaReport: string;
  defamationReport: string;
  csv: string;
}

export default function ReportViewer({
  dmcaReport,
  defamationReport,
  csv,
}: ReportViewerProps) {
  const [tab, setTab] = useState(0);

  const reports = [
    { label: "DMCA Report", content: dmcaReport },
    { label: "Defamation Report", content: defamationReport },
  ].filter((r) => r.content);

  const currentContent = reports[tab]?.content ?? "";

  const handleCopy = async () => {
    await navigator.clipboard.writeText(currentContent);
    toast.success("Copied to clipboard");
  };

  const handleDownloadCSV = () => {
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "takedown_report.csv";
    a.click();
    URL.revokeObjectURL(url);
    toast.success("CSV downloaded");
  };

  if (!reports.length) {
    return (
      <p className="text-sm text-gray-500">No reports generated yet.</p>
    );
  }

  return (
    <div className="space-y-4">
      <Tabs value={tab} onChange={(_, v) => setTab(v)} size="small">
        {reports.map((r, i) => (
          <Tab key={i} label={r.label} />
        ))}
      </Tabs>

      <div className="flex gap-2">
        <Button
          size="small"
          variant="outlined"
          startIcon={<ContentCopyIcon />}
          onClick={handleCopy}
        >
          Copy
        </Button>
        {csv && (
          <Button
            size="small"
            variant="outlined"
            startIcon={<DownloadIcon />}
            onClick={handleDownloadCSV}
          >
            Download CSV
          </Button>
        )}
      </div>

      <pre className="max-h-[500px] overflow-auto rounded-lg border border-gray-200 bg-gray-50 p-4 text-xs leading-relaxed dark:border-gray-700 dark:bg-gray-800">
        {currentContent}
      </pre>
    </div>
  );
}
