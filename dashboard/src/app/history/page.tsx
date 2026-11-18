"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getHistory } from "@/lib/api";
import type { ScanRecord } from "@/lib/types";
import { DataGrid, GridColDef } from "@mui/x-data-grid";
import { Chip, TextField, Skeleton } from "@mui/material";

const columns: GridColDef[] = [
  {
    field: "entity_name",
    headerName: "Entity",
    flex: 1,
    minWidth: 150,
  },
  {
    field: "entity_type",
    headerName: "Type",
    width: 120,
    renderCell: (p) => (
      <Chip label={p.value} size="small" variant="outlined" />
    ),
  },
  {
    field: "scan_type",
    headerName: "Scan",
    width: 120,
    renderCell: (p) => (
      <Chip
        label={p.value}
        size="small"
        color={
          p.value === "both"
            ? "primary"
            : p.value === "defamatory"
            ? "error"
            : "warning"
        }
        variant="outlined"
      />
    ),
  },
  {
    field: "status",
    headerName: "Status",
    width: 120,
    renderCell: (p) => (
      <Chip
        label={p.value}
        size="small"
        color={p.value === "completed" ? "success" : "warning"}
      />
    ),
  },
  {
    field: "defamatory_found",
    headerName: "Defamatory",
    width: 110,
    type: "number",
    valueGetter: (_, row) => row.summary?.defamatory_found ?? 0,
  },
  {
    field: "pirated_found",
    headerName: "Pirated",
    width: 100,
    type: "number",
    valueGetter: (_, row) => row.summary?.pirated_found ?? 0,
  },
  {
    field: "created_at",
    headerName: "Date",
    width: 180,
    valueFormatter: (value: string) =>
      value ? new Date(value).toLocaleString() : "",
  },
];

export default function HistoryPage() {
  const router = useRouter();
  const [scans, setScans] = useState<ScanRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");

  useEffect(() => {
    getHistory(200)
      .then((data) => setScans(data.scans || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const filtered = search
    ? scans.filter((s) =>
        s.entity_name.toLowerCase().includes(search.toLowerCase())
      )
    : scans;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Scan History</h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Browse all previous scans
        </p>
      </div>

      <TextField
        placeholder="Search by entity name..."
        size="small"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        sx={{ maxWidth: 360 }}
        fullWidth
      />

      {loading ? (
        <Skeleton variant="rounded" height={400} sx={{ borderRadius: 3 }} />
      ) : (
        <div className="card !p-0 overflow-hidden">
          <DataGrid
            rows={filtered}
            columns={columns}
            getRowId={(row) => row.scan_id}
            pageSizeOptions={[10, 25, 50]}
            initialState={{
              pagination: { paginationModel: { pageSize: 10 } },
              sorting: { sortModel: [{ field: "created_at", sort: "desc" }] },
            }}
            onRowClick={(p) => router.push(`/scan/${p.id}`)}
            disableRowSelectionOnClick
            autoHeight
            sx={{
              border: "none",
              "& .MuiDataGrid-row": { cursor: "pointer" },
              "& .MuiDataGrid-columnHeaders": {
                backgroundColor: "rgba(0,0,0,0.02)",
              },
            }}
          />
        </div>
      )}
    </div>
  );
}
